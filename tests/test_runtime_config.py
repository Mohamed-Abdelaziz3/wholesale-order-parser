"""Regression tests for the single-merchant production startup contract."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.runtime_config as runtime_config
from app.main import create_app
from app.runtime_config import (
    ProductionConfigurationError,
    load_runtime_environment,
    validate_production_configuration,
)
from app.security import announce, client_key, load_auth_config


def _mountinfo_escape(path: str) -> str:
    """Encode the path field exactly as Linux mountinfo does."""

    return (
        path.replace("\\", r"\134")
        .replace(" ", r"\040")
        .replace("\t", r"\011")
        .replace("\n", r"\012")
    )


def mountinfo_for(*mount_points: str) -> str:
    """Build raw kernel mountinfo text for semantic parser tests."""

    root = _mountinfo_escape(os.path.abspath(os.path.sep))
    lines = [f"25 1 0:24 / {root} rw,relatime - overlay overlay rw"]
    for index, mount_point in enumerate(mount_points, start=26):
        lines.append(
            f"{index} 25 0:{index} / {_mountinfo_escape(mount_point)} "
            "rw,relatime - ext4 /dev/vdb rw"
        )
    return "\n".join(lines)


def install_mountinfo(monkeypatch, *mount_points: str) -> None:
    """Mock raw procfs evidence while exercising the real parser and checks."""

    monkeypatch.setattr(
        runtime_config,
        "_read_linux_mountinfo",
        lambda: mountinfo_for(*mount_points),
    )


def production_env_for_volume(
    volume: Path,
    *,
    db_path: Path | str | None = None,
    **overrides: str,
) -> dict[str, str]:
    """Return complete production configuration using host-native absolute paths."""

    resolved_db_path = Path(db_path) if db_path is not None else volume / "orders.db"
    return production_env(
        PERSISTENT_VOLUME_PATH=str(volume),
        ORDERS_DB_PATH=str(resolved_db_path),
        CATALOG_BACKUP_DIR=str(volume / "catalog-backups"),
        RAILWAY_VOLUME_MOUNT_PATH=str(volume),
        **overrides,
    )


def production_env(**overrides: str) -> dict[str, str]:
    """Return a complete non-secret-shaped production configuration fixture."""
    volume = (Path(__file__).resolve().parent / "runtime-config-volume").resolve()
    values = {
        "APP_ENV": "production",
        "GEMINI_API_KEY": "AIzaSyD-demo-key-value-with-enough-length-12345",
        "APP_USERS": "reviewer:7fVx6-M1aDqJp9uW3zK8sR4t",
        "APP_SESSION_SECRET": "xZ7vM3nQ8pL2rT6wY9aC4dF1hJ5kS0uE",
        "SESSION_HTTPS_ONLY": "true",
        "TRUST_PROXY_HEADERS": "true",
        "FORWARDED_ALLOW_IPS": "127.0.0.1/8,10.0.0.0/8,100.0.0.0/8",
        "PERSISTENT_VOLUME_PATH": str(volume),
        "ORDERS_DB_PATH": str(volume / "orders.db"),
        "CATALOG_BACKUP_DIR": str(volume / "catalog-backups"),
        "RAILWAY_VOLUME_MOUNT_PATH": str(volume),
    }
    values.update(overrides)
    return values


def test_complete_production_configuration_is_accepted(monkeypatch):
    env = production_env()
    install_mountinfo(monkeypatch, env["PERSISTENT_VOLUME_PATH"])
    validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("GEMINI_API_KEY", "", "GEMINI_API_KEY"),
        ("APP_USERS", "", "APP_USERS"),
        ("APP_SESSION_SECRET", "short", "APP_SESSION_SECRET"),
        ("SESSION_HTTPS_ONLY", "false", "SESSION_HTTPS_ONLY"),
        ("TRUST_PROXY_HEADERS", "false", "TRUST_PROXY_HEADERS"),
        ("FORWARDED_ALLOW_IPS", "*", "FORWARDED_ALLOW_IPS"),
        ("PERSISTENT_VOLUME_PATH", "", "PERSISTENT_VOLUME_PATH"),
        ("ORDERS_DB_PATH", "", "ORDERS_DB_PATH"),
        ("RAILWAY_VOLUME_MOUNT_PATH", "", "RAILWAY_VOLUME_MOUNT_PATH"),
    ],
)
def test_production_rejects_each_required_safety_control(
    field, value, expected, monkeypatch
):
    baseline = production_env()
    env = production_env(**{field: value})
    install_mountinfo(monkeypatch, baseline["PERSISTENT_VOLUME_PATH"])
    with pytest.raises(ProductionConfigurationError, match=expected):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_rejects_shared_password_and_auto_generated_authentication():
    env = production_env(APP_USERS="", APP_PASSWORD="this-must-not-be-used")
    with pytest.raises(ProductionConfigurationError, match="APP_USERS"):
        load_auth_config(env)


def test_production_rejects_weak_named_account_password():
    with pytest.raises(ProductionConfigurationError, match="generated random value"):
        validate_production_configuration(production_env(APP_USERS="reviewer:password"))


def test_production_rejects_the_example_password_placeholder():
    with pytest.raises(ProductionConfigurationError, match="generated random value"):
        validate_production_configuration(
            production_env(APP_USERS="reviewer:change_me_before_deploying")
        )


def test_production_rejects_database_and_backup_paths_outside_volume():
    volume = Path(production_env()["PERSISTENT_VOLUME_PATH"])
    outside = volume.parent / "outside-runtime-config-volume"
    with pytest.raises(ProductionConfigurationError, match="ORDERS_DB_PATH must be inside"):
        validate_production_configuration(
            production_env(ORDERS_DB_PATH=str(outside / "orders.db"))
        )
    with pytest.raises(ProductionConfigurationError, match="CATALOG_BACKUP_DIR must be inside"):
        validate_production_configuration(
            production_env(CATALOG_BACKUP_DIR=str(outside / "backups"))
        )


def test_production_requires_railway_mount_path_to_match_explicit_volume_path():
    with pytest.raises(ProductionConfigurationError, match="RAILWAY_VOLUME_MOUNT_PATH"):
        validate_production_configuration(
            production_env(RAILWAY_VOLUME_MOUNT_PATH="/wrong-mounted-volume")
        )


def test_railway_runtime_cannot_silently_use_development_mode():
    with pytest.raises(ProductionConfigurationError, match="APP_ENV=production"):
        validate_production_configuration({"RAILWAY_SERVICE_ID": "service-id"})


def test_application_factory_refuses_production_without_confirmed_volume(monkeypatch):
    """The deployed ASGI entry point runs the fail-closed validation itself."""
    env = production_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    install_mountinfo(monkeypatch, env["PERSISTENT_VOLUME_PATH"])
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")

    with pytest.raises(ProductionConfigurationError, match="RAILWAY_VOLUME_MOUNT_PATH"):
        create_app(env["ORDERS_DB_PATH"])


def test_production_rejects_existing_plain_directory_that_is_not_a_mount(
    tmp_path, monkeypatch
):
    """An image directory is not evidence of a mounted persistent volume."""

    volume = tmp_path / "app" / "data"
    volume.mkdir(parents=True)
    env = production_env_for_volume(volume)
    install_mountinfo(monkeypatch)

    with pytest.raises(ProductionConfigurationError, match="actual mounted filesystem"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_rejects_declared_volume_absent_from_mount_table(tmp_path, monkeypatch):
    """A claimed Railway path is rejected when kernel mountinfo does not contain it."""

    volume = tmp_path / "declared-volume"
    env = production_env_for_volume(volume)
    install_mountinfo(monkeypatch)

    with pytest.raises(ProductionConfigurationError, match="actual mounted filesystem"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_rejects_database_outside_verified_mount(tmp_path, monkeypatch):
    volume = tmp_path / "mounted-volume"
    volume.mkdir()
    env = production_env_for_volume(
        volume,
        db_path=tmp_path / "ephemeral" / "orders.db",
    )
    install_mountinfo(monkeypatch, str(volume))

    with pytest.raises(ProductionConfigurationError, match="ORDERS_DB_PATH must be inside"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_accepts_database_inside_verified_mount(tmp_path, monkeypatch):
    volume = tmp_path / "mounted-volume"
    volume.mkdir()
    env = production_env_for_volume(volume)
    install_mountinfo(monkeypatch, str(volume))

    validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_database_dotdot_path_cannot_escape_verified_mount(tmp_path, monkeypatch):
    volume = tmp_path / "mounted-volume"
    volume.mkdir()
    escaped_db_path = volume / ".." / "ephemeral" / "orders.db"
    env = production_env_for_volume(volume, db_path=escaped_db_path)
    install_mountinfo(monkeypatch, str(volume))

    with pytest.raises(ProductionConfigurationError, match="ORDERS_DB_PATH must be inside"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_database_symlink_cannot_escape_verified_mount(tmp_path, monkeypatch):
    volume = tmp_path / "mounted-volume"
    outside = tmp_path / "ephemeral"
    volume.mkdir()
    outside.mkdir()
    escape_link = volume / "escape"
    try:
        escape_link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("This platform cannot create the symlink needed for the safety test.")

    env = production_env_for_volume(volume, db_path=escape_link / "orders.db")
    install_mountinfo(monkeypatch, str(volume))

    with pytest.raises(ProductionConfigurationError, match="ORDERS_DB_PATH must be inside"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


@pytest.mark.parametrize(
    "mountinfo",
    [
        "malformed",
        "",
        "25 1 0:24 / / rw,relatime - ext4 /dev/vdb",
    ],
)
def test_production_rejects_unusable_linux_mountinfo(monkeypatch, mountinfo):
    env = production_env()
    monkeypatch.setattr(runtime_config, "_read_linux_mountinfo", lambda: mountinfo)

    with pytest.raises(ProductionConfigurationError, match="could not be verified"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_rejects_unreadable_linux_mountinfo(monkeypatch):
    class UnreadableMountinfo:
        def read_text(self, *, encoding: str) -> str:
            raise OSError("permission denied")

    env = production_env()
    monkeypatch.setattr(runtime_config.sys, "platform", "linux")
    monkeypatch.setattr(runtime_config, "_LINUX_MOUNTINFO_PATH", UnreadableMountinfo())

    with pytest.raises(ProductionConfigurationError, match="could not be verified"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_production_rejects_non_linux_runtime_without_mount_evidence(monkeypatch):
    env = production_env()
    monkeypatch.setattr(runtime_config.sys, "platform", "win32")

    with pytest.raises(ProductionConfigurationError, match="could not be verified"):
        validate_production_configuration(env, db_path=env["ORDERS_DB_PATH"])


def test_development_does_not_require_or_read_a_railway_mount(monkeypatch):
    def unexpected_mount_read() -> str:
        raise AssertionError("development validation must not inspect mountinfo")

    monkeypatch.setattr(runtime_config, "_read_linux_mountinfo", unexpected_mount_read)
    validate_production_configuration({"APP_ENV": "development"})


def test_runtime_loader_does_not_search_the_current_working_directory(tmp_path, monkeypatch):
    """A synced repository .env cannot be accidentally loaded by import order."""
    (tmp_path / ".env").write_text("UNSAFE_CWD_SECRET=should-not-load\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WOP_ENV_FILE", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "machine-local-program-data"))
    monkeypatch.delenv("UNSAFE_CWD_SECRET", raising=False)

    assert load_runtime_environment() is None
    assert "UNSAFE_CWD_SECRET" not in __import__("os").environ


def test_runtime_loader_rejects_cloud_synced_explicit_env_file(tmp_path, monkeypatch):
    synced_file = tmp_path / "OneDrive" / "wop.env"
    synced_file.parent.mkdir()
    synced_file.write_text("APP_ENV=development\n", encoding="utf-8")
    monkeypatch.setenv("WOP_ENV_FILE", str(synced_file))

    with pytest.raises(ProductionConfigurationError, match="cloud-synchronised"):
        load_runtime_environment()


def test_generated_password_is_never_logged(caplog):
    config = load_auth_config({})
    announce(config)
    assert config.shared_password
    assert config.shared_password not in caplog.text


def test_malformed_credential_error_does_not_echo_its_value():
    secret_like_value = "this-must-not-appear-in-an-error-or-log"
    with pytest.raises(ValueError) as raised:
        load_auth_config({"APP_USERS": secret_like_value})
    assert secret_like_value not in str(raised.value)


def test_client_key_uses_only_the_proxy_normalised_request_client():
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.200"},
        client=SimpleNamespace(host="10.0.0.25"),
    )
    assert client_key(request) == "10.0.0.25"
