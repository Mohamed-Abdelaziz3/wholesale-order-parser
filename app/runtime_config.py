"""Explicit runtime configuration for the single-merchant pilot.

Production is intentionally a narrow deployment profile: one Railway service,
one SQLite database, and one mounted persistent volume.  Configuration is
validated before the application starts accepting traffic so a missing secret
or an ephemeral database cannot look like a successful deployment.

This module deliberately does *not* load ``.env`` from the working directory.
The repository may live in a cloud-synchronised folder; local secrets belong in
an explicitly selected, machine-local file or in the process environment.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

PRODUCTION_ENVIRONMENT = "production"
PERSISTENT_VOLUME_ENV = "PERSISTENT_VOLUME_PATH"
DEFAULT_WINDOWS_ENV_FILE = Path(r"C:\ProgramData\wop\wop.env")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PLACEHOLDER_VALUES = frozenset(
    {
        "change-me",
        "change_me",
        "change_me_before_deploying",
        "example",
        "password",
        "replace-me",
        "test",
        "test-password",
        "your_gemini_api_key_here",
        "your-secret-here",
    }
)
_NORMALISED_PLACEHOLDER_VALUES = frozenset(
    value.replace("_", "-") for value in _PLACEHOLDER_VALUES
)
_RAILWAY_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
_LINUX_MOUNTINFO_PATH = Path("/proc/self/mountinfo")


class ProductionConfigurationError(RuntimeError):
    """Raised when a pilot deployment would be unsafe to start."""


class _MountVerificationError(RuntimeError):
    """Raised when the Linux mount table cannot safely be used as evidence."""


def _value(source: Mapping[str, str], key: str) -> str:
    return (source.get(key, "") or "").strip()


def is_production(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether the explicit application mode is production."""
    source = os.environ if env is None else env
    return _value(source, "APP_ENV").lower() == PRODUCTION_ENVIRONMENT


def is_railway_runtime(env: Optional[Mapping[str, str]] = None) -> bool:
    """Detect Railway only from its runtime-provided identifiers."""
    source = os.environ if env is None else env
    return any(_value(source, marker) for marker in _RAILWAY_MARKERS)


def _looks_like_placeholder(value: str) -> bool:
    compact = value.strip().lower().replace(" ", "").replace("_", "-")
    return not compact or compact in _NORMALISED_PLACEHOLDER_VALUES


def _has_minimum_secret_strength(value: str, *, minimum_length: int) -> bool:
    """Reject obvious placeholders and low-entropy operator mistakes.

    Entropy cannot be proven from a string.  This is deliberately a floor, not
    a password-quality score: operators must use a generated random value.
    """
    return (
        len(value) >= minimum_length
        and not _looks_like_placeholder(value)
        and len(set(value)) >= 4
    )


def _production_authentication_errors(source: Mapping[str, str]) -> list[str]:
    """Return non-secret-bearing errors for the pilot authentication profile."""
    errors: list[str] = []
    users_raw = _value(source, "APP_USERS")
    shared_password = _value(source, "APP_PASSWORD")
    session_secret = _value(source, "APP_SESSION_SECRET")

    if shared_password:
        errors.append("APP_PASSWORD is not permitted in production; configure APP_USERS")
    if not users_raw:
        errors.append("APP_USERS must contain at least one named account in production")
    else:
        seen_names: set[str] = set()
        for position, entry in enumerate(users_raw.split(","), start=1):
            entry = entry.strip()
            if not entry or ":" not in entry:
                errors.append(
                    f"APP_USERS entry {position} must use the format name:password"
                )
                continue
            name, password = (part.strip() for part in entry.split(":", 1))
            if not name:
                errors.append(f"APP_USERS entry {position} has an empty account name")
            elif name in seen_names:
                errors.append(f"APP_USERS contains a duplicate account name: {name!r}")
            else:
                seen_names.add(name)
            if not _has_minimum_secret_strength(password, minimum_length=16):
                errors.append(
                    f"APP_USERS password for account {name or position!r} must be "
                    "a generated random value of at least 16 characters"
                )

    if not _has_minimum_secret_strength(session_secret, minimum_length=32):
        errors.append(
            "APP_SESSION_SECRET must be an explicitly configured generated random "
            "value of at least 32 characters"
        )
    return errors


