"""File-level safety checks for durable catalog-only recovery snapshots."""

import json

import pytest

from app.catalog_backups import (
    CatalogBackupError,
    prune_snapshot_files,
    read_snapshot,
    write_snapshot,
)


def _rows(name: str = "Catalog A"):
    return [
        {
            "product_id": "SKU-1",
            "product_name": name,
            "aliases": ["alias"],
            "unit": "piece",
            "price": "19.99",
            "sort_order": 0,
        }
    ]


def test_snapshot_round_trip_is_catalog_only_and_hash_validated(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    written = write_snapshot(
        _rows(),
        db_path=db_path,
        source_catalog_version=4,
        actor="operator",
        created_at="2026-08-09T12:30:00+00:00",
    )
    restored = read_snapshot(
        db_path=db_path,
        filename=written["filename"],
        backup_id=written["backup_id"],
    )
    assert restored["products"] == _rows()
    assert restored["source_catalog_version"] == 4
    assert "original_text" not in json.dumps(restored, ensure_ascii=False)


def test_snapshot_rejects_tampered_content_and_unsafe_file_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    written = write_snapshot(
        _rows(),
        db_path=db_path,
        source_catalog_version=None,
        actor=None,
        created_at="2026-08-09T12:31:00+00:00",
    )
    path = tmp_path / "catalog-backups" / written["filename"]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["products"][0]["product_name"] = "Tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogBackupError, match="hash"):
        read_snapshot(db_path=db_path, filename=written["filename"], backup_id=written["backup_id"])
    with pytest.raises(CatalogBackupError, match="filename"):
        read_snapshot(db_path=db_path, filename="../orders.db", backup_id=written["backup_id"])


def test_retention_removes_only_old_generated_snapshot_files(tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("PERSISTENT_VOLUME_PATH", raising=False)
    db_path = tmp_path / "orders.db"
    first = write_snapshot(
        _rows("A"), db_path=db_path, source_catalog_version=1, actor="a", created_at="2026-08-09T10:00:00+00:00"
    )
    second = write_snapshot(
        _rows("B"), db_path=db_path, source_catalog_version=2, actor="a", created_at="2026-08-09T11:00:00+00:00"
    )
    removed = prune_snapshot_files([first, second], db_path=db_path, retain=1)
    assert removed == [first["backup_id"]]
    with pytest.raises(CatalogBackupError, match="cannot be read"):
        read_snapshot(db_path=db_path, filename=first["filename"], backup_id=first["backup_id"])
    assert read_snapshot(db_path=db_path, filename=second["filename"], backup_id=second["backup_id"])["products"][0]["product_name"] == "B"
