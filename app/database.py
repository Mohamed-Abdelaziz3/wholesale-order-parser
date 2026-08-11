"""Fail-closed SQLite persistence for advisory matching and human approval."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from os import PathLike
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.parse import quote

from .catalog_backups import (
    DEFAULT_RETENTION as CATALOG_BACKUP_RETENTION,
)
from .catalog_backups import (
    CatalogBackupError,
    CatalogBackupIntegrityError,
    CatalogBackupMissingError,
    CatalogBackupUnavailableError,
    read_snapshot,
    snapshot_path,
    write_snapshot,
)
from .catalog_backups import (
    canonical_rows as canonical_catalog_rows,
)
from .catalog_backups import (
    content_sha256 as catalog_content_sha256,
)
from .money import (
    ZERO,
    from_storage,
    legacy_to_storage,
    line_total,
    money_sum,
    parse_money,
    to_storage,
)
from .runtime_config import load_runtime_environment
from .units import (
    UNIT_CHECK_EQUIVALENT,
    UNIT_CHECK_HUMAN_OVERRIDE,
    UNIT_CHECK_LEGACY_APPROVED,
    UNIT_CHECK_LEGACY_UNVERIFIED,
    UNIT_CHECK_MANUAL_CATALOG,
    UNIT_CHECK_PENDING,
    classify_unit_check,
    unit_check_is_approval_ready,
    unit_check_requires_resolution,
    units_equivalent,
)

# Never search the repository working directory for a .env file.  This module
# is imported independently by maintenance commands/tests as well as the app
# factory, so it enforces the same explicit machine-local configuration rule.
load_runtime_environment()

DB_PATH = os.getenv("ORDERS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"
)
DatabasePath = Union[str, PathLike[str]]
MODEL_STATUSES = {"needs_review", "unresolved", "ambiguous"}
# ``CANCELLED`` is deliberately distinct from ``NOT_FOUND``.  The latter says
# the merchant could not map a requested product to the catalogue; the former
# says that a real requested line was withdrawn by the customer.  Both are
# final human decisions, but only SELECT lines may enter a snapshot.
HUMAN_DECISIONS = {"SELECT", "NOT_FOUND", "CANCELLED"}
MIGRATION_ID = "2026_07_27_human_approval_provenance_v2"
ORDER_FLEXIBILITY_MIGRATION_ID = "2026_08_01_order_flexibility_v1"
PROCESS_IDEMPOTENCY_MIGRATION_ID = "2026_08_09_process_request_idempotency_v1"
MONEY_MIGRATION_ID = "2026_08_09_decimal_text_money_v1"
UNIT_SAFETY_MIGRATION_ID = "2026_08_09_unit_mismatch_fail_closed_v1"
CATALOG_BACKUP_AVAILABILITY_MIGRATION_ID = "2026_08_11_catalog_backup_availability_v1"
PROCESS_REQUEST_LEASE_SECONDS = 90

CATALOG_BACKUP_AVAILABLE = "AVAILABLE"
CATALOG_BACKUP_MISSING = "MISSING"
CATALOG_BACKUP_CORRUPT = "CORRUPT"

FINAL_ITEM_STATUSES = {"human_selected", "not_found_confirmed", "cancelled_by_human"}
CUSTOMER_FIELD_LIMITS = {
    "Customer name": 200,
    "Customer phone": 80,
    "Customer address": 500,
}


def _read_only_uri(path: str) -> str:
    """Build a read-only SQLite URI that survives special characters in a path.

    Without percent-encoding, a '#' or '?' anywhere in the path is parsed by
    SQLite as a fragment or query separator: it silently opens a different,
    empty database. Every caller here is a safety check, so a phantom database
    means the check passes for the wrong file — the legacy-schema guard is
    bypassed and the migration preflight reports zero rows to migrate.
    """
    absolute = os.path.abspath(path).replace(os.sep, "/")
    return "file:" + quote(absolute, safe="/:") + "?mode=ro"


def resolve_db_path(db_path: Optional[DatabasePath] = None) -> str:
    """Resolve a database path at call time instead of capturing it on import.

    When no path is supplied, ``ORDERS_DB_PATH`` wins over the in-repo default so
    a container can point the database at a mounted volume without code changes.
    """
    if db_path is not None:
        return os.fspath(db_path)
    configured = (os.getenv("ORDERS_DB_PATH") or "").strip()
    return configured or DB_PATH


def get_connection(db_path: Optional[DatabasePath] = None) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    Note: ``sqlite3.Connection`` used as a context manager commits or rolls back
    the transaction but does **not** close the connection. Prefer :func:`session`
    for anything on a request path; this function remains for callers that
    manage the lifetime themselves.
    """
    conn = sqlite3.connect(resolve_db_path(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session(
    db_path: Optional[DatabasePath] = None,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit or roll back, and always close it.

    ``immediate=True`` acquires the write lock up front with ``BEGIN IMMEDIATE``
    so a read-then-write sequence (for example: check order status, then insert
    an approval) cannot interleave with a competing writer.
    """
    conn = get_connection(db_path)
    try:
        if immediate:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:  # pragma: no cover - rollback on a dead handle
                pass
            raise
    finally:
        conn.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> Dict[str, sqlite3.Row]:
    if not _table_exists(conn, table):
        return {}
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _create_safe_approved_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            original_item_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT NOT NULL,
            requested_unit TEXT,
            catalog_unit TEXT,
            unit_check_status TEXT NOT NULL DEFAULT 'LEGACY_APPROVED',
            unit_resolution TEXT,
            unit_resolution_note TEXT,
            unit_resolution_actor TEXT,
            unit_resolved_at TEXT,
            unit_resolution_action_id TEXT,
            price TEXT NOT NULL,
            line_total TEXT NOT NULL DEFAULT '0.00',
            confidence REAL NOT NULL,
            model_recommendation_id TEXT,
            model_decision TEXT,
            human_decision TEXT,
            human_selected_sku TEXT,
            human_actor TEXT,
            human_confirmed_at TEXT,
            review_action_id TEXT,
            approval_actor TEXT,
            approval_action_id TEXT,
            provenance_verified INTEGER NOT NULL DEFAULT 0,
            is_manually_corrected INTEGER NOT NULL DEFAULT 0,
            is_human_confirmed INTEGER NOT NULL DEFAULT 0,
            catalog_price TEXT,
            price_override TEXT,
            price_overridden INTEGER NOT NULL DEFAULT 0,
            price_override_actor TEXT,
            price_overridden_at TEXT,
            price_override_action_id TEXT,
            approved_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY (original_item_id) REFERENCES order_items(id),
            UNIQUE(order_id, original_item_id)
        )
        """
    )


def _event_payload(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        value = json.loads(row["details"] or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _valid_legacy_provenance(
    conn: sqlite3.Connection,
) -> tuple[Dict[tuple[int, int, str], sqlite3.Row], Dict[int, sqlite3.Row]]:
    selections: Dict[tuple[int, int, str], sqlite3.Row] = {}
    approvals: Dict[int, sqlite3.Row] = {}
    audit_columns = _columns(conn, "audit_events")
    if not {"actor", "action_id"}.issubset(audit_columns):
        return selections, approvals

    rows = conn.execute(
        """
        SELECT * FROM audit_events
        WHERE actor IS NOT NULL AND TRIM(actor) <> ''
          AND action_id IS NOT NULL AND TRIM(action_id) <> ''
          AND event_type IN ('human_selected_final_sku', 'order_approved')
        ORDER BY id
        """
    )
    for row in rows:
        payload = _event_payload(row)
        if row["event_type"] == "human_selected_final_sku":
            try:
                key = (
                    int(row["order_id"]),
                    int(payload["item_id"]),
                    str(payload["human_selected_final_sku"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            selections[key] = row
        else:
            approvals[int(row["order_id"])] = row
    return selections, approvals


def preflight_human_review_migration(
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Report legacy snapshot rows that cannot prove human approval without mutating."""
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(_read_only_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "approved_order_items"):
            return {
                "database_path": path,
                "approved_snapshot_rows": 0,
                "currently_marked_human_confirmed": 0,
                "rows_with_valid_provenance": 0,
                "rows_to_mark_legacy_unverified": 0,
            }
        selections, approvals = _valid_legacy_provenance(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM approved_order_items")]
        valid = 0
        for row in rows:
            key = (row["order_id"], row["original_item_id"], row["product_id"])
            if key in selections and row["order_id"] in approvals:
                valid += 1
        confirmed = sum(1 for row in rows if int(row.get("is_human_confirmed") or 0) == 1)
        return {
            "database_path": path,
            "approved_snapshot_rows": len(rows),
            "currently_marked_human_confirmed": confirmed,
            "rows_with_valid_provenance": valid,
            "rows_to_mark_legacy_unverified": len(rows) - valid,
        }
    finally:
        conn.close()


def _migrate_approved_snapshots(conn: sqlite3.Connection) -> None:
    """Transactionally rebuild the evidence table with a fail-closed default."""
    columns = _columns(conn, "approved_order_items")
    human_column = columns.get("is_human_confirmed")
    default = str(human_column["dflt_value"] if human_column else "").strip("()'\"")
    required = {
        "model_recommendation_id",
        "model_decision",
        "human_decision",
        "human_selected_sku",
        "human_actor",
        "human_confirmed_at",
        "review_action_id",
        "approval_actor",
        "approval_action_id",
        "provenance_verified",
    }
    if default == "0" and required.issubset(columns):
        return

    selections, approvals = _valid_legacy_provenance(conn)
    legacy_rows = [dict(row) for row in conn.execute("SELECT * FROM approved_order_items")]
    item_rows = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM order_items")
    }

    conn.execute("SAVEPOINT approved_snapshot_provenance_migration")
    try:
        conn.execute("DROP TABLE IF EXISTS approved_order_items__safe")
        _create_safe_approved_table(conn, "approved_order_items__safe")
        for row in legacy_rows:
            item = item_rows.get(row["original_item_id"], {})
            key = (row["order_id"], row["original_item_id"], row["product_id"])
            selection = selections.get(key)
            approval = approvals.get(row["order_id"])
            verified = selection is not None and approval is not None
            conn.execute(
                """
                INSERT INTO approved_order_items__safe (
                    id, order_id, original_item_id, product_id, product_name,
                    quantity, unit, price, confidence, model_recommendation_id,
                    model_decision, human_decision, human_selected_sku, human_actor,
                    human_confirmed_at, review_action_id, approval_actor,
                    approval_action_id, provenance_verified, is_manually_corrected,
                    is_human_confirmed, catalog_price, price_override,
                    price_overridden, price_override_actor, price_overridden_at,
                    price_override_action_id, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["order_id"],
                    row["original_item_id"],
                    row["product_id"],
                    row["product_name"],
                    row["quantity"],
                    row["unit"],
                    row["price"],
                    row["confidence"],
                    item.get("recommendation_product_id"),
                    item.get("recommendation_decision"),
                    "SELECT" if verified else None,
                    row["product_id"] if verified else None,
                    selection["actor"] if verified else None,
                    selection["timestamp"] if verified else None,
                    selection["action_id"] if verified else None,
                    approval["actor"] if verified else None,
                    approval["action_id"] if verified else None,
                    1 if verified else 0,
                    int(row.get("is_manually_corrected") or 0),
                    1 if verified else 0,
                    row.get("catalog_price", row["price"]),
                    row.get("price_override"),
                    int(row.get("price_overridden") or 0),
                    row.get("price_override_actor"),
                    row.get("price_overridden_at"),
                    row.get("price_override_action_id"),
                    row["approved_at"],
                ),
            )
        conn.execute("DROP TABLE approved_order_items")
        conn.execute("ALTER TABLE approved_order_items__safe RENAME TO approved_order_items")
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
            (MIGRATION_ID, utc_now_iso()),
        )
        conn.execute("RELEASE SAVEPOINT approved_snapshot_provenance_migration")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT approved_snapshot_provenance_migration")
        conn.execute("RELEASE SAVEPOINT approved_snapshot_provenance_migration")
        raise


def verify_human_review_migration(
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Return post-migration safety checks."""
    with session(db_path) as conn:
        columns = _columns(conn, "approved_order_items")
        default = str(columns["is_human_confirmed"]["dflt_value"]).strip("()'\"")
        unverified = conn.execute(
            "SELECT COUNT(*) FROM approved_order_items WHERE provenance_verified=0 OR is_human_confirmed=0"
        ).fetchone()[0]
        unsafe = conn.execute(
            """
            SELECT COUNT(*) FROM approved_order_items
            WHERE is_human_confirmed=1 AND (
                provenance_verified<>1 OR human_actor IS NULL OR review_action_id IS NULL
                OR approval_actor IS NULL OR approval_action_id IS NULL
            )
            """
        ).fetchone()[0]
        return {
            "migration_id": MIGRATION_ID,
            "is_human_confirmed_default": int(default),
            "legacy_unverified_rows": unverified,
            "unsafe_confirmed_rows": unsafe,
        }


def _unsafe_snapshot_schema_exists(db_path: DatabasePath) -> bool:
    path = resolve_db_path(db_path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    conn = sqlite3.connect(_read_only_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = _columns(conn, "approved_order_items")
        if not columns:
            return False
        human_column = columns.get("is_human_confirmed")
        default = str(human_column["dflt_value"] if human_column else "").strip("()'\"")
        return default != "0" or "provenance_verified" not in columns
    finally:
        conn.close()


def initialize_runtime_database(db_path: DatabasePath) -> None:
    """Initialize safely, refusing to rewrite a legacy runtime database implicitly."""
    if _unsafe_snapshot_schema_exists(db_path):
        raise RuntimeError(
            "Human-review schema migration is required. Run the read-only preflight, "
            "back up the database, then invoke apply_human_review_migration explicitly."
        )
    init_db(db_path)


def apply_human_review_migration(db_path: DatabasePath) -> Dict[str, Any]:
    """Explicit operator entry point for the forward migration."""
    init_db(db_path)
    return verify_human_review_migration(db_path)


# ---------------------------------------------------------------------------
# Decimal-text money migration
#
# SQLite cannot alter a column affinity in place.  These tables are therefore
# rebuilt as one verified transaction rather than carrying a second set of
# ``*_minor`` / legacy columns forever.  Existing REAL values are copied as the
# exact human-visible Decimal spelling returned by SQLite; they are never
# rounded during migration.  New writes use ``money.to_storage`` (two places).
# ---------------------------------------------------------------------------

_MONEY_COLUMNS = {
    "catalog_products": {"price"},
    "orders": {"discount"},
    "order_items": {
        "matched_price",
        "recommendation_price",
        "catalog_price",
        "price_override",
    },
    "approved_order_items": {"price", "catalog_price", "price_override", "line_total"},
    "approved_order_financials": {"subtotal", "discount", "grand_total"},
}


def _money_schema_needs_migration(conn: sqlite3.Connection) -> bool:
    for table, fields in _MONEY_COLUMNS.items():
        columns = _columns(conn, table)
        if not columns:
            continue
        for field in fields:
            column = columns.get(field)
            if column is None or str(column["type"] or "").upper() != "TEXT":
                return True
    return False


def preflight_money_migration(
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Report legacy money scale/schema without changing the database."""
    path = resolve_db_path(db_path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {
            "database_path": path,
            "needs_migration": False,
            "legacy_values_over_two_places": [],
        }
    conn = sqlite3.connect(_read_only_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    findings: list[Dict[str, Any]] = []
    try:
        needs_migration = _money_schema_needs_migration(conn)
        for table, fields in _MONEY_COLUMNS.items():
            columns = _columns(conn, table)
            if not columns:
                continue
            for field in fields:
                if field not in columns:
                    continue
                for row in conn.execute(
                    f"SELECT rowid AS _rowid, {field} FROM {table} WHERE {field} IS NOT NULL"  # noqa: S608 - static migration schema names
                ):
                    try:
                        amount = from_storage(row[field], f"{table}.{field}")
                    except ValueError:
                        findings.append(
                            {
                                "table": table,
                                "column": field,
                                "rowid": row["_rowid"],
                                "value": str(row[field]),
                                "issue": "invalid",
                            }
                        )
                        continue
                    if amount.as_tuple().exponent < -2:
                        findings.append(
                            {
                                "table": table,
                                "column": field,
                                "rowid": row["_rowid"],
                                "value": legacy_to_storage(amount),
                                "issue": "more_than_two_places",
                            }
                        )
        return {
            "database_path": path,
            "needs_migration": needs_migration,
            "legacy_values_over_two_places": findings,
        }
    finally:
        conn.close()


def _create_money_orders_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'needs_review',
            export_count INTEGER NOT NULL DEFAULT 0,
            last_exported_at TEXT,
            processing_time_ms REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            customer_name TEXT,
            customer_phone TEXT,
            customer_address TEXT,
            discount TEXT NOT NULL DEFAULT '0.00'
        )
        """
    )


def _create_money_order_items_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            extracted_product TEXT NOT NULL,
            extracted_quantity REAL NOT NULL,
            extracted_unit TEXT NOT NULL,
            requested_unit TEXT,
            final_quantity REAL,
            final_unit TEXT,
            unit_check_status TEXT NOT NULL DEFAULT 'PENDING_CATALOG_SELECTION',
            unit_resolution TEXT,
            unit_resolution_note TEXT,
            unit_resolution_actor TEXT,
            unit_resolved_at TEXT,
            unit_resolution_action_id TEXT,
            matched_product_id TEXT,
            matched_product_name TEXT,
            matched_unit TEXT,
            matched_price TEXT,
            recommendation_product_id TEXT,
            recommendation_product_name TEXT,
            recommendation_unit TEXT,
            recommendation_price TEXT,
            recommendation_decision TEXT,
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'needs_review',
            is_manually_corrected INTEGER NOT NULL DEFAULT 0,
            is_human_confirmed INTEGER NOT NULL DEFAULT 0,
            human_decision TEXT,
            human_selected_sku TEXT,
            human_actor TEXT,
            human_confirmed_at TEXT,
            review_action_id TEXT,
            catalog_price TEXT,
            price_override TEXT,
            price_overridden INTEGER NOT NULL DEFAULT 0,
            price_override_actor TEXT,
            price_overridden_at TEXT,
            price_override_action_id TEXT,
            candidates_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
        """
    )


def _create_money_catalog_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            product_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '0.00',
            sort_order INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )


def _create_money_financials_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            order_id INTEGER PRIMARY KEY,
            customer_name TEXT,
            customer_phone TEXT,
            customer_address TEXT,
            subtotal TEXT NOT NULL,
            discount TEXT NOT NULL,
            grand_total TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
        """
    )


def _insert_row(cursor: sqlite3.Cursor, table: str, row: Dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    cursor.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608 - table/columns are migration-internal
        [row[column] for column in columns],
    )


def _legacy_money_value(row: Dict[str, Any], field: str, *, default: Optional[str] = None) -> Optional[str]:
    value = row.get(field)
    if value is None:
        return default
    return legacy_to_storage(value, field)


def _apply_money_schema_migration(db_path: Optional[DatabasePath] = None) -> None:
    """Replace legacy REAL commercial columns with Decimal-text tables.

    This deliberately runs outside :func:`session`: SQLite requires
    ``foreign_keys`` to be disabled before the transaction that replaces parent
    tables.  The transaction is still ``BEGIN IMMEDIATE`` and ends with
    ``foreign_key_check`` before commit.
    """
    path = resolve_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        if not _money_schema_needs_migration(conn):
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (MONEY_MIGRATION_ID, utc_now_iso()),
            )
            conn.commit()
            return

        conn.execute("BEGIN IMMEDIATE")
        source = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608 - table comes only from _MONEY_COLUMNS
            for table in _MONEY_COLUMNS
        }
        cursor = conn.cursor()
        for table in (
            "orders__money_new",
            "order_items__money_new",
            "catalog_products__money_new",
            "approved_order_items__money_new",
            "approved_order_financials__money_new",
        ):
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
        _create_money_orders_table(conn, "orders__money_new")
        _create_money_order_items_table(conn, "order_items__money_new")
        _create_money_catalog_table(conn, "catalog_products__money_new")
        _create_safe_approved_table(conn, "approved_order_items__money_new")
        _create_money_financials_table(conn, "approved_order_financials__money_new")

        for original in source["orders"]:
            row = dict(original)
            row["discount"] = _legacy_money_value(row, "discount", default="0.00")
            _insert_row(cursor, "orders__money_new", row)
        for original in source["order_items"]:
            row = dict(original)
            for field in (
                "matched_price",
                "recommendation_price",
                "catalog_price",
                "price_override",
            ):
                row[field] = _legacy_money_value(row, field)
            _insert_row(cursor, "order_items__money_new", row)
        for original in source["catalog_products"]:
            row = dict(original)
            row["price"] = _legacy_money_value(row, "price", default="0.00")
            _insert_row(cursor, "catalog_products__money_new", row)
        for original in source["approved_order_items"]:
            row = dict(original)
            for field in ("price", "catalog_price", "price_override"):
                row[field] = _legacy_money_value(row, field)
            # ``line_total`` did not exist before this migration.  It is a
            # derived frozen field and does not modify the legacy unit price or
            # quantity evidence.
            row["line_total"] = to_storage(line_total(row["price"], row["quantity"]))
            _insert_row(cursor, "approved_order_items__money_new", row)
        for original in source["approved_order_financials"]:
            row = dict(original)
            for field in ("subtotal", "discount", "grand_total"):
                row[field] = _legacy_money_value(row, field, default="0.00")
            _insert_row(cursor, "approved_order_financials__money_new", row)

        # Child tables retain foreign keys by name.  Recreating the same names
        # under a single transaction preserves their targets and all IDs.
        for table in (
            "approved_order_items",
            "approved_order_financials",
            "order_items",
            "orders",
            "catalog_products",
        ):
            cursor.execute(f"DROP TABLE {table}")
        for new, live in (
            ("orders__money_new", "orders"),
            ("order_items__money_new", "order_items"),
            ("catalog_products__money_new", "catalog_products"),
            ("approved_order_items__money_new", "approved_order_items"),
            ("approved_order_financials__money_new", "approved_order_financials"),
        ):
            cursor.execute(f"ALTER TABLE {new} RENAME TO {live}")
        problems = list(cursor.execute("PRAGMA foreign_key_check"))
        if problems:
            raise RuntimeError(f"Money migration foreign-key check failed: {problems[:3]}")
        cursor.execute(
            "INSERT OR REPLACE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
            (MONEY_MIGRATION_ID, utc_now_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            pass
        conn.close()


def init_db(db_path: Optional[DatabasePath] = None) -> None:
    """Initialize fresh schema and apply the forward provenance migration."""
    with session(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'needs_review',
                export_count INTEGER NOT NULL DEFAULT 0,
                last_exported_at TEXT,
                processing_time_ms REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_at TEXT,
                customer_name TEXT,
                customer_phone TEXT,
                customer_address TEXT,
                discount TEXT NOT NULL DEFAULT '0.00'
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                extracted_product TEXT NOT NULL,
                extracted_quantity REAL NOT NULL,
                extracted_unit TEXT NOT NULL,
                requested_unit TEXT,
                final_quantity REAL,
                final_unit TEXT,
                unit_check_status TEXT NOT NULL DEFAULT 'PENDING_CATALOG_SELECTION',
                unit_resolution TEXT,
                unit_resolution_note TEXT,
                unit_resolution_actor TEXT,
                unit_resolved_at TEXT,
                unit_resolution_action_id TEXT,
                matched_product_id TEXT,
                matched_product_name TEXT,
                matched_unit TEXT,
                matched_price TEXT,
                recommendation_product_id TEXT,
                recommendation_product_name TEXT,
                recommendation_unit TEXT,
                recommendation_price TEXT,
                recommendation_decision TEXT,
                confidence REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'needs_review',
                is_manually_corrected INTEGER NOT NULL DEFAULT 0,
                is_human_confirmed INTEGER NOT NULL DEFAULT 0,
                human_decision TEXT,
                human_selected_sku TEXT,
                human_actor TEXT,
                human_confirmed_at TEXT,
                review_action_id TEXT,
                catalog_price TEXT,
                price_override TEXT,
                price_overridden INTEGER NOT NULL DEFAULT 0,
                price_override_actor TEXT,
                price_overridden_at TEXT,
                price_override_action_id TEXT,
                candidates_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (item_id) REFERENCES order_items(id)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                actor TEXT,
                action_id TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS human_actions (
                action_id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL,
                item_id INTEGER,
                actor TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS process_requests (
                action_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('processing', 'succeeded', 'failed')),
                claim_token TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                order_id INTEGER UNIQUE,
                failure_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_products (
                product_id TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                price TEXT NOT NULL DEFAULT '0.00',
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_count INTEGER NOT NULL,
                source_name TEXT,
                content_sha256 TEXT NOT NULL,
                actor TEXT,
                operation TEXT NOT NULL DEFAULT 'replace',
                source_backup_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_backups (
                backup_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE,
                source_catalog_version INTEGER,
                product_count INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                actor TEXT,
                created_at TEXT NOT NULL,
                availability_status TEXT NOT NULL DEFAULT 'AVAILABLE'
                    CHECK (availability_status IN ('AVAILABLE', 'MISSING', 'CORRUPT')),
                availability_reason TEXT,
                availability_changed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS shop_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                shop_name TEXT NOT NULL DEFAULT '',
                shop_address TEXT NOT NULL DEFAULT '',
                shop_phone TEXT NOT NULL DEFAULT '',
                tax_id TEXT NOT NULL DEFAULT '',
                document_title TEXT NOT NULL DEFAULT '',
                footer_note TEXT NOT NULL DEFAULT '',
                logo_data_url TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS approved_order_financials (
                order_id INTEGER PRIMARY KEY,
                customer_name TEXT,
                customer_phone TEXT,
                customer_address TEXT,
                subtotal TEXT NOT NULL,
                discount TEXT NOT NULL,
                grand_total TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("SAVEPOINT human_review_forward_migration")
        try:
            _ensure_columns(
                conn,
                "order_items",
                {
                    # The raw customer-requested unit never changes after model
                    # ingestion. Human commercial choices are kept separately.
                    "requested_unit": "TEXT",
                    "final_quantity": "REAL",
                    "final_unit": "TEXT",
                    "unit_check_status": "TEXT NOT NULL DEFAULT 'PENDING_CATALOG_SELECTION'",
                    "unit_resolution": "TEXT",
                    "unit_resolution_note": "TEXT",
                    "unit_resolution_actor": "TEXT",
                    "unit_resolved_at": "TEXT",
                    "unit_resolution_action_id": "TEXT",
                    "recommendation_product_id": "TEXT",
                    "recommendation_product_name": "TEXT",
                    "recommendation_unit": "TEXT",
                    "recommendation_price": "TEXT",
                    "recommendation_decision": "TEXT",
                    "is_human_confirmed": "INTEGER NOT NULL DEFAULT 0",
                    "human_decision": "TEXT",
                    "human_selected_sku": "TEXT",
                    "human_actor": "TEXT",
                    "human_confirmed_at": "TEXT",
                    "review_action_id": "TEXT",
                    # matched_price is retained for compatibility.  The
                    # explicit fields below make the current catalogue price
                    # and a negotiated effective price independently auditable.
                    "catalog_price": "TEXT",
                    "price_override": "TEXT",
                    "price_overridden": "INTEGER NOT NULL DEFAULT 0",
                    "price_override_actor": "TEXT",
                    "price_overridden_at": "TEXT",
                    "price_override_action_id": "TEXT",
                },
            )
            # Releases before this safety barrier reused ``extracted_unit`` for
            # the final reviewer choice.  A NULL dedicated request-evidence
            # field therefore means we cannot prove what the customer asked
            # for, even when the remaining value happens to look compatible.
            # New ingestion stores an empty string for a genuinely missing
            # unit, so this affects only legacy rows and makes them visibly
            # fail closed until a reviewer records a fresh decision.
            conn.execute(
                """
                UPDATE order_items
                SET unit_check_status=?
                WHERE requested_unit IS NULL
                  AND unit_check_status=?
                """,
                (UNIT_CHECK_LEGACY_UNVERIFIED, UNIT_CHECK_PENDING),
            )
            _ensure_columns(
                conn,
                "orders",
                {
                    "customer_name": "TEXT",
                    "customer_phone": "TEXT",
                    "customer_address": "TEXT",
                    "discount": "TEXT NOT NULL DEFAULT '0.00'",
                },
            )
            _ensure_columns(
                conn,
                "audit_events",
                {"actor": "TEXT", "action_id": "TEXT"},
            )
            _ensure_columns(
                conn,
                "catalog_versions",
                {
                    "operation": "TEXT NOT NULL DEFAULT 'replace'",
                    "source_backup_id": "TEXT",
                },
            )
            _ensure_columns(
                conn,
                "catalog_backups",
                {
                    # Older pilot databases have only a file pointer. Preserve
                    # it as an initially unverified recovery candidate, then
                    # reconcile it before any future catalog replacement.
                    "availability_status": "TEXT NOT NULL DEFAULT 'AVAILABLE'",
                    "availability_reason": "TEXT",
                    "availability_changed_at": "TEXT",
                },
            )
            conn.execute(
                """
                UPDATE catalog_backups
                SET availability_status=?
                WHERE availability_status IS NULL OR TRIM(availability_status) = ''
                """,
                (CATALOG_BACKUP_AVAILABLE,),
            )
            # Shop identity arrived after the first deployments. Forward-migrate
            # rather than recreate, so an existing database keeps its orders.
            _ensure_columns(
                conn,
                "shop_settings",
                {
                    "shop_name": "TEXT NOT NULL DEFAULT ''",
                    "shop_address": "TEXT NOT NULL DEFAULT ''",
                    "shop_phone": "TEXT NOT NULL DEFAULT ''",
                    "tax_id": "TEXT NOT NULL DEFAULT ''",
                    "document_title": "TEXT NOT NULL DEFAULT ''",
                    "footer_note": "TEXT NOT NULL DEFAULT ''",
                    "logo_data_url": "TEXT NOT NULL DEFAULT ''",
                    "updated_by": "TEXT",
                },
            )
            if not _table_exists(conn, "approved_order_items"):
                _create_safe_approved_table(conn, "approved_order_items")
            else:
                _migrate_approved_snapshots(conn)
            _ensure_columns(
                conn,
                "approved_order_items",
                {
                    "requested_unit": "TEXT",
                    "catalog_unit": "TEXT",
                    "unit_check_status": "TEXT NOT NULL DEFAULT 'LEGACY_APPROVED'",
                    "unit_resolution": "TEXT",
                    "unit_resolution_note": "TEXT",
                    "unit_resolution_actor": "TEXT",
                    "unit_resolved_at": "TEXT",
                    "unit_resolution_action_id": "TEXT",
                    "catalog_price": "TEXT",
                    "price_override": "TEXT",
                    "price_overridden": "INTEGER NOT NULL DEFAULT 0",
                    "price_override_actor": "TEXT",
                    "price_overridden_at": "TEXT",
                    "price_override_action_id": "TEXT",
                    "line_total": "TEXT NOT NULL DEFAULT '0.00'",
                },
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (ORDER_FLEXIBILITY_MIGRATION_ID, utc_now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (PROCESS_IDEMPOTENCY_MIGRATION_ID, utc_now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (UNIT_SAFETY_MIGRATION_ID, utc_now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (CATALOG_BACKUP_AVAILABILITY_MIGRATION_ID, utc_now_iso()),
            )
            conn.execute("RELEASE SAVEPOINT human_review_forward_migration")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT human_review_forward_migration")
            conn.execute("RELEASE SAVEPOINT human_review_forward_migration")
            raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_events(order_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_process_requests_state_lease "
            "ON process_requests(state, lease_expires_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_name ON orders(customer_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders(customer_phone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalog_sort ON catalog_products(sort_order, product_id)"
        )
        conn.commit()
    _apply_money_schema_migration(db_path)
    # Rebuilding SQLite tables removes their indexes. Reassert the small set of
    # request-path indexes after the money migration (a no-op for fresh DBs).
    with session(db_path) as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_name ON orders(customer_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders(customer_phone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalog_sort ON catalog_products(sort_order, product_id)"
        )


# ---------------------------------------------------------------------------
# Shop identity
#
# One row, one deployment. This build is single-tenant by design: the merchant
# who runs it is the only merchant it serves, so there is no tenant key here and
# adding one would be a much larger change than putting a name on a document.
# ---------------------------------------------------------------------------

DEFAULT_DOCUMENT_TITLE = "إذن صرف"

SHOP_SETTINGS_FIELDS = (
    "shop_name",
    "shop_address",
    "shop_phone",
    "tax_id",
    "document_title",
    "footer_note",
    "logo_data_url",
)


def default_shop_settings() -> Dict[str, Any]:
    """Settings for a deployment whose merchant has not filled the panel in yet."""
    return {
        "shop_name": "",
        "shop_address": "",
        "shop_phone": "",
        "tax_id": "",
        "document_title": DEFAULT_DOCUMENT_TITLE,
        "footer_note": "",
        "logo_data_url": "",
        "updated_at": None,
        "updated_by": None,
    }


def get_shop_settings(db_path: Optional[DatabasePath] = None) -> Dict[str, Any]:
    """Read the single settings row, falling back to defaults when unset."""
    settings = default_shop_settings()
    with session(db_path) as conn:
        if not _table_exists(conn, "shop_settings"):
            return settings
        row = conn.execute("SELECT * FROM shop_settings WHERE id=1").fetchone()
    if not row:
        return settings
    stored = dict(row)
    for key in SHOP_SETTINGS_FIELDS:
        settings[key] = stored.get(key) or ""
    settings["document_title"] = settings["document_title"] or DEFAULT_DOCUMENT_TITLE
    settings["updated_at"] = stored.get("updated_at")
    settings["updated_by"] = stored.get("updated_by")
    return settings


def save_shop_settings(
    values: Dict[str, Any],
    *,
    actor: str,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Upsert the single settings row. ``shop_name`` and ``document_title`` are required."""
    actor = _require_text(actor, "Settings actor")
    record = {key: str(values.get(key) or "").strip() for key in SHOP_SETTINGS_FIELDS}
    record["shop_name"] = _require_text(record["shop_name"], "Shop name")
    record["document_title"] = _require_text(record["document_title"], "Document title")
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO shop_settings (
                id, shop_name, shop_address, shop_phone, tax_id,
                document_title, footer_note, logo_data_url, updated_at, updated_by
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                shop_name=excluded.shop_name,
                shop_address=excluded.shop_address,
                shop_phone=excluded.shop_phone,
                tax_id=excluded.tax_id,
                document_title=excluded.document_title,
                footer_note=excluded.footer_note,
                logo_data_url=excluded.logo_data_url,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
            """,
            (
                record["shop_name"],
                record["shop_address"],
                record["shop_phone"],
                record["tax_id"],
                record["document_title"],
                record["footer_note"],
                record["logo_data_url"],
                now,
                actor,
            ),
        )
        conn.commit()
    return get_shop_settings(db_path)


# ---------------------------------------------------------------------------
# Catalog storage
#
# The catalog lives in the database rather than only on disk so an operator can
# replace it at runtime without a redeploy. Approved snapshots copy product name
# and price at approval time, so replacing the catalog can never rewrite history.
# ---------------------------------------------------------------------------


def catalog_is_empty(db_path: Optional[DatabasePath] = None) -> bool:
    with session(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM catalog_products").fetchone()[0] == 0


def load_catalog_rows(db_path: Optional[DatabasePath] = None) -> List[Dict[str, Any]]:
    """Return catalog rows in their stored order."""
    with session(db_path) as conn:
        return [
            {
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "aliases": [a for a in (row["aliases"] or "").split("|") if a],
                "unit": row["unit"],
                "price": row["price"],
            }
            for row in conn.execute(
                "SELECT * FROM catalog_products ORDER BY sort_order, product_id"
            )
        ]


def _prepare_catalog_products(
    products: List[Dict[str, Any]],
    *,
    allow_legacy_prices: bool = False,
) -> list[Dict[str, Any]]:
    """Validate catalog rows before acquiring the destructive replacement lock.

    Uploaded catalog values are new commercial input and must meet the current
    two-decimal invariant. A validated recovery snapshot is different: it may
    contain a pre-pilot legacy price with more than two places, which must be
    restored exactly rather than silently rounded or rejected as new input.
    """
    if not products:
        raise ValueError("Cannot install an empty catalog")
    seen: set[str] = set()
    prepared: list[Dict[str, Any]] = []
    for index, source in enumerate(products):
        record = dict(source)
        product_id = _require_text(record.get("product_id"), "Catalog SKU")
        product_name = _require_text(record.get("product_name"), "Catalog product name")
        unit = _require_text(record.get("unit"), "Catalog unit")
        if product_id in seen:
            raise ValueError(f"Catalog contains duplicate SKU '{product_id}'")
        seen.add(product_id)
        aliases_source = record.get("aliases") or []
        aliases = (
            [part for part in str(aliases_source).split("|") if part]
            if isinstance(aliases_source, str)
            else [str(part).strip() for part in aliases_source if str(part).strip()]
        )
        price = (
            legacy_to_storage(record.get("price"), "Catalog price")
            if allow_legacy_prices
            else to_storage(parse_money(record.get("price"), "Catalog price"))
        )
        prepared.append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "aliases": aliases,
                "unit": unit,
                "price": price,
                "sort_order": int(record.get("sort_order", index) or 0),
            }
        )
    return canonical_catalog_rows(prepared)


def _catalog_rows_tx(cursor: sqlite3.Cursor) -> list[Dict[str, Any]]:
    return [
        {
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "aliases": [part for part in (row["aliases"] or "").split("|") if part],
            "unit": row["unit"],
            "price": row["price"],
            "sort_order": row["sort_order"],
        }
        for row in cursor.execute(
            "SELECT * FROM catalog_products ORDER BY sort_order, product_id"
        )
    ]


def _catalog_backup_failure_state(error: CatalogBackupError) -> tuple[str, str]:
    """Return the durable availability state for a failed snapshot inspection."""
    if isinstance(error, CatalogBackupMissingError):
        return CATALOG_BACKUP_MISSING, "Snapshot file is missing"
    if isinstance(error, CatalogBackupIntegrityError):
        return CATALOG_BACKUP_CORRUPT, "Snapshot integrity validation failed"
    if isinstance(error, CatalogBackupUnavailableError):
        raise RuntimeError("Catalog backup could not be inspected safely") from error
    # A permissions or I/O failure is neither proof that the snapshot is gone
    # nor proof that its contents changed. Do not permanently retire a valid
    # recovery point during a transient outage; fail this catalog operation so
    # the operator can retry once the storage fault is resolved.
    raise RuntimeError("Catalog backup could not be inspected safely") from error


def _validate_catalog_backup_record(
    metadata: Dict[str, Any],
    *,
    db_path: DatabasePath,
) -> tuple[Optional[Dict[str, Any]], Optional[tuple[str, str]]]:
    """Validate one tracked snapshot and classify only durable evidence.

    The database row remains the audit record even when its recovery file is no
    longer usable. Callers therefore receive a lifecycle state rather than an
    exception for historical snapshot failures.
    """
    try:
        manifest = read_snapshot(
            db_path=db_path,
            filename=metadata["filename"],
            backup_id=metadata["backup_id"],
        )
    except CatalogBackupError as exc:
        return None, _catalog_backup_failure_state(exc)
    if (
        manifest["product_count"] != metadata["product_count"]
        or manifest["content_sha256"] != metadata["content_sha256"]
    ):
        return None, (
            CATALOG_BACKUP_CORRUPT,
            "Snapshot metadata does not match validated content",
        )
    return manifest, None


def _mark_catalog_backup_unavailable_tx(
    cursor: sqlite3.Cursor,
    metadata: Dict[str, Any],
    *,
    status: str,
    reason: str,
    now: str,
) -> None:
    """Retire one unusable recovery point without erasing its audit evidence."""
    if status not in {
        CATALOG_BACKUP_MISSING,
        CATALOG_BACKUP_CORRUPT,
    }:
        raise ValueError("Catalog backup availability status is invalid")
    cursor.execute(
        """
        UPDATE catalog_backups
        SET availability_status=?, availability_reason=?, availability_changed_at=?
        WHERE backup_id=? AND availability_status=?
        """,
        (
            status,
            reason,
            now,
            metadata["backup_id"],
            CATALOG_BACKUP_AVAILABLE,
        ),
    )


def _reconcile_catalog_backup_availability_tx(
    cursor: sqlite3.Cursor,
    *,
    db_path: DatabasePath,
    now: str,
) -> None:
    """Persist immutable evidence for every newly discovered unavailable backup."""
    records = [
        dict(row)
        for row in cursor.execute(
            """
            SELECT * FROM catalog_backups
            WHERE availability_status=?
            ORDER BY created_at DESC, backup_id DESC
            """,
            (CATALOG_BACKUP_AVAILABLE,),
        )
    ]
    for metadata in records:
        _, unavailable = _validate_catalog_backup_record(metadata, db_path=db_path)
        if unavailable:
            status, reason = unavailable
            _mark_catalog_backup_unavailable_tx(
                cursor,
                metadata,
                status=status,
                reason=reason,
                now=now,
            )


def _reconcile_catalog_backup_availability(db_path: Optional[DatabasePath] = None) -> None:
    """Commit historical backup availability evidence before catalog mutation.

    Snapshot files are outside SQLite's transaction boundary. Committing this
    short reconciliation separately ensures a later catalog-write rollback can
    never resurrect an already-discovered orphan as an active restore point.
    """
    resolved_db_path = resolve_db_path(db_path)
    with session(resolved_db_path, immediate=True) as conn:
        _reconcile_catalog_backup_availability_tx(
            conn.cursor(),
            db_path=resolved_db_path,
            now=utc_now_iso(),
        )


def _insert_catalog_backup_metadata_tx(cursor: sqlite3.Cursor, snapshot: Dict[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO catalog_backups (
            backup_id, filename, source_catalog_version, product_count,
            content_sha256, actor, created_at, availability_status,
            availability_reason, availability_changed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot["backup_id"],
            snapshot["filename"],
            snapshot.get("source_catalog_version"),
            snapshot["product_count"],
            snapshot["content_sha256"],
            snapshot.get("actor"),
            snapshot["created_at"],
            CATALOG_BACKUP_AVAILABLE,
            None,
            snapshot["created_at"],
        ),
    )


def _prune_catalog_backups_tx(
    cursor: sqlite3.Cursor,
    *,
    db_path: Optional[DatabasePath],
    now: str,
) -> None:
    """Prune only currently verified recovery points.

    Missing and corrupted historical files become durable metadata states
    instead of errors that undo a safe replacement. They are excluded from
    restore and retention candidates but intentionally remain visible to
    operators as evidence of the lost recovery point. Transient I/O failures
    remain retryable and abort the current catalog operation safely.
    """
    resolved_db_path = resolve_db_path(db_path)
    _reconcile_catalog_backup_availability_tx(
        cursor,
        db_path=resolved_db_path,
        now=now,
    )
    while True:
        records = [
            dict(row)
            for row in cursor.execute(
                """
                SELECT * FROM catalog_backups
                WHERE availability_status=?
                ORDER BY created_at DESC, backup_id DESC
                """,
                (CATALOG_BACKUP_AVAILABLE,),
            )
        ]
        if len(records) <= CATALOG_BACKUP_RETENTION:
            return

        candidates = records[CATALOG_BACKUP_RETENTION:]
        changed_availability = False
        # Validate every target before deleting any file. A corrupted snapshot
        # is evidence to retain, never a file to remove merely to unblock
        # retention.
        for metadata in candidates:
            _, unavailable = _validate_catalog_backup_record(
                metadata,
                db_path=resolved_db_path,
            )
            if unavailable:
                status, reason = unavailable
                _mark_catalog_backup_unavailable_tx(
                    cursor,
                    metadata,
                    status=status,
                    reason=reason,
                    now=now,
                )
                changed_availability = True
        if changed_availability:
            continue

        for metadata in candidates:
            try:
                snapshot_path(
                    db_path=resolved_db_path,
                    filename=metadata["filename"],
                ).unlink()
            except FileNotFoundError:
                _mark_catalog_backup_unavailable_tx(
                    cursor,
                    metadata,
                    status=CATALOG_BACKUP_MISSING,
                    reason="Snapshot file is missing",
                    now=now,
                )
                changed_availability = True
            except CatalogBackupError as exc:
                status, reason = _catalog_backup_failure_state(exc)
                _mark_catalog_backup_unavailable_tx(
                    cursor,
                    metadata,
                    status=status,
                    reason=reason,
                    now=now,
                )
                changed_availability = True
            except OSError as exc:
                raise RuntimeError(
                    "Catalog backup retention cleanup failed; catalog replacement was aborted"
                ) from exc
            else:
                cursor.execute(
                    "DELETE FROM catalog_backups WHERE backup_id=?",
                    (metadata["backup_id"],),
                )
        if not changed_availability:
            return


def _replace_catalog_rows_tx(
    cursor: sqlite3.Cursor,
    products: list[Dict[str, Any]],
    *,
    source_name: Optional[str],
    actor: Optional[str],
    now: str,
    operation: str,
    source_backup_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace only catalog rows and append an attributable version record."""
    digest = catalog_content_sha256(products)
    cursor.execute("DELETE FROM catalog_products")
    cursor.executemany(
        """
        INSERT INTO catalog_products
            (product_id, product_name, aliases, unit, price, sort_order, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                product["product_id"],
                product["product_name"],
                "|".join(product.get("aliases") or []),
                product["unit"],
                product["price"],
                index,
                now,
            )
            for index, product in enumerate(products)
        ],
    )
    cursor.execute(
        """
        INSERT INTO catalog_versions
            (product_count, source_name, content_sha256, actor, operation, source_backup_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (len(products), source_name, digest, actor, operation, source_backup_id, now),
    )
    return {
        "version_id": int(cursor.lastrowid),
        "product_count": len(products),
        "content_sha256": digest,
        "installed_at": now,
        "source_name": source_name,
        "actor": actor,
        "operation": operation,
        "source_backup_id": source_backup_id,
    }


def _backup_current_catalog_tx(
    cursor: sqlite3.Cursor,
    *,
    db_path: Optional[DatabasePath],
    actor: Optional[str],
    now: str,
) -> Optional[Dict[str, Any]]:
    current = _catalog_rows_tx(cursor)
    if not current:
        return None
    version = cursor.execute("SELECT id FROM catalog_versions ORDER BY id DESC LIMIT 1").fetchone()
    try:
        snapshot = write_snapshot(
            current,
            db_path=resolve_db_path(db_path),
            source_catalog_version=int(version["id"]) if version else None,
            actor=actor,
            created_at=now,
        )
    except CatalogBackupError as exc:
        # The caller still holds the pre-replacement SQLite state. Raising here
        # aborts before catalog_products is touched.
        raise RuntimeError("Catalog backup failed; catalog replacement was aborted") from exc
    _insert_catalog_backup_metadata_tx(cursor, snapshot)
    _prune_catalog_backups_tx(cursor, db_path=db_path, now=now)
    return snapshot


def replace_catalog(
    products: List[Dict[str, Any]],
    db_path: Optional[DatabasePath] = None,
    *,
    source_name: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically replace the catalog after a durable pre-replacement backup."""
    normalised_products = _prepare_catalog_products(products)
    # Reconciliation commits separately because snapshot files are outside the
    # SQLite transaction. A later failed replacement must not make a known
    # missing/corrupt recovery point appear active again.
    _reconcile_catalog_backup_availability(db_path)
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        _backup_current_catalog_tx(cursor, db_path=db_path, actor=actor, now=now)
        return _replace_catalog_rows_tx(
            cursor,
            normalised_products,
            source_name=source_name,
            actor=actor,
            now=now,
            operation="replace",
        )


def list_catalog_backups(db_path: Optional[DatabasePath] = None) -> List[Dict[str, Any]]:
    """List durable recovery points without exposing arbitrary filesystem paths."""
    with session(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM catalog_backups ORDER BY created_at DESC, backup_id DESC"
            )
        ]


def restore_catalog_backup(
    backup_id: str,
    *,
    actor: str,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Restore one validated catalog-only snapshot, preserving all orders.

    Operators must stop the one-replica app before calling this maintenance
    function/CLI, then restart it so the live in-memory matcher reloads from
    the restored catalog. The function intentionally accepts a durable backup
    ID rather than a path supplied by an operator.
    """
    actor = _require_text(actor, "Restore actor")
    backup_id = _require_text(backup_id, "Catalog backup ID")
    _reconcile_catalog_backup_availability(db_path)
    now = utc_now_iso()
    restore_failure: Optional[str] = None
    restored: Optional[Dict[str, Any]] = None
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        metadata_row = cursor.execute(
            "SELECT * FROM catalog_backups WHERE backup_id=?", (backup_id,)
        ).fetchone()
        if not metadata_row:
            raise ValueError("Catalog backup was not found")
        metadata = dict(metadata_row)
        if metadata["availability_status"] != CATALOG_BACKUP_AVAILABLE:
            raise ValueError("Catalog backup is unavailable; restore was aborted")
        manifest, unavailable = _validate_catalog_backup_record(
            metadata,
            db_path=resolve_db_path(db_path),
        )
        if unavailable:
            status, reason = unavailable
            # Do not raise while this transaction is open: committing the
            # lifecycle change is what prevents a vanished file from being
            # rediscovered as an active recovery point forever.
            _mark_catalog_backup_unavailable_tx(
                cursor,
                metadata,
                status=status,
                reason=reason,
                now=now,
            )
            restore_failure = "Catalog backup validation failed; restore was aborted"
        else:
            assert manifest is not None
            # The current catalog is itself protected before it is replaced.
            # This makes restore reversible and follows the same retention policy.
            _backup_current_catalog_tx(cursor, db_path=db_path, actor=actor, now=now)
            products = _prepare_catalog_products(manifest["products"], allow_legacy_prices=True)
            restored = _replace_catalog_rows_tx(
                cursor,
                products,
                source_name=f"catalog-backup:{backup_id}",
                actor=actor,
                now=now,
                operation="restore",
                source_backup_id=backup_id,
            )
    if restore_failure:
        raise ValueError(restore_failure)
    assert restored is not None
    return restored


def current_catalog_version(db_path: Optional[DatabasePath] = None) -> Optional[Dict[str, Any]]:
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM catalog_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_orders(
    db_path: Optional[DatabasePath] = None,
    *,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Recent orders with review progress, optionally narrowed by customer.

    Cancelled lines remain persisted for audit purposes but are intentionally
    not counted as work still to review.  ``search`` is parameterised rather
    than interpolated so a phone number or customer name cannot change the
    history query.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    needle = (search or "").strip()
    params: list[Any] = []
    query = """
        SELECT
            o.id, o.original_text, o.status, o.created_at, o.approved_at,
            o.export_count, o.last_exported_at, o.customer_name,
            o.customer_phone, o.customer_address, o.discount,
            COUNT(CASE WHEN i.status <> 'cancelled_by_human' THEN 1 END) AS total_items,
            COALESCE(SUM(CASE
                WHEN i.status <> 'cancelled_by_human' THEN i.is_human_confirmed
                ELSE 0
            END), 0) AS confirmed_items
        FROM orders o
        LEFT JOIN order_items i ON i.order_id = o.id
    """
    if needle:
        query += " WHERE o.customer_name LIKE ? OR o.customer_phone LIKE ?"
        pattern = f"%{needle}%"
        params.extend((pattern, pattern))
    query += " GROUP BY o.id ORDER BY o.id DESC LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    with session(db_path) as conn:
        rows = conn.execute(query, params)
        return [dict(row) for row in rows]


def log_audit_event_tx(
    cursor: sqlite3.Cursor,
    order_id: int,
    event_type: str,
    details: Optional[str] = None,
    timestamp: Optional[str] = None,
    *,
    actor: Optional[str] = None,
    action_id: Optional[str] = None,
) -> None:
    cursor.execute(
        """
        INSERT INTO audit_events (order_id, event_type, details, actor, action_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (order_id, event_type, details, actor, action_id, timestamp or utc_now_iso()),
    )


def log_audit_event(
    order_id: int,
    event_type: str,
    details: Optional[str] = None,
    db_path: Optional[DatabasePath] = None,
) -> None:
    with session(db_path) as conn:
        log_audit_event_tx(conn.cursor(), order_id, event_type, details)
        conn.commit()


def _normalized_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return confidence if 0.0 <= confidence <= 1.0 else 0.0


class ProcessRequestConflict(ValueError):
    """A process idempotency key was reused for a different logical request."""


class ProcessRequestInProgress(RuntimeError):
    """The durable owner of an idempotent process request is still running."""


def _process_payload_sha256(message: str) -> str:
    """Hash the canonical process command without duplicating raw PII in a side table."""
    payload = _canonical_action_payload({"message": message})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lease_expired(value: Any, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        return True
    return expires <= now


def begin_process_request(
    action_id: str,
    *,
    actor: str,
    message: str,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Durably claim one logical process command before provider work begins.

    The reservation is intentionally separate from ``human_actions``: a process
    command has no order ID until the model result has been validated and saved.
    It follows the same fail-closed contract, binding one action ID to one actor
    and one canonical payload for the lifetime of the completed order.
    """
    action_id = str(action_id or "").strip()
    actor = str(actor or "").strip()
    if not action_id:
        raise ValueError("Action ID is required")
    if not actor:
        raise ValueError("Process actor is required")
    payload_sha256 = _process_payload_sha256(message)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=PROCESS_REQUEST_LEASE_SECONDS)).isoformat()
    claim_token = secrets.token_urlsafe(24)

    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        existing = cursor.execute(
            "SELECT * FROM process_requests WHERE action_id=?", (action_id,)
        ).fetchone()
        if not existing:
            cursor.execute(
                """
                INSERT INTO process_requests (
                    action_id, actor, payload_sha256, state, claim_token,
                    lease_expires_at, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, 'processing', ?, ?, 1, ?, ?)
                """,
                (action_id, actor, payload_sha256, claim_token, lease_expires_at, now, now),
            )
            return {
                "outcome": "claimed",
                "action_id": action_id,
                "claim_token": claim_token,
                "payload_sha256": payload_sha256,
                "attempt_count": 1,
            }

        row = dict(existing)
        if row["actor"] != actor or row["payload_sha256"] != payload_sha256:
            raise ProcessRequestConflict(
                f"Action ID '{action_id}' was already used for another command"
            )
        if row["state"] == "succeeded":
            if row.get("order_id") is None:
                raise ValueError("Completed process request has no order")
            return {"outcome": "succeeded", "order_id": int(row["order_id"])}

        reclaim = row["state"] == "failed" or _lease_expired(row.get("lease_expires_at"), now_dt)
        if reclaim:
            attempts = int(row.get("attempt_count") or 0) + 1
            cursor.execute(
                """
                UPDATE process_requests
                SET state='processing', claim_token=?, lease_expires_at=?,
                    attempt_count=?, failure_code=NULL, updated_at=?
                WHERE action_id=?
                """,
                (claim_token, lease_expires_at, attempts, now, action_id),
            )
            return {
                "outcome": "claimed",
                "action_id": action_id,
                "claim_token": claim_token,
                "payload_sha256": payload_sha256,
                "attempt_count": attempts,
            }
        return {
            "outcome": "in_progress",
            "action_id": action_id,
            "lease_expires_at": row.get("lease_expires_at"),
        }


def get_process_request(
    action_id: str,
    *,
    actor: str,
    message: str,
    db_path: Optional[DatabasePath] = None,
) -> Optional[Dict[str, Any]]:
    """Read a process request only when its full idempotency binding matches."""
    payload_sha256 = _process_payload_sha256(message)
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM process_requests WHERE action_id=?", (action_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["actor"] != actor or result["payload_sha256"] != payload_sha256:
        raise ProcessRequestConflict(
            f"Action ID '{action_id}' was already used for another command"
        )
    return result


def fail_process_request(
    action_id: str,
    *,
    actor: str,
    message: str,
    claim_token: str,
    failure_code: str,
    db_path: Optional[DatabasePath] = None,
) -> None:
    """Release only the current owner's failed attempt for an exact-key retry."""
    payload_sha256 = _process_payload_sha256(message)
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE process_requests
            SET state='failed', lease_expires_at=NULL, failure_code=?, updated_at=?
            WHERE action_id=? AND actor=? AND payload_sha256=?
              AND state='processing' AND claim_token=?
            """,
            (failure_code, now, action_id, actor, payload_sha256, claim_token),
        )


def _validate_model_ingestion(items_data: List[Dict[str, Any]]) -> None:
    forbidden_human_keys = {
        "human_decision",
        "human_selected_sku",
        "human_actor",
        "human_confirmed_at",
        "review_action_id",
    }
    for item in items_data:
        if item.get("matched_product"):
            raise ValueError("Model ingestion cannot persist a final matched product")
        if item.get("is_human_confirmed"):
            raise ValueError("Model ingestion cannot mark an item human-confirmed")
        if item.get("status", "needs_review") not in MODEL_STATUSES:
            raise ValueError("Model ingestion can persist review-required state only")
        if any(item.get(key) is not None for key in forbidden_human_keys):
            raise ValueError("Model ingestion cannot persist human approval provenance")


def _save_processed_order_tx(
    cursor: sqlite3.Cursor,
    *,
    original_text: str,
    items_data: List[Dict[str, Any]],
    unresolved_text: List[str],
    processing_time_ms: float,
) -> int:
    """Persist only model recommendations using an existing write transaction."""
    _validate_model_ingestion(items_data)
    now = utc_now_iso()
    cursor.execute(
        """
        INSERT INTO orders (original_text, status, processing_time_ms, created_at, updated_at)
        VALUES (?, 'needs_review', ?, ?, ?)
        """,
        (original_text, processing_time_ms, now, now),
    )
    order_id = int(cursor.lastrowid)
    for item in items_data:
        recommendation = item.get("recommendation_product") or {}
        confidence = _normalized_confidence(item.get("confidence"))
        requested_unit = str(item.get("extracted_unit") or "").strip()
        recommendation_unit = str(recommendation.get("unit") or "").strip()
        unit_check_status = (
            classify_unit_check(requested_unit, recommendation_unit)
            if recommendation.get("product_id")
            else UNIT_CHECK_PENDING
        )
        cursor.execute(
            """
            INSERT INTO order_items (
                order_id, raw_text, extracted_product, extracted_quantity, extracted_unit,
                requested_unit, unit_check_status,
                recommendation_product_id, recommendation_product_name, recommendation_unit,
                recommendation_price, recommendation_decision, confidence, status,
                candidates_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                item.get("raw_text", ""),
                item.get("extracted_product", ""),
                item.get("extracted_quantity", 1.0),
                requested_unit,
                requested_unit,
                unit_check_status,
                recommendation.get("product_id"),
                recommendation.get("product_name"),
                recommendation_unit or None,
                (
                    to_storage(parse_money(recommendation.get("price"), "Recommendation price"))
                    if recommendation.get("price") is not None
                    else None
                ),
                item.get("recommendation_decision") or "INVALID_OUTPUT",
                confidence,
                item.get("status", "needs_review"),
                json.dumps(item.get("candidates", []), ensure_ascii=False),
                now,
                now,
            ),
        )
        item_id = int(cursor.lastrowid)
        log_audit_event_tx(
            cursor,
            order_id,
            "model_recommendation_recorded",
            json.dumps(
                {
                    "item_id": item_id,
                    "decision": item.get("recommendation_decision") or "INVALID_OUTPUT",
                    "recommended_product_id": recommendation.get("product_id"),
                    "model_confidence": confidence,
                    "requires_human_confirmation": True,
                },
                ensure_ascii=False,
            ),
            now,
        )
    log_audit_event_tx(
        cursor,
        order_id,
        "analysis_completed",
        json.dumps(
            {
                "total_items": len(items_data),
                "status": "needs_review",
                "unresolved": unresolved_text,
            },
            ensure_ascii=False,
        ),
        now,
    )
    return order_id


def save_processed_order(
    original_text: str,
    items_data: List[Dict[str, Any]],
    unresolved_text: List[str],
    processing_time_ms: float,
    db_path: Optional[DatabasePath] = None,
) -> int:
    """Persist model recommendations only; human or approved state is rejected."""
    with session(db_path) as conn:
        return _save_processed_order_tx(
            conn.cursor(),
            original_text=original_text,
            items_data=items_data,
            unresolved_text=unresolved_text,
            processing_time_ms=processing_time_ms,
        )


def complete_process_request(
    action_id: str,
    *,
    actor: str,
    message: str,
    claim_token: str,
    items_data: List[Dict[str, Any]],
    unresolved_text: List[str],
    processing_time_ms: float,
    db_path: Optional[DatabasePath] = None,
) -> int:
    """Atomically persist a process result and its durable idempotency mapping."""
    payload_sha256 = _process_payload_sha256(message)
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT * FROM process_requests WHERE action_id=?", (action_id,)
        ).fetchone()
        if not row:
            raise ValueError("Process request reservation is missing")
        request_row = dict(row)
        if request_row["actor"] != actor or request_row["payload_sha256"] != payload_sha256:
            raise ProcessRequestConflict(
                f"Action ID '{action_id}' was already used for another command"
            )
        if request_row["state"] == "succeeded":
            if request_row.get("order_id") is None:
                raise ValueError("Completed process request has no order")
            return int(request_row["order_id"])
        if request_row["state"] != "processing" or request_row.get("claim_token") != claim_token:
            raise ProcessRequestInProgress("The original process request is still running")

        order_id = _save_processed_order_tx(
            cursor,
            original_text=message,
            items_data=items_data,
            unresolved_text=unresolved_text,
            processing_time_ms=processing_time_ms,
        )
        cursor.execute(
            """
            UPDATE process_requests
            SET state='succeeded', order_id=?, claim_token=NULL, lease_expires_at=NULL,
                failure_code=NULL, updated_at=?, completed_at=?
            WHERE action_id=?
            """,
            (order_id, now, now, action_id),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "process_request_completed",
            json.dumps(
                {
                    "request_payload_sha256": payload_sha256,
                    "attempt_count": request_row["attempt_count"],
                    "requires_human_confirmation": True,
                },
                ensure_ascii=False,
            ),
            now,
            actor=actor,
            action_id=action_id,
        )
        return order_id


def _snapshot_subtotal(snapshot: List[Dict[str, Any]]) -> str:
    """Calculate a money subtotal from the frozen effective unit prices."""
    totals = []
    for item in snapshot:
        frozen_total = item.get("line_total")
        totals.append(
            from_storage(frozen_total, "Snapshot line total")
            if frozen_total not in (None, "")
            else line_total(item.get("price"), item.get("quantity"))
        )
    return to_storage(money_sum(totals))


def _frozen_order_financials(
    cursor: sqlite3.Cursor,
    order: Dict[str, Any] | sqlite3.Row,
    snapshot: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Read financial/customer metadata captured alongside a new approval.

    Approved orders created before this feature deliberately have no additional
    row.  Their old snapshot remains untouched and gets a stable zero-discount
    fallback, rather than a backfill that could rewrite historical evidence.
    """
    order_id = order["id"]
    stored = cursor.execute(
        "SELECT * FROM approved_order_financials WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if stored:
        return dict(stored)
    subtotal = _snapshot_subtotal(snapshot)
    return {
        "order_id": order_id,
        "customer_name": order["customer_name"] or "",
        "customer_phone": order["customer_phone"] or "",
        "customer_address": order["customer_address"] or "",
        "subtotal": subtotal,
        "discount": "0.00",
        "grand_total": subtotal,
        "approved_at": order["approved_at"],
    }


def get_order_by_id(
    order_id: int,
    db_path: Optional[DatabasePath] = None,
) -> Optional[Dict[str, Any]]:
    with session(db_path) as conn:
        order_row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order_row:
            return None
        order = dict(order_row)
        items = []
        for row in conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)):
            item = dict(row)
            try:
                item["candidates"] = json.loads(item.get("candidates_json") or "[]")
            except json.JSONDecodeError:
                item["candidates"] = []
            item["is_manually_corrected"] = bool(item.get("is_manually_corrected"))
            item["is_human_confirmed"] = bool(item.get("is_human_confirmed"))
            items.append(item)
        order["items"] = items
        order["approved_items"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM approved_order_items WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        for item in items:
            item["price_overridden"] = bool(item.get("price_overridden"))
        financials = _frozen_order_financials(conn.cursor(), order, order["approved_items"])
        order["approved_financials"] = financials
        order["approved_subtotal"] = financials["subtotal"]
        order["approved_discount"] = financials["discount"]
        order["approved_grand_total"] = financials["grand_total"]
        # Read approved orders from their frozen metadata, never a mutable
        # working-order field.  The fallback above preserves legacy orders.
        if order["status"] == "approved":
            order["customer_name"] = financials["customer_name"]
            order["customer_phone"] = financials["customer_phone"]
            order["customer_address"] = financials["customer_address"]
            order["discount"] = financials["discount"]
        order["audit_events"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM audit_events WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        order["corrections"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM corrections WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        order["unresolved"] = unresolved_text_of(order["audit_events"])
        return order


def unresolved_text_of(audit_events: List[Dict[str, Any]]) -> List[str]:
    """Recover the extractor's unresolved fragments from the analysis event.

    The fragments were always persisted here and never read back, so a customer
    writing "ومتنساش الفكة" had that instruction silently dropped between the
    extractor and the operator's screen. This is the read side of that record.
    """
    fragments: List[str] = []
    for event in audit_events:
        if event.get("event_type") != "analysis_completed":
            continue
        try:
            details = json.loads(event.get("details") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(details, dict):
            continue
        for fragment in details.get("unresolved") or []:
            text = str(fragment).strip()
            if text and text not in fragments:
                fragments.append(text)
    return fragments


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _optional_bounded_text(value: Any, name: str) -> str:
    """Normalise optional customer text without silently truncating it."""
    text = str(value or "").strip()
    limit = CUSTOMER_FIELD_LIMITS[name]
    if len(text) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return text


def _finite_number(
    value: Any,
    name: str,
    *,
    minimum: Optional[float] = None,
) -> float:
    """Return a finite amount; SQLite accepts NaN-like values too readily."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}")
    return number


def _canonical_action_payload(payload: Dict[str, Any]) -> str:
    def normalise(value: Any) -> Any:
        if isinstance(value, Decimal):
            return to_storage(value)
        if isinstance(value, dict):
            return {str(key): normalise(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalise(item) for item in value]
        return value

    return json.dumps(normalise(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _money_payload_matches(
    payload_json: Any,
    field: str,
    expected: Decimal,
    *,
    allow_legacy: bool = False,
) -> bool:
    """Compare action evidence without changing its historical monetary value.

    New human mutations must satisfy the two-decimal input policy.  Frozen
    snapshots from before that policy can contain an auditable higher-scale
    amount, however, so export verification opts into the read-only legacy
    parser explicitly rather than making current commercial writes permissive.
    """
    try:
        payload = json.loads(str(payload_json))
        parser = from_storage if allow_legacy else parse_money
        return isinstance(payload, dict) and parser(payload.get(field), field) == expected
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _existing_action(
    cursor: sqlite3.Cursor,
    *,
    action_id: str,
    order_id: int,
    item_id: Optional[int],
    actor: str,
    action_type: str,
    payload: Dict[str, Any],
    allow_existing_item_id: bool = False,
) -> Optional[sqlite3.Row]:
    """Return an exact prior action or fail closed on action-id reuse.

    ``manual_item_added`` is the only command whose item id is not known until
    after the row is inserted.  It may opt into ``allow_existing_item_id``;
    every other command still requires an exact order/item/actor/payload match.
    """
    payload_json = _canonical_action_payload(payload)
    existing = cursor.execute(
        "SELECT * FROM human_actions WHERE action_id=?",
        (action_id,),
    ).fetchone()
    if not existing:
        return None
    same_item = allow_existing_item_id or existing["item_id"] == item_id
    same = (
        existing["order_id"] == order_id
        and same_item
        and existing["actor"] == actor
        and existing["action_type"] == action_type
        and existing["payload_json"] == payload_json
    )
    if not same:
        raise ValueError(f"Action ID '{action_id}' was already used for another command")
    return existing


def _insert_action(
    cursor: sqlite3.Cursor,
    *,
    action_id: str,
    order_id: int,
    item_id: Optional[int],
    actor: str,
    action_type: str,
    payload: Dict[str, Any],
    timestamp: str,
) -> bool:
    payload_json = _canonical_action_payload(payload)
    existing = _existing_action(
        cursor,
        action_id=action_id,
        order_id=order_id,
        item_id=item_id,
        actor=actor,
        action_type=action_type,
        payload=payload,
    )
    if existing:
        return False
    cursor.execute(
        """
        INSERT INTO human_actions
            (action_id, order_id, item_id, actor, action_type, payload_json, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action_id, order_id, item_id, actor, action_type, payload_json, timestamp),
    )
    return True


def _money_amount(value: Any, name: str):
    """Validate monetary input without binary float arithmetic or silent rounding."""
    return parse_money(value, name)


def _refresh_order_review_status(
    cursor: sqlite3.Cursor,
    order_id: int,
    now: str,
) -> None:
    """Mark an order ready only once every non-deleted line has a final state."""
    rows = [
        dict(row)
        for row in cursor.execute(
            "SELECT status, human_decision, unit_check_status FROM order_items WHERE order_id=?",
            (order_id,),
        )
    ]
    all_final = rows and all(row["status"] in FINAL_ITEM_STATUSES for row in rows)
    unit_ready = all(
        row.get("human_decision") != "SELECT"
        or unit_check_is_approval_ready(row.get("unit_check_status"))
        for row in rows
    )
    order_status = "analyzed" if all_final and unit_ready else "needs_review"
    cursor.execute(
        "UPDATE orders SET status=?, updated_at=? WHERE id=?",
        (order_status, now, order_id),
    )


def update_order_customer(
    order_id: int,
    *,
    actor: str,
    action_id: str,
    customer_name: Any = "",
    customer_phone: Any = "",
    customer_address: Any = "",
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Atomically update editable customer details with attributable intent."""
    actor = _require_text(actor, "Customer details actor")
    action_id = _require_text(action_id, "Action ID")
    payload = {
        "customer_name": _optional_bounded_text(customer_name, "Customer name"),
        "customer_phone": _optional_bounded_text(customer_phone, "Customer phone"),
        "customer_address": _optional_bounded_text(customer_address, "Customer address"),
    }
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        existing = _existing_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="order_customer_updated",
            payload=payload,
        )
        if existing:
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="order_customer_updated",
            payload=payload,
            timestamp=now,
        )
        cursor.execute(
            """
            UPDATE orders
            SET customer_name=?, customer_phone=?, customer_address=?, updated_at=?
            WHERE id=?
            """,
            (
                payload["customer_name"],
                payload["customer_phone"],
                payload["customer_address"],
                now,
                order_id,
            ),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "customer_details_updated",
            json.dumps(payload, ensure_ascii=False),
            now,
            actor=actor,
            action_id=action_id,
        )
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def add_manual_order_item(
    order_id: int,
    *,
    actor: str,
    action_id: str,
    selected_sku: str,
    quantity: Any,
    unit: str,
    catalog_lookup: Dict[str, Any],
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Add a human-selected catalogue line with the same provenance as review.

    The manual add is itself an explicit human SELECT decision.  It never
    inherits a model recommendation and carries a zero confidence, while its
    ``human_actions`` row and audit event are committed with the new item.
    """
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    selected_sku = _require_text(selected_sku, "Selected SKU")
    final_quantity = _finite_number(quantity, "Quantity", minimum=0.000000001)
    submitted_unit = _require_text(unit, "Unit")
    payload = {
        "selected_sku": selected_sku,
        "quantity": final_quantity,
        "unit": submitted_unit,
    }
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        existing = _existing_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="manual_item_added",
            payload=payload,
            allow_existing_item_id=True,
        )
        if existing:
            item = cursor.execute(
                "SELECT 1 FROM order_items WHERE id=? AND order_id=?",
                (existing["item_id"], order_id),
            ).fetchone()
            if not item:
                raise ValueError("Existing manual-line action has no matching item")
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        product = catalog_lookup.get(selected_sku)
        if not product:
            raise ValueError(f"Invalid product code: {selected_sku}")
        if not units_equivalent(submitted_unit, product.unit):
            raise ValueError(
                "Manual-line unit must match the selected catalog unit; unit conversions are not supported"
            )
        final_unit = _require_text(product.unit, "Catalog unit")
        catalog_price = parse_money(product.price, "Catalog price")
        cursor.execute(
            """
            INSERT INTO order_items (
                order_id, raw_text, extracted_product, extracted_quantity, extracted_unit,
                requested_unit, final_quantity, final_unit, unit_check_status,
                matched_product_id, matched_product_name, matched_unit, matched_price,
                recommendation_decision, confidence, status, is_manually_corrected,
                is_human_confirmed, human_decision, human_selected_sku, human_actor,
                human_confirmed_at, review_action_id, catalog_price, candidates_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', 0.0, 'human_selected', 0,
                      1, 'SELECT', ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (
                order_id,
                f"Manual line: {product.product_name}",
                product.product_name,
                final_quantity,
                final_unit,
                final_quantity,
                final_unit,
                UNIT_CHECK_MANUAL_CATALOG,
                selected_sku,
                product.product_name,
                product.unit,
                to_storage(catalog_price),
                selected_sku,
                actor,
                now,
                action_id,
                to_storage(catalog_price),
                now,
                now,
            ),
        )
        item_id = int(cursor.lastrowid)
        _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="manual_item_added",
            payload=payload,
            timestamp=now,
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "human_added_line",
            json.dumps(
                {
                    "item_id": item_id,
                    "selected_sku": selected_sku,
                    "quantity": final_quantity,
                    "unit": final_unit,
                    "catalog_unit": final_unit,
                    "unit_check_status": UNIT_CHECK_MANUAL_CATALOG,
                    "catalog_price": to_storage(catalog_price),
                    "requires_human_confirmation": False,
                    "human_decision": "SELECT",
                },
                ensure_ascii=False,
            ),
            now,
            actor=actor,
            action_id=action_id,
        )
        _refresh_order_review_status(cursor, order_id, now)
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def cancel_order_item(
    order_id: int,
    item_id: int,
    *,
    actor: str,
    action_id: str,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Soft-cancel an existing line as a final, attributable human decision."""
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    payload = {"final_decision": "CANCELLED"}
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        item = cursor.execute(
            "SELECT * FROM order_items WHERE id=? AND order_id=?",
            (item_id, order_id),
        ).fetchone()
        if not item:
            raise ValueError(f"Item #{item_id} not found in Order #{order_id}")
        existing = _existing_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="item_cancel",
            payload=payload,
        )
        if existing:
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        if item["status"] == "cancelled_by_human":
            raise ValueError(f"Item #{item_id} is already cancelled")
        _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="item_cancel",
            payload=payload,
            timestamp=now,
        )
        cursor.execute(
            """
            UPDATE order_items
            SET status='cancelled_by_human', is_human_confirmed=1,
                human_decision='CANCELLED', human_selected_sku=NULL,
                human_actor=?, human_confirmed_at=?, review_action_id=?, updated_at=?
            WHERE id=?
            """,
            (actor, now, action_id, now, item_id),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "human_cancelled_line",
            json.dumps(
                {
                    "item_id": item_id,
                    "previous_status": item["status"],
                    "previous_human_decision": item["human_decision"],
                    "excluded_from_approved_snapshot": True,
                },
                ensure_ascii=False,
            ),
            now,
            actor=actor,
            action_id=action_id,
        )
        _refresh_order_review_status(cursor, order_id, now)
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def override_order_item_price(
    order_id: int,
    item_id: int,
    *,
    actor: str,
    action_id: str,
    price: Any,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Record an explicit negotiated unit price for one already-selected line."""
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    override = _money_amount(price, "Price")
    payload = {"price": to_storage(override)}
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        item = cursor.execute(
            "SELECT * FROM order_items WHERE id=? AND order_id=?",
            (item_id, order_id),
        ).fetchone()
        if not item:
            raise ValueError(f"Item #{item_id} not found in Order #{order_id}")
        existing = _existing_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="item_price_override",
            payload=payload,
        )
        if existing:
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        if (
            item["status"] != "human_selected"
            or not item["is_human_confirmed"]
            or item["human_decision"] != "SELECT"
        ):
            raise ValueError("A price can be overridden only after a human SELECT decision")
        catalog_price = item["catalog_price"]
        if catalog_price is None:
            catalog_price = item["matched_price"]
        catalog_price = parse_money(catalog_price, "Catalog price")
        _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="item_price_override",
            payload=payload,
            timestamp=now,
        )
        cursor.execute(
            """
            UPDATE order_items
            SET catalog_price=?, price_override=?, price_overridden=1,
                price_override_actor=?, price_overridden_at=?,
                price_override_action_id=?, updated_at=?
            WHERE id=?
            """,
            (
                to_storage(catalog_price),
                to_storage(override),
                actor,
                now,
                action_id,
                now,
                item_id,
            ),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "price_overridden",
            json.dumps(
                {
                    "item_id": item_id,
                    "product_id": item["matched_product_id"],
                    "catalog_price": to_storage(catalog_price),
                    "price_override": to_storage(override),
                },
                ensure_ascii=False,
            ),
            now,
            actor=actor,
            action_id=action_id,
        )
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def update_order_discount(
    order_id: int,
    *,
    actor: str,
    action_id: str,
    discount: Any,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Set an absolute order discount; approval later freezes and bounds it."""
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    amount = _money_amount(discount, "Discount")
    payload = {"discount": to_storage(amount)}
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        existing = _existing_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="order_discount_updated",
            payload=payload,
        )
        if existing:
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="order_discount_updated",
            payload=payload,
            timestamp=now,
        )
        cursor.execute(
            "UPDATE orders SET discount=?, updated_at=? WHERE id=?",
            (to_storage(amount), now, order_id),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "order_discount_updated",
            json.dumps(payload, ensure_ascii=False),
            now,
            actor=actor,
            action_id=action_id,
        )
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def review_order_item(
    order_id: int,
    item_id: int,
    *,
    actor: str,
    action_id: str,
    final_decision: str,
    catalog_lookup: Dict[str, Any],
    selected_sku: Optional[str] = None,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    unit_resolution: Optional[str] = None,
    unit_resolution_note: Optional[str] = None,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Apply one attributable review without ever inferring a unit conversion.

    ``extracted_*`` remains the customer request.  A selected catalog line gets
    its own ``final_*`` commercial fields, so "3 cartons" can never be silently
    rewritten as "3 pieces" merely because the selected SKU is priced per piece.
    """
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    decision = _require_text(final_decision, "Final decision").upper()
    if decision not in {"SELECT", "NOT_FOUND"}:
        raise ValueError("Final decision must be SELECT or NOT_FOUND")

    resolution = str(unit_resolution or "").strip().upper() or None
    note = str(unit_resolution_note or "").strip()
    if len(note) > 500:
        raise ValueError("Unit-resolution note must be at most 500 characters")
    if resolution not in {None, "HUMAN_OVERRIDE"}:
        raise ValueError("Unit resolution must be HUMAN_OVERRIDE when supplied")

    if decision == "SELECT":
        selected_sku = _require_text(selected_sku, "Selected SKU")
        if quantity is None:
            raise ValueError("Quantity must be a positive number")
        quantity = _finite_number(quantity, "Quantity", minimum=0.000000001)
        unit = _require_text(unit, "Final commercial unit")
        if note and resolution is None:
            raise ValueError("Unit-resolution note requires HUMAN_OVERRIDE")
    else:
        if selected_sku:
            raise ValueError("NOT_FOUND must not include a selected SKU")
        if resolution or note:
            raise ValueError("NOT_FOUND must not include a unit-resolution override")

    # This exact request payload is immutable human evidence.  The audit event
    # additionally records the requested/catalog/final units seen at review.
    payload = {
        "final_decision": decision,
        "selected_sku": selected_sku,
        "quantity": float(quantity) if quantity is not None else None,
        "unit": unit,
        "unit_resolution": resolution,
        "unit_resolution_note": note or None,
    }
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        if order["status"] == "approved":
            raise ValueError(f"Order #{order_id} is approved and locked for editing")
        item_row = cursor.execute(
            "SELECT * FROM order_items WHERE id=? AND order_id=?",
            (item_id, order_id),
        ).fetchone()
        if not item_row:
            raise ValueError(f"Item #{item_id} not found in Order #{order_id}")
        item = dict(item_row)
        if item["status"] == "cancelled_by_human":
            raise ValueError(
                f"Item #{item_id} is cancelled and cannot be edited; add a new manual line instead"
            )
        created = _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=item_id,
            actor=actor,
            action_type="item_review",
            payload=payload,
            timestamp=now,
        )
        if not created:
            conn.commit()
            return get_order_by_id(order_id, db_path)  # type: ignore[return-value]

        if decision == "NOT_FOUND":
            cursor.execute(
                """
                UPDATE order_items
                SET matched_product_id=NULL, matched_product_name=NULL, matched_unit=NULL,
                    matched_price=NULL, final_quantity=NULL, final_unit=NULL,
                    unit_resolution=NULL, unit_resolution_note=NULL,
                    unit_resolution_actor=NULL, unit_resolved_at=NULL,
                    unit_resolution_action_id=NULL, status='not_found_confirmed',
                    is_human_confirmed=1, human_decision='NOT_FOUND',
                    human_selected_sku=NULL, human_actor=?, human_confirmed_at=?,
                    review_action_id=?, catalog_price=NULL, price_override=NULL,
                    price_overridden=0, price_override_actor=NULL,
                    price_overridden_at=NULL, price_override_action_id=NULL, updated_at=?
                WHERE id=?
                """,
                (actor, now, action_id, now, item_id),
            )
            log_audit_event_tx(
                cursor,
                order_id,
                "human_confirmed_not_found",
                json.dumps(
                    {
                        "item_id": item_id,
                        "model_decision": item.get("recommendation_decision"),
                        "excluded_from_approved_snapshot": True,
                    },
                    ensure_ascii=False,
                ),
                now,
                actor=actor,
                action_id=action_id,
            )
        else:
            product = catalog_lookup.get(selected_sku)
            if not product:
                raise ValueError(f"Invalid product code: {selected_sku}")
            if not units_equivalent(unit, product.unit):
                raise ValueError(
                    "Final commercial unit must match the selected catalog unit; unit conversions are not supported"
                )

            requested_unit = item.get("requested_unit")
            check = classify_unit_check(requested_unit, product.unit)
            if unit_check_requires_resolution(check):
                if resolution != "HUMAN_OVERRIDE" or not note:
                    raise ValueError(
                        "Customer and catalog units differ or one is missing. "
                        "Enter the final quantity in the catalog unit and record a HUMAN_OVERRIDE note; "
                        "the system does not convert units."
                    )
                resolved_status = UNIT_CHECK_HUMAN_OVERRIDE
            else:
                if resolution or note:
                    raise ValueError("HUMAN_OVERRIDE is allowed only when the unit differs or is missing")
                resolved_status = UNIT_CHECK_EQUIVALENT

            catalog_price = parse_money(product.price, "Catalog price")
            final_quantity = float(quantity)
            final_unit = _require_text(product.unit, "Catalog unit")
            # A correction means the human overrode the advisory model output,
            # not merely the provisional matcher state.
            model_sku = item.get("recommendation_product_id")
            model_quantity = float(item.get("extracted_quantity") or 0.0)
            model_unit = item.get("requested_unit") or item.get("extracted_unit")

            product_changed = model_sku != selected_sku
            quantity_changed = abs(model_quantity - final_quantity) > 0.001
            unit_changed = not units_equivalent(model_unit, final_unit)
            changed = product_changed or quantity_changed or unit_changed

            corrections = []
            if product_changed:
                corrections.append(("product_id", model_sku, selected_sku))
            if quantity_changed:
                corrections.append(("quantity", model_quantity, final_quantity))
            if unit_changed:
                corrections.append(("unit", model_unit, final_unit))

            for field, old, new in corrections:
                cursor.execute(
                    """
                    INSERT INTO corrections
                        (order_id, item_id, field_name, old_value, new_value, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        item_id,
                        field,
                        None if old is None else str(old),
                        str(new),
                        now,
                    ),
                )
            cursor.execute(
                """
                UPDATE order_items
                SET matched_product_id=?, matched_product_name=?, matched_unit=?,
                    matched_price=?, final_quantity=?, final_unit=?,
                    unit_check_status=?, unit_resolution=?, unit_resolution_note=?,
                    unit_resolution_actor=?, unit_resolved_at=?, unit_resolution_action_id=?,
                    status='human_selected', is_manually_corrected=?,
                    is_human_confirmed=1, human_decision='SELECT',
                    human_selected_sku=?, human_actor=?, human_confirmed_at=?,
                    review_action_id=?, catalog_price=?, price_override=NULL,
                    price_overridden=0, price_override_actor=NULL,
                    price_overridden_at=NULL, price_override_action_id=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    selected_sku,
                    product.product_name,
                    final_unit,
                    to_storage(catalog_price),
                    final_quantity,
                    final_unit,
                    resolved_status,
                    resolution,
                    note or None,
                    actor if resolution else None,
                    now if resolution else None,
                    action_id if resolution else None,
                    1 if changed else 0,
                    selected_sku,
                    actor,
                    now,
                    action_id,
                    to_storage(catalog_price),
                    now,
                    item_id,
                ),
            )
            audit_payload = {
                "item_id": item_id,
                "model_recommendation": item.get("recommendation_product_id"),
                "human_selected_final_sku": selected_sku,
                "model_confidence": item.get("confidence"),
                "requested_unit": requested_unit or "",
                "catalog_unit": final_unit,
                "final_quantity": final_quantity,
                "final_unit": final_unit,
                "unit_check_status": resolved_status,
                "unit_resolution": resolution,
                "unit_resolution_note": note or None,
            }
            log_audit_event_tx(
                cursor,
                order_id,
                "human_selected_final_sku",
                json.dumps(audit_payload, ensure_ascii=False),
                now,
                actor=actor,
                action_id=action_id,
            )
            if resolution == "HUMAN_OVERRIDE":
                log_audit_event_tx(
                    cursor,
                    order_id,
                    "unit_mismatch_resolved",
                    json.dumps(audit_payload, ensure_ascii=False),
                    now,
                    actor=actor,
                    action_id=action_id,
                )
            log_audit_event_tx(
                cursor,
                order_id,
                (
                    "human_confirmed_model_recommendation"
                    if item.get("recommendation_product_id") == selected_sku
                    else "human_rejected_model_recommendation"
                ),
                json.dumps(
                    {
                        "item_id": item_id,
                        "model_recommendation": item.get("recommendation_product_id"),
                        "final_sku": selected_sku,
                        "model_confidence": item.get("confidence"),
                    },
                    ensure_ascii=False,
                ),
                now,
                actor=actor,
                action_id=action_id,
            )

        _refresh_order_review_status(cursor, order_id, now)
        conn.commit()
    return get_order_by_id(order_id, db_path)  # type: ignore[return-value]


def update_order_item(
    order_id: int,
    item_id: int,
    updates: Dict[str, Any],
    catalog_lookup: Dict[str, Any],
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Compatibility boundary that still requires the complete review command."""
    return review_order_item(
        order_id,
        item_id,
        actor=updates.get("actor"),
        action_id=updates.get("action_id"),
        final_decision=updates.get("final_decision"),
        selected_sku=updates.get("selected_sku") or updates.get("product_id"),
        quantity=updates.get("quantity"),
        unit=updates.get("unit"),
        unit_resolution=updates.get("unit_resolution"),
        unit_resolution_note=updates.get("unit_resolution_note"),
        catalog_lookup=catalog_lookup,
        db_path=db_path,
    )


def confirm_not_found(
    order_id: int,
    item_id: int,
    *,
    actor: str,
    action_id: str,
    catalog_lookup: Optional[Dict[str, Any]] = None,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    return review_order_item(
        order_id,
        item_id,
        actor=actor,
        action_id=action_id,
        final_decision="NOT_FOUND",
        catalog_lookup=catalog_lookup or {},
        db_path=db_path,
    )


def _final_action_spec(item: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Return the exact action/audit pair that proves a final line decision."""
    decision = item.get("human_decision")
    if decision == "SELECT":
        if item.get("recommendation_decision") == "MANUAL":
            return "manual_item_added", "human_added_line"
        return "item_review", "human_selected_final_sku"
    if decision == "NOT_FOUND":
        return "item_review", "human_confirmed_not_found"
    if decision == "CANCELLED":
        return "item_cancel", "human_cancelled_line"
    return None


def _verified_price_override(
    cursor: sqlite3.Cursor,
    order_id: int,
    item: Dict[str, Any],
) -> bool:
    """Verify that an effective override price still has attributable evidence."""
    overridden = bool(item.get("price_overridden"))
    if not overridden:
        return item.get("price_override") is None
    override = item.get("price_override")
    action_id = item.get("price_override_action_id")
    actor = item.get("price_override_actor")
    timestamp = item.get("price_overridden_at")
    if override is None or not action_id or not actor or not timestamp:
        return False
    try:
        expected = _money_amount(override, "Price")
    except ValueError:
        return False
    action = cursor.execute(
        """
        SELECT * FROM human_actions
        WHERE action_id=? AND order_id=? AND item_id=?
          AND actor=? AND action_type='item_price_override'
        """,
        (action_id, order_id, item["id"], actor),
    ).fetchone()
    event = cursor.execute(
        """
        SELECT 1 FROM audit_events
        WHERE order_id=? AND event_type='price_overridden' AND actor=? AND action_id=?
        """,
        (order_id, actor, action_id),
    ).fetchone()
    return bool(
        action
        and action["timestamp"] == timestamp
        and _money_payload_matches(action["payload_json"], "price", expected)
        and event
    )


def _verified_final_line_decision(
    cursor: sqlite3.Cursor,
    order_id: int,
    item: Dict[str, Any],
) -> bool:
    """Fail closed unless the live line still matches its human evidence."""
    spec = _final_action_spec(item)
    action_id = item.get("review_action_id")
    actor = item.get("human_actor")
    confirmed_at = item.get("human_confirmed_at")
    if (
        not spec
        or not item.get("is_human_confirmed")
        or not action_id
        or not actor
        or not confirmed_at
    ):
        return False
    action_type, event_type = spec
    action = cursor.execute(
        """
        SELECT * FROM human_actions
        WHERE action_id=? AND order_id=? AND item_id=? AND actor=? AND action_type=?
        """,
        (action_id, order_id, item["id"], actor, action_type),
    ).fetchone()
    event = cursor.execute(
        """
        SELECT 1 FROM audit_events
        WHERE order_id=? AND event_type=? AND actor=? AND action_id=?
        """,
        (order_id, event_type, actor, action_id),
    ).fetchone()
    if not action or action["timestamp"] != confirmed_at or not event:
        return False

    decision = item.get("human_decision")
    if decision == "SELECT":
        if (
            item.get("status") != "human_selected"
            or not item.get("matched_product_id")
            or item.get("human_selected_sku") != item.get("matched_product_id")
            or item.get("final_quantity") is None
            or not item.get("final_unit")
            or not item.get("matched_unit")
            or not units_equivalent(item.get("final_unit"), item.get("matched_unit"))
            or not unit_check_is_approval_ready(item.get("unit_check_status"))
        ):
            return False
        try:
            action_payload = json.loads(action["payload_json"])
            action_quantity = _finite_number(
                action_payload.get("quantity"), "Reviewed quantity", minimum=0.000000001
            )
            final_quantity = _finite_number(
                item.get("final_quantity"), "Final quantity", minimum=0.000000001
            )
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return False
        if action_type != "manual_item_added" and (
            not isinstance(action_payload, dict)
            or action_payload.get("final_decision") != "SELECT"
            or action_payload.get("selected_sku") != item.get("matched_product_id")
            or abs(action_quantity - final_quantity) > 0.000000001
            or not units_equivalent(action_payload.get("unit"), item.get("final_unit"))
        ):
            return False
        if action_type == "manual_item_added":
            # A manual add may use an obvious alias (pcs/piece, قطعة/قطع),
            # while the final document intentionally keeps the catalog spelling.
            try:
                manual_payload = json.loads(action["payload_json"])
                manual_quantity = _finite_number(
                    manual_payload.get("quantity"), "Manual quantity", minimum=0.000000001
                )
            except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                return False
            if (
                not isinstance(manual_payload, dict)
                or manual_payload.get("selected_sku") != item.get("matched_product_id")
                or abs(manual_quantity - final_quantity) > 0.000000001
                or not units_equivalent(manual_payload.get("unit"), item.get("final_unit"))
            ):
                return False
        elif item.get("unit_check_status") == UNIT_CHECK_HUMAN_OVERRIDE:
            if (
                action_payload.get("unit_resolution") != "HUMAN_OVERRIDE"
                or action_payload.get("unit_resolution_note") != item.get("unit_resolution_note")
                or item.get("unit_resolution") != "HUMAN_OVERRIDE"
                or not item.get("unit_resolution_note")
                or item.get("unit_resolution_actor") != actor
                or item.get("unit_resolved_at") != confirmed_at
                or item.get("unit_resolution_action_id") != action_id
            ):
                return False
            resolution_event = cursor.execute(
                """
                SELECT 1 FROM audit_events
                WHERE order_id=? AND event_type='unit_mismatch_resolved'
                  AND actor=? AND action_id=?
                """,
                (order_id, actor, action_id),
            ).fetchone()
            if not resolution_event:
                return False
        elif item.get("unit_check_status") == UNIT_CHECK_EQUIVALENT:
            if (
                action_payload.get("unit_resolution") is not None
                or action_payload.get("unit_resolution_note") is not None
                or item.get("unit_resolution") is not None
                or item.get("unit_resolution_note") is not None
            ):
                return False
        else:
            return False
        return _verified_price_override(cursor, order_id, item)
    if decision == "NOT_FOUND":
        return item.get("status") == "not_found_confirmed" and item.get("matched_product_id") is None
    if decision == "CANCELLED":
        return item.get("status") == "cancelled_by_human"
    return False


def _effective_item_price(item: Dict[str, Any]):
    """Return (effective, catalog, override, overridden) for a selected line."""
    catalog = item.get("catalog_price")
    if catalog is None:
        catalog = item.get("matched_price")
    catalog_price = from_storage(catalog, "Catalog price")
    if catalog_price < ZERO:
        raise ValueError("Catalog price must not be negative")
    overridden = bool(item.get("price_overridden"))
    if not overridden:
        if item.get("price_override") is not None:
            raise ValueError("A non-overridden line cannot carry a price override")
        return catalog_price, catalog_price, None, False
    override = _money_amount(item.get("price_override"), "Price")
    return override, catalog_price, override, True


def _approval_response(cursor: sqlite3.Cursor, order: sqlite3.Row) -> Dict[str, Any]:
    snapshot = [
        dict(row)
        for row in cursor.execute(
            "SELECT * FROM approved_order_items WHERE order_id=? ORDER BY id",
            (order["id"],),
        )
    ]
    financials = _frozen_order_financials(cursor, order, snapshot)
    return {
        "order_id": order["id"],
        "status": "approved",
        "message": "Ready for warehouse",
        "approved_at": order["approved_at"],
        "snapshot": snapshot,
        "customer_name": financials["customer_name"],
        "customer_phone": financials["customer_phone"],
        "customer_address": financials["customer_address"],
        "subtotal": financials["subtotal"],
        "discount": financials["discount"],
        "grand_total": financials["grand_total"],
    }


def approve_order(
    order_id: int,
    *,
    actor: str,
    action_id: str,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Atomically approve reviewed lines, audit the action, and create the snapshot."""
    actor = _require_text(actor, "Approval actor")
    action_id = _require_text(action_id, "Action ID")
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValueError(f"Order #{order_id} not found")
        if order["status"] == "approved":
            existing = cursor.execute(
                "SELECT * FROM human_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if (
                existing
                and existing["order_id"] == order_id
                and existing["item_id"] is None
                and existing["actor"] == actor
                and existing["action_type"] == "order_approval"
            ):
                return _approval_response(cursor, order)
            if existing:
                raise ValueError(f"Action ID '{action_id}' was already used for another command")
            raise ValueError(
                f"Order #{order_id} is already approved; retry requires the original approval action ID"
            )

        items = [
            dict(row)
            for row in cursor.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,))
        ]
        if not items:
            raise ValueError("Cannot approve order with no items")
        invalid = []
        unresolved_unit_items = []
        selected = []
        for item in items:
            decision = item.get("human_decision")
            if decision == "SELECT" and not unit_check_is_approval_ready(
                item.get("unit_check_status")
            ):
                unresolved_unit_items.append(item.get("raw_text") or f"Item #{item['id']}")
                continue
            if decision not in HUMAN_DECISIONS or not _verified_final_line_decision(
                cursor, order_id, item
            ):
                invalid.append(item.get("raw_text") or f"Item #{item['id']}")
                continue
            if decision == "SELECT":
                selected.append(item)
        if unresolved_unit_items:
            raise ValueError(
                "Cannot approve order with unresolved unit mismatch: "
                f"{', '.join(unresolved_unit_items)}. Record an explicit HUMAN_OVERRIDE "
                "with the final quantity in the catalog unit, or exclude the line."
            )
        if invalid:
            raise ValueError(
                f"Cannot approve order with unresolved items: {', '.join(invalid)}. "
                "Every item requires an attributable final decision."
            )
        if not selected:
            raise ValueError("Cannot approve an order with no human-selected SKU to export")

        payload = {
            "final_decision": "APPROVE",
            "selected_item_ids": [item["id"] for item in selected],
            "excluded_not_found_item_ids": [
                item["id"] for item in items if item.get("human_decision") == "NOT_FOUND"
            ],
            "excluded_cancelled_item_ids": [
                item["id"] for item in items if item.get("human_decision") == "CANCELLED"
            ],
        }
        created = _insert_action(
            cursor,
            action_id=action_id,
            order_id=order_id,
            item_id=None,
            actor=actor,
            action_type="order_approval",
            payload=payload,
            timestamp=now,
        )
        if not created:
            refreshed = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if refreshed["status"] != "approved":
                raise ValueError("Duplicate approval action did not complete")
            return _approval_response(cursor, refreshed)

        frozen_lines: list[tuple[Dict[str, Any], Decimal, Decimal, Optional[Decimal], bool]] = []
        for item in selected:
            try:
                effective_price, catalog_price, price_override, price_overridden = _effective_item_price(item)
            except ValueError:
                invalid.append(item.get("raw_text") or f"Item #{item['id']}")
                continue
            frozen_lines.append((item, effective_price, catalog_price, price_override, price_overridden))
        if invalid:
            raise ValueError(
                f"Cannot approve order with invalid price evidence: {', '.join(invalid)}"
            )
        frozen_line_totals = [
            line_total(price, _finite_number(item.get("final_quantity"), "Quantity", minimum=0.0))
            for item, price, _catalog, _override, _overridden in frozen_lines
        ]
        subtotal = money_sum(frozen_line_totals)
        discount = _money_amount(order["discount"], "Discount")
        if discount > subtotal:
            raise ValueError("Discount cannot exceed the approved order subtotal")
        grand_total = (subtotal - discount).quantize(Decimal("0.01"))

        for (item, effective_price, catalog_price, price_override, price_overridden), frozen_line_total in zip(
            frozen_lines, frozen_line_totals, strict=True
        ):
            cursor.execute(
                """
                INSERT INTO approved_order_items (
                    order_id, original_item_id, product_id, product_name, quantity,
                    unit, requested_unit, catalog_unit, unit_check_status, unit_resolution,
                    unit_resolution_note, unit_resolution_actor, unit_resolved_at,
                    unit_resolution_action_id, price, line_total, confidence,
                    model_recommendation_id, model_decision,
                    human_decision, human_selected_sku, human_actor, human_confirmed_at,
                    review_action_id, approval_actor, approval_action_id,
                    provenance_verified, is_manually_corrected, is_human_confirmed,
                    catalog_price, price_override, price_overridden,
                    price_override_actor, price_overridden_at, price_override_action_id,
                    approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SELECT', ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["id"],
                    item["matched_product_id"],
                    item["matched_product_name"],
                    item["final_quantity"],
                    item["final_unit"],
                    item.get("requested_unit"),
                    item.get("matched_unit"),
                    item["unit_check_status"],
                    item.get("unit_resolution"),
                    item.get("unit_resolution_note"),
                    item.get("unit_resolution_actor"),
                    item.get("unit_resolved_at"),
                    item.get("unit_resolution_action_id"),
                    to_storage(effective_price),
                    to_storage(frozen_line_total),
                    item["confidence"],
                    item.get("recommendation_product_id"),
                    item.get("recommendation_decision"),
                    item["human_selected_sku"],
                    item["human_actor"],
                    item["human_confirmed_at"],
                    item["review_action_id"],
                    actor,
                    action_id,
                    item["is_manually_corrected"],
                    to_storage(catalog_price),
                    to_storage(price_override) if price_override is not None else None,
                    1 if price_overridden else 0,
                    item.get("price_override_actor"),
                    item.get("price_overridden_at"),
                    item.get("price_override_action_id"),
                    now,
                ),
            )
        cursor.execute(
            "UPDATE orders SET status='approved', approved_at=?, updated_at=? WHERE id=?",
            (now, now, order_id),
        )
        cursor.execute(
            """
            INSERT INTO approved_order_financials (
                order_id, customer_name, customer_phone, customer_address,
                subtotal, discount, grand_total, approved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                order["customer_name"] or "",
                order["customer_phone"] or "",
                order["customer_address"] or "",
                to_storage(subtotal),
                to_storage(discount),
                to_storage(grand_total),
                now,
            ),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "order_approved",
            json.dumps(
                {
                    "approved_snapshot_item_ids": [item["id"] for item in selected],
                    "excluded_not_found_item_ids": payload["excluded_not_found_item_ids"],
                    "excluded_cancelled_item_ids": payload["excluded_cancelled_item_ids"],
                    "subtotal": to_storage(subtotal),
                    "discount": to_storage(discount),
                    "grand_total": to_storage(grand_total),
                },
                ensure_ascii=False,
            ),
            now,
            actor=actor,
            action_id=action_id,
        )
        conn.commit()
        approved = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return _approval_response(cursor, approved)


def _verified_snapshot_item_provenance(
    cursor: sqlite3.Cursor,
    order_id: int,
    item: Dict[str, Any],
) -> bool:
    """Re-check exportable snapshot evidence without trusting mutable line rows."""
    if not (
        item.get("provenance_verified") == 1
        and item.get("is_human_confirmed") == 1
        and item.get("human_decision") == "SELECT"
        and item.get("human_selected_sku") == item.get("product_id")
        and item.get("human_actor")
        and item.get("human_confirmed_at")
        and item.get("review_action_id")
        and item.get("approval_actor")
        and item.get("approval_action_id")
    ):
        return False
    action_type = (
        "manual_item_added" if item.get("model_decision") == "MANUAL" else "item_review"
    )
    review_event_type = (
        "human_added_line" if action_type == "manual_item_added" else "human_selected_final_sku"
    )
    review_action = cursor.execute(
        """
        SELECT * FROM human_actions
        WHERE action_id=? AND order_id=? AND item_id=? AND actor=? AND action_type=?
        """,
        (
            item["review_action_id"],
            order_id,
            item["original_item_id"],
            item["human_actor"],
            action_type,
        ),
    ).fetchone()
    approval_action = cursor.execute(
        """
        SELECT 1 FROM human_actions
        WHERE action_id=? AND order_id=? AND item_id IS NULL
          AND actor=? AND action_type='order_approval'
        """,
        (item["approval_action_id"], order_id, item["approval_actor"]),
    ).fetchone()
    review_event = cursor.execute(
        """
        SELECT 1 FROM audit_events
        WHERE order_id=? AND event_type=? AND actor=? AND action_id=?
        """,
        (order_id, review_event_type, item["human_actor"], item["review_action_id"]),
    ).fetchone()
    approval_event = cursor.execute(
        """
        SELECT 1 FROM audit_events
        WHERE order_id=? AND event_type='order_approved' AND actor=? AND action_id=?
        """,
        (order_id, item["approval_actor"], item["approval_action_id"]),
    ).fetchone()
    if (
        not review_action
        or review_action["timestamp"] != item["human_confirmed_at"]
        or not approval_action
        or not review_event
        or not approval_event
    ):
        return False
    unit_status = item.get("unit_check_status") or UNIT_CHECK_LEGACY_APPROVED
    if unit_status != UNIT_CHECK_LEGACY_APPROVED:
        if (
            not unit_check_is_approval_ready(unit_status)
            or not item.get("catalog_unit")
            or not units_equivalent(item.get("unit"), item.get("catalog_unit"))
        ):
            return False
        try:
            review_payload = json.loads(review_action["payload_json"])
            reviewed_quantity = _finite_number(
                review_payload.get("quantity"), "Reviewed quantity", minimum=0.000000001
            )
            snapshot_quantity = _finite_number(
                item.get("quantity"), "Snapshot quantity", minimum=0.000000001
            )
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return False
        if action_type == "item_review":
            if (
                not isinstance(review_payload, dict)
                or review_payload.get("final_decision") != "SELECT"
                or review_payload.get("selected_sku") != item.get("product_id")
                or abs(reviewed_quantity - snapshot_quantity) > 0.000000001
                or not units_equivalent(review_payload.get("unit"), item.get("unit"))
            ):
                return False
        elif action_type == "manual_item_added":
            if (
                not isinstance(review_payload, dict)
                or review_payload.get("selected_sku") != item.get("product_id")
                or abs(reviewed_quantity - snapshot_quantity) > 0.000000001
                or not units_equivalent(review_payload.get("unit"), item.get("unit"))
            ):
                return False
        if unit_status == UNIT_CHECK_HUMAN_OVERRIDE:
            if (
                review_payload.get("unit_resolution") != "HUMAN_OVERRIDE"
                or review_payload.get("unit_resolution_note") != item.get("unit_resolution_note")
                or item.get("unit_resolution") != "HUMAN_OVERRIDE"
                or not item.get("unit_resolution_note")
                or item.get("unit_resolution_actor") != item.get("human_actor")
                or item.get("unit_resolved_at") != item.get("human_confirmed_at")
                or item.get("unit_resolution_action_id") != item.get("review_action_id")
            ):
                return False
            resolution_event = cursor.execute(
                """
                SELECT 1 FROM audit_events
                WHERE order_id=? AND event_type='unit_mismatch_resolved'
                  AND actor=? AND action_id=?
                """,
                (order_id, item["human_actor"], item["review_action_id"]),
            ).fetchone()
            if not resolution_event:
                return False
        elif unit_status == UNIT_CHECK_EQUIVALENT:
            if item.get("unit_resolution") or item.get("unit_resolution_note"):
                return False
    overridden = bool(item.get("price_overridden"))
    catalog = item.get("catalog_price")
    if catalog is None:
        catalog = item.get("price")
    try:
        catalog_price = from_storage(catalog, "Snapshot catalog price")
        effective = from_storage(item.get("price"), "Snapshot price")
    except ValueError:
        return False
    if catalog_price < ZERO or effective < ZERO:
        return False
    if not overridden:
        return item.get("price_override") is None and effective == catalog_price

    override = item.get("price_override")
    override_action_id = item.get("price_override_action_id")
    override_actor = item.get("price_override_actor")
    override_time = item.get("price_overridden_at")
    if override is None or not override_action_id or not override_actor or not override_time:
        return False
    try:
        # This is frozen, pre-existing commercial evidence.  It must remain
        # exportable even if the old recorded value predates today's two-place
        # input rule; mutable overrides are still validated by _money_amount.
        amount = from_storage(override, "Price")
    except ValueError:
        return False
    if effective != amount:
        return False
    override_action = cursor.execute(
        """
        SELECT * FROM human_actions
        WHERE action_id=? AND order_id=? AND item_id=? AND actor=?
          AND action_type='item_price_override'
        """,
        (override_action_id, order_id, item["original_item_id"], override_actor),
    ).fetchone()
    override_event = cursor.execute(
        """
        SELECT 1 FROM audit_events
        WHERE order_id=? AND event_type='price_overridden' AND actor=? AND action_id=?
        """,
        (order_id, override_actor, override_action_id),
    ).fetchone()
    return bool(
        override_action
        and override_action["timestamp"] == override_time
        and _money_payload_matches(
            override_action["payload_json"], "price", amount, allow_legacy=True
        )
        and override_event
    )


def load_approved_document(
    order_id: int,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Everything the picking note needs, read-only, from frozen state alone.

    Deliberately not :func:`record_export`: that one mutates (``export_count``,
    ``last_exported_at``) and an operator re-downloading the same document must
    get the same bytes back. Every field here is frozen at approval — the
    snapshot rows, the lines the human confirmed missing, and the extractor
    fragments — so the live catalog cannot reach into an old document.
    """
    with session(db_path) as conn:
        order_row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order_row:
            raise LookupError(f"Order #{order_id} not found")
        order = dict(order_row)
        if order["status"] != "approved":
            raise ValueError(
                f"Cannot produce a document for order #{order_id} with status "
                f"'{order['status']}'. Only approved orders have a document."
            )
        snapshot = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM approved_order_items WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        if not snapshot:
            raise ValueError(f"Approved snapshot missing for order #{order_id}")
        not_found = [
            dict(row)
            for row in conn.execute(
                """
                SELECT raw_text, extracted_product, extracted_quantity, extracted_unit
                FROM order_items
                WHERE order_id=? AND human_decision='NOT_FOUND'
                ORDER BY id
                """,
                (order_id,),
            )
        ]
        audit_events = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM audit_events WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        financials = _frozen_order_financials(conn.cursor(), order, snapshot)
    return {
        "order_id": order_id,
        "status": order["status"],
        "approved_at": order["approved_at"],
        "created_at": order["created_at"],
        "original_text": order["original_text"],
        "snapshot": snapshot,
        "customer_name": financials["customer_name"],
        "customer_phone": financials["customer_phone"],
        "customer_address": financials["customer_address"],
        "subtotal": financials["subtotal"],
        "discount": financials["discount"],
        "grand_total": financials["grand_total"],
        "not_found_items": not_found,
        "unresolved": unresolved_text_of(audit_events),
    }


def _snapshot_hash(snapshot: List[Dict[str, Any]]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_export(
    order_id: int,
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Export only a provenance-verified approved snapshot."""
    now = utc_now_iso()
    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
        order_row = cursor.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order_row:
            raise ValueError(f"Order #{order_id} not found")
        order = dict(order_row)
        if order["status"] != "approved":
            raise ValueError(
                f"Cannot export order #{order_id} with status '{order['status']}'. "
                "Only approved orders can be exported."
            )
        snapshot = [
            dict(row)
            for row in cursor.execute(
                "SELECT * FROM approved_order_items WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]
        if not snapshot:
            raise ValueError(f"Approved snapshot missing for order #{order_id}")
        for item in snapshot:
            if not _verified_snapshot_item_provenance(cursor, order_id, item):
                raise ValueError(
                    f"Approved snapshot for order #{order_id} lacks verified human provenance"
                )

        financials = _frozen_order_financials(cursor, order, snapshot)
        snapshot_sha256 = _snapshot_hash(snapshot)
        export_count = int(order.get("export_count") or 0) + 1
        cursor.execute(
            "UPDATE orders SET export_count=?, last_exported_at=?, updated_at=? WHERE id=?",
            (export_count, now, now, order_id),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "order_exported",
            json.dumps(
                {
                    "export_count": export_count,
                    "snapshot_row_ids": [item["id"] for item in snapshot],
                    "approved_at": order["approved_at"],
                    "snapshot_sha256": snapshot_sha256,
                    "subtotal": financials["subtotal"],
                    "discount": financials["discount"],
                    "grand_total": financials["grand_total"],
                },
                ensure_ascii=False,
            ),
            now,
        )
        conn.commit()
        return {
            "order_id": order_id,
            "original_text": order["original_text"],
            "customer_name": financials["customer_name"],
            "customer_phone": financials["customer_phone"],
            "customer_address": financials["customer_address"],
            "subtotal": financials["subtotal"],
            "discount": financials["discount"],
            "grand_total": financials["grand_total"],
            "export_count": export_count,
            "last_exported_at": now,
            "snapshot_sha256": snapshot_sha256,
            "snapshot": snapshot,
        }
