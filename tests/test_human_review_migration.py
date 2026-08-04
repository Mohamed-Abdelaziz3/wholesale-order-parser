"""Forward-migration tests from the exact fail-open approved snapshot schema."""

import sqlite3

import pytest

from app import database


def create_defective_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'analyzed',
            export_count INTEGER NOT NULL DEFAULT 0,
            last_exported_at TEXT,
            processing_time_ms REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT
        );
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            extracted_product TEXT NOT NULL,
            extracted_quantity REAL NOT NULL,
            extracted_unit TEXT NOT NULL,
            matched_product_id TEXT,
            matched_product_name TEXT,
            matched_unit TEXT,
            matched_price REAL,
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'needs_review',
            is_manually_corrected INTEGER NOT NULL DEFAULT 0,
            candidates_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE approved_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            original_item_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            price REAL NOT NULL,
            confidence REAL NOT NULL,
            is_manually_corrected INTEGER NOT NULL DEFAULT 0,
            approved_at TEXT NOT NULL,
            is_human_confirmed INTEGER NOT NULL DEFAULT 1,
            UNIQUE(order_id, original_item_id)
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        );
        INSERT INTO orders
            (id, original_text, status, created_at, updated_at, approved_at)
        VALUES (1, 'legacy', 'approved', '2026-01-01', '2026-01-01', '2026-01-01');
        INSERT INTO order_items (
            id, order_id, raw_text, extracted_product, extracted_quantity,
            extracted_unit, matched_product_id, matched_product_name, matched_price,
            confidence, status, created_at, updated_at
        ) VALUES (
            10, 1, 'legacy line', 'legacy', 2, 'piece', 'CL010',
            'Flash Lemon', 10, 0.62, 'confirmed', '2026-01-01', '2026-01-01'
        );
        INSERT INTO approved_order_items (
            id, order_id, original_item_id, product_id, product_name, quantity,
            unit, price, confidence, approved_at
        ) VALUES (
            20, 1, 10, 'CL010', 'Flash Lemon', 2, 'piece', 10, 1.0, '2026-01-01'
        );
        INSERT INTO audit_events (order_id, event_type, details, timestamp)
        VALUES (1, 'order_approved', '{"total_approved_items":1}', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()


def test_defective_default_is_migrated_and_legacy_export_fails_closed(tmp_path):
    db_path = tmp_path / "defective.db"
    create_defective_database(db_path)
    preflight = database.preflight_human_review_migration(db_path)
    assert preflight["approved_snapshot_rows"] == 1
    assert preflight["currently_marked_human_confirmed"] == 1
    assert preflight["rows_with_valid_provenance"] == 0
    assert preflight["rows_to_mark_legacy_unverified"] == 1

    database.init_db(db_path)
    database.init_db(db_path)  # idempotent re-entry
    post = database.verify_human_review_migration(db_path)
    assert post == {
        "migration_id": database.MIGRATION_ID,
        "is_human_confirmed_default": 0,
        "legacy_unverified_rows": 1,
        "unsafe_confirmed_rows": 0,
    }
    order = database.get_order_by_id(1, db_path)
    assert order["approved_items"][0]["product_id"] == "CL010"
    assert order["approved_items"][0]["is_human_confirmed"] == 0
    assert order["approved_items"][0]["provenance_verified"] == 0
    with pytest.raises(ValueError, match="lacks verified human provenance"):
        database.record_export(1, db_path)


def test_fresh_database_uses_fail_closed_snapshot_default(tmp_path):
    db_path = tmp_path / "fresh.db"
    database.init_db(db_path)
    with database.get_connection(db_path) as conn:
        column = next(
            row for row in conn.execute("PRAGMA table_info(approved_order_items)")
            if row["name"] == "is_human_confirmed"
        )
    assert str(column["dflt_value"]).strip("()'\"") == "0"
