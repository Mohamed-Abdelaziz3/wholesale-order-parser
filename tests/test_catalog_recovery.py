"""Catalog-only recovery must never rewind approved operational history."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import database
from app.catalog_backups import CatalogBackupError, CatalogBackupUnavailableError, read_snapshot
from app.catalog_maintenance import main as catalog_maintenance_main
from app.models import CatalogProduct


def _catalog(sku: str, name: str, price: str = "10.00") -> list[dict]:
    return [
        {
            "product_id": sku,
            "product_name": name,
            "aliases": [],
            "unit": "piece",
            "price": price,
        }
    ]


def _backup_path(db_path, backup: dict):
    return db_path.parent / "catalog-backups" / backup["filename"]


def _snapshot_sku(db_path, backup: dict) -> str:
    return read_snapshot(
        db_path=db_path,
        filename=backup["filename"],
        backup_id=backup["backup_id"],
    )["products"][0]["product_id"]


def _backup_for_snapshot_sku(db_path, sku: str) -> dict:
    for backup in database.list_catalog_backups(db_path):
        if backup["availability_status"] == database.CATALOG_BACKUP_AVAILABLE and _snapshot_sku(
            db_path, backup
        ) == sku:
            return backup
    raise AssertionError(f"No available backup contains SKU {sku!r}")


def _approve_a_record(db_path, sku: str = "A") -> int:
    order_id = database.save_processed_order(
        "synthetic customer order",
        [
            {
                "raw_text": "2 pieces product",
                "extracted_product": "Product A",
                "extracted_quantity": 2,
                "extracted_unit": "piece",
                "recommendation_product": {
                    "product_id": sku,
                    "product_name": "Catalog A",
                    "unit": "piece",
                    "price": "10.00",
                },
                "recommendation_decision": "SELECT",
                "confidence": 0.9,
                "status": "needs_review",
                "candidates": [],
            }
        ],
        [],
        1.0,
        db_path,
    )
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    lookup = {
        sku: CatalogProduct(
            product_id=sku, product_name="Catalog A", aliases=[], unit="piece", price="10.00"
        )
    }
    database.review_order_item(
        order_id,
        item_id,
        actor="operator",
        action_id=f"review-{order_id}",
        final_decision="SELECT",
        selected_sku=sku,
        quantity=2,
        unit="piece",
        catalog_lookup=lookup,
        db_path=db_path,
    )
    database.approve_order(
        order_id, actor="operator", action_id=f"approve-{order_id}", db_path=db_path
    )
    return order_id


def test_catalog_a_replace_b_restore_a_preserves_approved_history(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    order_id = _approve_a_record(db_path)

    database.replace_catalog(_catalog("B", "Catalog B", "25.00"), db_path, actor="operator")
    backups = database.list_catalog_backups(db_path)
    assert len(backups) == 1
    assert backups[0]["product_count"] == 1

    restored = database.restore_catalog_backup(
        backups[0]["backup_id"], actor="operator", db_path=db_path
    )
    assert restored["operation"] == "restore"
    assert restored["source_backup_id"] == backups[0]["backup_id"]
    assert database.load_catalog_rows(db_path) == [
        {"product_id": "A", "product_name": "Catalog A", "aliases": [], "unit": "piece", "price": "10.00"}
    ]

    # Restore changes catalog rows only. The approved snapshot remains immutable
    # and is still exportable after catalog B existed and catalog A returned.
    order = database.get_order_by_id(order_id, db_path)
    assert order["status"] == "approved"
    assert order["approved_items"][0]["product_id"] == "A"
    exported = database.record_export(order_id, db_path)
    assert exported["snapshot"][0]["price"] == "10.00"


def test_backup_write_failure_aborts_before_catalog_delete(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")

    def fail_snapshot(*_args, **_kwargs):
        raise CatalogBackupError("simulated durable-volume failure")

    monkeypatch.setattr(database, "write_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="backup failed"):
        database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "A"


def test_missing_historical_backup_is_retired_and_does_not_block_future_uploads(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    orphan = database.list_catalog_backups(db_path)[0]
    _backup_path(db_path, orphan).unlink()

    database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[orphan["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_MISSING
    assert records[orphan["backup_id"]]["availability_reason"] == "Snapshot file is missing"
    backup_a = _backup_for_snapshot_sku(db_path, "A")
    assert backup_a["availability_status"] == database.CATALOG_BACKUP_AVAILABLE
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "B"

    # The same old orphan cannot turn the next upload into a retention deadlock.
    database.replace_catalog(_catalog("C", "Catalog C"), db_path, actor="operator")
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "C"

    with pytest.raises(ValueError, match="unavailable"):
        database.restore_catalog_backup(orphan["backup_id"], actor="operator", db_path=db_path)
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "C"


def test_orphan_status_commits_even_if_the_later_replacement_aborts(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    orphan = database.list_catalog_backups(db_path)[0]
    _backup_path(db_path, orphan).unlink()

    def fail_snapshot(*_args, **_kwargs):
        raise CatalogBackupError("simulated durable-volume failure")

    monkeypatch.setattr(database, "write_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="backup failed"):
        database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[orphan["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_MISSING
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "A"


def test_transient_backup_read_failure_aborts_without_retiring_valid_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    backup = database.list_catalog_backups(db_path)[0]
    original_read_snapshot = database.read_snapshot

    def temporarily_unreadable(*_args, **_kwargs):
        raise CatalogBackupUnavailableError("simulated transient storage fault")

    monkeypatch.setattr(database, "read_snapshot", temporarily_unreadable)
    with pytest.raises(RuntimeError, match="could not be inspected safely"):
        database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[backup["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_AVAILABLE
    assert _backup_path(db_path, backup).exists()
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "A"

    monkeypatch.setattr(database, "read_snapshot", original_read_snapshot)
    database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "B"


def test_corrupt_historical_backup_is_retained_as_evidence_and_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")
    corrupt = _backup_for_snapshot_sku(db_path, "A")
    path = _backup_path(db_path, corrupt)
    payload = json.loads(path.read_text(encoding="utf-8"))
    # This malformed but parseable JSON used to escape ``read_snapshot`` as a
    # raw ValueError during integer canonicalization. It must be recorded as
    # CORRUPT rather than recreating the historical upload deadlock.
    payload["products"][0]["sort_order"] = "not-an-integer"
    path.write_text(json.dumps(payload), encoding="utf-8")

    database.replace_catalog(_catalog("C", "Catalog C"), db_path, actor="operator")

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[corrupt["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_CORRUPT
    assert (
        records[corrupt["backup_id"]]["availability_reason"]
        == "Snapshot integrity validation failed"
    )
    assert path.exists()
    with pytest.raises(ValueError, match="unavailable"):
        database.restore_catalog_backup(corrupt["backup_id"], actor="operator", db_path=db_path)
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "C"


def test_valid_backup_restores_after_orphan_without_touching_approved_history(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    orphan = database.list_catalog_backups(db_path)[0]
    _backup_path(db_path, orphan).unlink()
    order_id = _approve_a_record(db_path)

    database.replace_catalog(_catalog("B", "Catalog B", "25.00"), db_path, actor="operator")
    backup_a = _backup_for_snapshot_sku(db_path, "A")
    database.restore_catalog_backup(backup_a["backup_id"], actor="operator", db_path=db_path)

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[orphan["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_MISSING
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "A"
    order = database.get_order_by_id(order_id, db_path)
    assert order["status"] == "approved"
    assert order["approved_items"][0]["product_id"] == "A"
    assert database.record_export(order_id, db_path)["snapshot"][0]["price"] == "10.00"


def test_retention_skips_missing_backup_evidence_and_catalog_uploads_continue(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    monkeypatch.setattr(database, "CATALOG_BACKUP_RETENTION", 2)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("SEED", "Seed"), db_path, actor="operator")
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    orphan = database.list_catalog_backups(db_path)[0]
    _backup_path(db_path, orphan).unlink()

    for sku in ("B", "C", "D", "E"):
        database.replace_catalog(_catalog(sku, f"Catalog {sku}"), db_path, actor="operator")

    records = database.list_catalog_backups(db_path)
    missing = next(row for row in records if row["backup_id"] == orphan["backup_id"])
    available = [
        row for row in records if row["availability_status"] == database.CATALOG_BACKUP_AVAILABLE
    ]
    assert missing["availability_status"] == database.CATALOG_BACKUP_MISSING
    assert len(available) == 2
    assert all(_backup_path(db_path, row).exists() for row in available)
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "E"


def test_restore_marks_backup_missing_if_file_disappears_after_reconciliation(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")
    backup_a = _backup_for_snapshot_sku(db_path, "A")
    original_reconcile = database._reconcile_catalog_backup_availability

    def remove_after_reconciliation(path):
        original_reconcile(path)
        _backup_path(db_path, backup_a).unlink()

    monkeypatch.setattr(
        database,
        "_reconcile_catalog_backup_availability",
        remove_after_reconciliation,
    )
    with pytest.raises(ValueError, match="validation failed"):
        database.restore_catalog_backup(backup_a["backup_id"], actor="operator", db_path=db_path)

    records = {row["backup_id"]: row for row in database.list_catalog_backups(db_path)}
    assert records[backup_a["backup_id"]]["availability_status"] == database.CATALOG_BACKUP_MISSING
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "B"


def test_concurrent_catalog_replacements_leave_only_valid_available_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")

    def replace(sku: str):
        return database.replace_catalog(_catalog(sku, f"Catalog {sku}"), db_path, actor="operator")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replace, ("B", "C")))

    assert {result["product_count"] for result in results} == {1}
    records = database.list_catalog_backups(db_path)
    assert len({row["backup_id"] for row in records}) == len(records)
    assert len({row["filename"] for row in records}) == len(records)
    for backup in records:
        if backup["availability_status"] == database.CATALOG_BACKUP_AVAILABLE:
            assert _snapshot_sku(db_path, backup) in {"A", "B", "C"}


def test_recovery_retention_is_bounded_to_ten_recent_snapshots(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    for index in range(12):
        database.replace_catalog(
            _catalog(f"SKU-{index}", f"Product {index}"), db_path, actor="operator"
        )
    backups = database.list_catalog_backups(db_path)
    assert len(backups) == 10
    assert all((tmp_path / "catalog-backups" / row["filename"]).exists() for row in backups)


def test_validated_legacy_price_backup_restores_without_rounding(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    # Simulate a catalog already present before the two-decimal pilot invariant.
    with database.session(db_path, immediate=True) as conn:
        conn.execute(
            "INSERT INTO catalog_products VALUES ('LEGACY', 'Legacy', '', 'piece', '19.995', 0, '2026-01-01')"
        )
    database.replace_catalog(_catalog("NEW", "New"), db_path, actor="operator")
    backup = database.list_catalog_backups(db_path)[0]
    database.restore_catalog_backup(backup["backup_id"], actor="operator", db_path=db_path)
    assert database.load_catalog_rows(db_path)[0]["price"] == "19.995"


def test_restore_cli_requires_stopped_service_acknowledgement(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    database.replace_catalog(_catalog("A", "Catalog A"), db_path, actor="operator")
    database.replace_catalog(_catalog("B", "Catalog B"), db_path, actor="operator")
    backup_id = database.list_catalog_backups(db_path)[0]["backup_id"]

    assert catalog_maintenance_main(
        ["--db", str(db_path), "restore", "--backup-id", backup_id, "--actor", "operator"]
    ) == 2
    assert "stop the one-replica" in capsys.readouterr().err
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "B"

    assert catalog_maintenance_main(
        [
            "--db", str(db_path),
            "restore",
            "--backup-id",
            backup_id,
            "--actor",
            "operator",
            "--confirm-service-stopped",
        ]
    ) == 0
    assert database.load_catalog_rows(db_path)[0]["product_id"] == "A"