def validate_production_authentication(
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Fail before generating credentials when ``APP_ENV=production``.

    This is called by :func:`app.security.load_auth_config` as a defence in
    depth even when another entry point bypasses the normal app factory.
    """
    source = os.environ if env is None else env
    if not is_production(source):
        return
    errors = _production_authentication_errors(source)
    if errors:
        raise ProductionConfigurationError(
            "Unsafe production authentication configuration: " + "; ".join(errors)
        )


def _is_enabled(source: Mapping[str, str], key: str) -> bool:
    return _value(source, key).lower() in _TRUE_VALUES


def _normalised_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _is_child_path(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent and child != parent
    except ValueError:
        # Different drive letters (Windows) cannot be inside one another.
        return False


def _decode_mountinfo_path(value: str) -> str:
    """Decode the octal path escapes used by ``/proc/*/mountinfo``.

    Mountinfo separates fields with spaces, so spaces and a few other special
    characters are represented as octal escapes (for example ``\\040``).  Only
    that documented escape form is decoded; ordinary backslashes remain part
    of the path.
    """

    return _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _parse_linux_mountinfo(mountinfo: str) -> frozenset[str]:
    """Return canonical mount points from current-namespace mountinfo.

    The mount-point is the fifth fixed field, before zero or more optional
    fields and the literal ``-`` separator.  Treat malformed kernel evidence
    as unusable rather than guessing which paths are mounted.
    """

    mount_points: set[str] = set()
    for line_number, line in enumerate(mountinfo.splitlines(), start=1):
        fields = line.split()
        if not fields:
            continue
        try:
            separator = fields.index("-")
        except ValueError as exc:
            raise _MountVerificationError(
                f"mountinfo line {line_number} has no separator"
            ) from exc
        if separator < 6 or len(fields) < separator + 4:
            raise _MountVerificationError(
                f"mountinfo line {line_number} is malformed"
            )

        mount_point = _decode_mountinfo_path(fields[4])
        if not os.path.isabs(mount_point):
            raise _MountVerificationError(
                f"mountinfo line {line_number} has a non-absolute mount point"
            )
        mount_points.add(_normalised_path(mount_point))

    if not mount_points:
        raise _MountVerificationError("mountinfo contains no mount points")
    return frozenset(mount_points)


def _read_linux_mountinfo() -> str:
    """Read the current process's Linux mount namespace evidence.

    The assisted pilot's only supported production topology is Railway Linux.
    A non-Linux runtime, unreadable procfs, or malformed data therefore cannot
    be treated as proof that a claimed data directory is persistent.
    """

    if not sys.platform.startswith("linux"):
        raise _MountVerificationError(
            "Linux /proc/self/mountinfo is unavailable in this runtime"
        )
    try:
        return _LINUX_MOUNTINFO_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _MountVerificationError(
            "Linux /proc/self/mountinfo could not be read"
        ) from exc


def _verified_linux_mount_points() -> frozenset[str]:
    """Read and parse mounted filesystem locations, failing closed on error."""

    return _parse_linux_mountinfo(_read_linux_mountinfo())


def _valid_proxy_network(value: str) -> bool:
    """Accept only literal IPs or CIDRs supported by Uvicorn's proxy middleware."""
    if value in {"*", "0.0.0.0/0", "::/0"}:
        return False
    try:
        if "/" in value:
            ipaddress.ip_network(value, strict=False)
        else:
            ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _production_runtime_errors(
    source: Mapping[str, str],
    *,
    db_path: Optional[os.PathLike[str] | str],
) -> list[str]:
    errors: list[str] = []

    api_key = _value(source, "GEMINI_API_KEY")
    if not _has_minimum_secret_strength(api_key, minimum_length=20):
        errors.append("GEMINI_API_KEY must be explicitly configured in production")

    if not _is_enabled(source, "SESSION_HTTPS_ONLY"):
        errors.append("SESSION_HTTPS_ONLY must be true in production")
    if not _is_enabled(source, "TRUST_PROXY_HEADERS"):
        errors.append("TRUST_PROXY_HEADERS must be true for the Railway deployment")

    proxy_raw = _value(source, "FORWARDED_ALLOW_IPS")
    proxy_values = [entry.strip() for entry in proxy_raw.split(",") if entry.strip()]
    if not proxy_values:
        errors.append("FORWARDED_ALLOW_IPS must list Railway proxy networks")
    elif not all(_valid_proxy_network(value) for value in proxy_values):
        errors.append(
            "FORWARDED_ALLOW_IPS must contain explicit IP addresses or CIDRs, never '*'"
        )

    volume_raw = _value(source, PERSISTENT_VOLUME_ENV)
    db_raw = _value(source, "ORDERS_DB_PATH")
    if not volume_raw or not os.path.isabs(volume_raw):
        errors.append(f"{PERSISTENT_VOLUME_ENV} must be an absolute mounted-volume path")
        volume_path = ""
    else:
        volume_path = _normalised_path(volume_raw)
    if not db_raw or not os.path.isabs(db_raw):
        errors.append("ORDERS_DB_PATH must be an explicit absolute path on the volume")
        resolved_db_path = ""
    else:
        resolved_db_path = _normalised_path(db_raw)
        if db_path is not None and _normalised_path(os.fspath(db_path)) != resolved_db_path:
            errors.append("resolved database path does not match ORDERS_DB_PATH")

    if volume_path and resolved_db_path and not _is_child_path(resolved_db_path, volume_path):
        errors.append(f"ORDERS_DB_PATH must be inside {PERSISTENT_VOLUME_ENV}")

    backup_raw = _value(source, "CATALOG_BACKUP_DIR")
    if backup_raw:
        if not os.path.isabs(backup_raw):
            errors.append("CATALOG_BACKUP_DIR must be an absolute path when configured")
        elif volume_path and not _is_child_path(_normalised_path(backup_raw), volume_path):
            errors.append(f"CATALOG_BACKUP_DIR must be inside {PERSISTENT_VOLUME_ENV}")

    # Railway injects this only after a volume is attached. It must agree with
    # the explicit path, but an environment-variable value is not evidence
    # that the path is a real mounted filesystem.
    railway_mount = _value(source, "RAILWAY_VOLUME_MOUNT_PATH")
    if not railway_mount:
        errors.append(
            "RAILWAY_VOLUME_MOUNT_PATH must be injected by Railway after a volume "
            "is attached; do not set it manually"
        )
    elif not os.path.isabs(railway_mount):
        errors.append("RAILWAY_VOLUME_MOUNT_PATH must be an absolute mount path")
    elif volume_raw and _normalised_path(railway_mount) != volume_path:
        errors.append(
            f"{PERSISTENT_VOLUME_ENV} must exactly match RAILWAY_VOLUME_MOUNT_PATH"
        )

    # A directory created in the image (including Dockerfile's /app/data) is
    # not a persistent volume. Railway's supported production runtime is
    # Linux, where the current process mount namespace provides reliable
    # evidence. Require the configured canonical volume path itself to be a
    # mountpoint; an ancestor mount or a matching environment variable is not
    # sufficient evidence for this narrow deployment contract.
    if volume_path:
        try:
            mounted_paths = _verified_linux_mount_points()
        except _MountVerificationError:
            errors.append(
                f"{PERSISTENT_VOLUME_ENV} could not be verified from Linux "
                "/proc/self/mountinfo; refusing production startup"
            )
        else:
            if volume_path not in mounted_paths:
                errors.append(
                    f"{PERSISTENT_VOLUME_ENV} must be an actual mounted filesystem "
                    "reported by /proc/self/mountinfo"
                )

    return errors


def validate_production_configuration(
    env: Optional[Mapping[str, str]] = None,
    *,
    db_path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Fail loudly when the Railway single-merchant safety contract is absent.

    ``db_path`` should be the already-resolved application database path.  It
    catches a caller supplying a path that differs from the checked environment
    variable, which would otherwise reintroduce an ephemeral fallback.
    """
    source = os.environ if env is None else env
    if is_railway_runtime(source) and not is_production(source):
        raise ProductionConfigurationError(
            "Railway deployment requires APP_ENV=production; refusing development defaults"
        )
    if not is_production(source):
        return

    errors = _production_authentication_errors(source)
    errors.extend(_production_runtime_errors(source, db_path=db_path))
    if errors:
        raise ProductionConfigurationError(
            "Unsafe production configuration: " + "; ".join(errors)
        )


def _is_cloud_synced_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts & {"onedrive", "dropbox", "google drive"})


def load_runtime_environment() -> Optional[Path]:
    """Load a machine-local dotenv file without searching the repository.

    ``WOP_ENV_FILE`` is optional but, when set, must be an absolute non-synchronised
    path.  On Windows only, ``C:\\ProgramData\\wop\\wop.env`` is a conventional
    non-synchronised fallback.  In containers and on Unix, use process
    environment variables instead of a hidden cwd ``.env`` lookup.
    """
    configured = _value(os.environ, "WOP_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ProductionConfigurationError("WOP_ENV_FILE must be an absolute path")
    elif os.name == "nt":
        program_data = os.environ.get("PROGRAMDATA")
        path = (
            Path(program_data) / "wop" / "wop.env"
            if program_data
            else DEFAULT_WINDOWS_ENV_FILE
        )
    else:
        return None

    path = path.resolve(strict=False)
    if _is_cloud_synced_path(path):
        raise ProductionConfigurationError(
            "WOP_ENV_FILE must not be located in a cloud-synchronised folder"
        )
    if not path.exists():
        if configured:
            raise ProductionConfigurationError("WOP_ENV_FILE does not exist")
        return None
    if not path.is_file():
        raise ProductionConfigurationError("WOP_ENV_FILE must identify a regular file")

    # Import lazily so pure validation tests do not require an environment loader.
    from dotenv import load_dotenv

    load_dotenv(path, override=False)
    return path
