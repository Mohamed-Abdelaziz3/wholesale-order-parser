"""Fail-closed SQLite persistence for advisory matching and human approval."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from os import PathLike
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()
if os.name == "nt":
    load_dotenv(r"C:\ProgramData\wop\.env")

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
            price REAL NOT NULL,
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
            catalog_price REAL,
            price_override REAL,
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
                discount REAL NOT NULL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS order_items (
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
                recommendation_product_id TEXT,
                recommendation_product_name TEXT,
                recommendation_unit TEXT,
                recommendation_price REAL,
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
                catalog_price REAL,
                price_override REAL,
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
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_products (
                product_id TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0.0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_count INTEGER NOT NULL,
                source_name TEXT,
                content_sha256 TEXT NOT NULL,
                actor TEXT,
                created_at TEXT NOT NULL
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
                subtotal REAL NOT NULL,
                discount REAL NOT NULL,
                grand_total REAL NOT NULL,
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
                    "recommendation_product_id": "TEXT",
                    "recommendation_product_name": "TEXT",
                    "recommendation_unit": "TEXT",
                    "recommendation_price": "REAL",
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
                    "catalog_price": "REAL",
                    "price_override": "REAL",
                    "price_overridden": "INTEGER NOT NULL DEFAULT 0",
                    "price_override_actor": "TEXT",
                    "price_overridden_at": "TEXT",
                    "price_override_action_id": "TEXT",
                },
            )
            _ensure_columns(
                conn,
                "orders",
                {
                    "customer_name": "TEXT",
                    "customer_phone": "TEXT",
                    "customer_address": "TEXT",
                    "discount": "REAL NOT NULL DEFAULT 0.0",
                },
            )
            _ensure_columns(
                conn,
                "audit_events",
                {"actor": "TEXT", "action_id": "TEXT"},
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
                    "catalog_price": "REAL",
                    "price_override": "REAL",
                    "price_overridden": "INTEGER NOT NULL DEFAULT 0",
                    "price_override_actor": "TEXT",
                    "price_overridden_at": "TEXT",
                    "price_override_action_id": "TEXT",
                },
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (ORDER_FLEXIBILITY_MIGRATION_ID, utc_now_iso()),
            )
            conn.execute("RELEASE SAVEPOINT human_review_forward_migration")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT human_review_forward_migration")
            conn.execute("RELEASE SAVEPOINT human_review_forward_migration")
            raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_events(order_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action_id)")
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


def replace_catalog(
    products: List[Dict[str, Any]],
    db_path: Optional[DatabasePath] = None,
    *,
    source_name: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically replace the entire catalog and record a version row."""
    if not products:
        raise ValueError("Cannot install an empty catalog")

    now = utc_now_iso()
    canonical = json.dumps(products, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    with session(db_path, immediate=True) as conn:
        cursor = conn.cursor()
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
                    product.get("unit") or "",
                    float(product.get("price") or 0.0),
                    index,
                    now,
                )
                for index, product in enumerate(products)
            ],
        )
        cursor.execute(
            """
            INSERT INTO catalog_versions
                (product_count, source_name, content_sha256, actor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (len(products), source_name, digest, actor, now),
        )
        version_id = int(cursor.lastrowid)

    return {
        "version_id": version_id,
        "product_count": len(products),
        "content_sha256": digest,
        "installed_at": now,
        "source_name": source_name,
        "actor": actor,
    }


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


def save_processed_order(
    original_text: str,
    items_data: List[Dict[str, Any]],
    unresolved_text: List[str],
    processing_time_ms: float,
    db_path: Optional[DatabasePath] = None,
) -> int:
    """Persist model recommendations only; human or approved state is rejected."""
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

    now = utc_now_iso()
    with session(db_path) as conn:
        cursor = conn.cursor()
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
            cursor.execute(
                """
                INSERT INTO order_items (
                    order_id, raw_text, extracted_product, extracted_quantity, extracted_unit,
                    recommendation_product_id, recommendation_product_name, recommendation_unit,
                    recommendation_price, recommendation_decision, confidence, status,
                    candidates_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item.get("raw_text", ""),
                    item.get("extracted_product", ""),
                    item.get("extracted_quantity", 1.0),
                    item.get("extracted_unit", "unit"),
                    recommendation.get("product_id"),
                    recommendation.get("product_name"),
                    recommendation.get("unit"),
                    recommendation.get("price"),
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
        conn.commit()
        return order_id


def _snapshot_subtotal(snapshot: List[Dict[str, Any]]) -> float:
    """Calculate a money subtotal from the frozen effective unit prices."""
    total = 0.0
    for item in snapshot:
        price = _finite_number(item.get("price"), "Snapshot price", minimum=0.0)
        quantity = _finite_number(item.get("quantity"), "Snapshot quantity", minimum=0.0)
        total += round(price * quantity, 2)
    return round(total, 2)


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
        "discount": 0.0,
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
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


def _money_amount(value: Any, name: str, *, minimum: float = 0.0) -> float:
    """Validate an operator-entered money amount to exactly cents precision."""
    amount = _finite_number(value, name, minimum=minimum)
    rounded = round(amount, 2)
    if abs(amount - rounded) > 1e-9:
        raise ValueError(f"{name} must have at most two decimal places")
    return rounded


def _refresh_order_review_status(
    cursor: sqlite3.Cursor,
    order_id: int,
    now: str,
) -> None:
    """Mark an order ready only once every non-deleted line has a final state."""
    statuses = [
        row["status"]
        for row in cursor.execute("SELECT status FROM order_items WHERE order_id=?", (order_id,))
    ]
    order_status = "analyzed" if statuses and all(s in FINAL_ITEM_STATUSES for s in statuses) else "needs_review"
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
    final_unit = _require_text(unit, "Unit")
    payload = {
        "selected_sku": selected_sku,
        "quantity": final_quantity,
        "unit": final_unit,
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
        catalog_price = _finite_number(product.price, "Catalog price", minimum=0.0)
        cursor.execute(
            """
            INSERT INTO order_items (
                order_id, raw_text, extracted_product, extracted_quantity, extracted_unit,
                matched_product_id, matched_product_name, matched_unit, matched_price,
                recommendation_decision, confidence, status, is_manually_corrected,
                is_human_confirmed, human_decision, human_selected_sku, human_actor,
                human_confirmed_at, review_action_id, catalog_price, candidates_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', 0.0, 'human_selected', 0,
                      1, 'SELECT', ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (
                order_id,
                f"Manual line: {product.product_name}",
                product.product_name,
                final_quantity,
                final_unit,
                selected_sku,
                product.product_name,
                product.unit,
                catalog_price,
                selected_sku,
                actor,
                now,
                action_id,
                catalog_price,
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
                    "catalog_price": catalog_price,
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
    payload = {"price": override}
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
        catalog_price = _finite_number(catalog_price, "Catalog price", minimum=0.0)
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
            (catalog_price, override, actor, now, action_id, now, item_id),
        )
        log_audit_event_tx(
            cursor,
            order_id,
            "price_overridden",
            json.dumps(
                {
                    "item_id": item_id,
                    "product_id": item["matched_product_id"],
                    "catalog_price": catalog_price,
                    "price_override": override,
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
    payload = {"discount": amount}
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
        cursor.execute("UPDATE orders SET discount=?, updated_at=? WHERE id=?", (amount, now, order_id))
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
    db_path: Optional[DatabasePath] = None,
) -> Dict[str, Any]:
    """Apply one explicit, attributable, idempotent human review command."""
    actor = _require_text(actor, "Human actor")
    action_id = _require_text(action_id, "Action ID")
    decision = _require_text(final_decision, "Final decision").upper()
    if decision not in {"SELECT", "NOT_FOUND"}:
        raise ValueError("Final decision must be SELECT or NOT_FOUND")
    if decision == "SELECT":
        selected_sku = _require_text(selected_sku, "Selected SKU")
        if quantity is None:
            raise ValueError("Quantity must be a positive number")
        quantity = _finite_number(quantity, "Quantity", minimum=0.000000001)
        unit = _require_text(unit, "Unit")
    elif selected_sku:
        raise ValueError("NOT_FOUND must not include a selected SKU")

    payload = {
        "final_decision": decision,
        "selected_sku": selected_sku,
        "quantity": float(quantity) if quantity is not None else None,
        "unit": unit,
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
                    matched_price=NULL, status='not_found_confirmed',
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
            catalog_price = _finite_number(product.price, "Catalog price", minimum=0.0)
            final_quantity = float(quantity)
            final_unit = str(unit)
            # A "correction" means the human overrode what the MODEL proposed.
            # The baseline is therefore the model recommendation, never
            # matched_product_id (which is NULL until this very review runs and
            # would otherwise record a spurious None -> SKU correction on every
            # accepted recommendation, poisoning the learning signal).
            model_sku = item.get("recommendation_product_id")
            model_quantity = float(item.get("extracted_quantity") or 0.0)
            model_unit = item.get("extracted_unit")

            product_changed = model_sku != selected_sku
            quantity_changed = abs(model_quantity - final_quantity) > 0.001
            unit_changed = (model_unit or "") != final_unit
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
                    matched_price=?, extracted_quantity=?, extracted_unit=?,
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
                    product.unit,
                    catalog_price,
                    final_quantity,
                    final_unit,
                    1 if changed else 0,
                    selected_sku,
                    actor,
                    now,
                    action_id,
                    catalog_price,
                    now,
                    item_id,
                ),
            )
            audit_payload = {
                "item_id": item_id,
                "model_recommendation": item.get("recommendation_product_id"),
                "human_selected_final_sku": selected_sku,
                "model_confidence": item.get("confidence"),
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
        expected_payload = _canonical_action_payload({"price": _money_amount(override, "Price")})
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
        and action["payload_json"] == expected_payload
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
        ):
            return False
        if action_type == "manual_item_added":
            expected_payload = _canonical_action_payload(
                {
                    "selected_sku": item.get("matched_product_id"),
                    "quantity": _finite_number(
                        item.get("extracted_quantity"), "Manual quantity", minimum=0.000000001
                    ),
                    "unit": _require_text(item.get("extracted_unit"), "Manual unit"),
                }
            )
            if action["payload_json"] != expected_payload:
                return False
        return _verified_price_override(cursor, order_id, item)
    if decision == "NOT_FOUND":
        return item.get("status") == "not_found_confirmed" and item.get("matched_product_id") is None
    if decision == "CANCELLED":
        return item.get("status") == "cancelled_by_human"
    return False


def _effective_item_price(item: Dict[str, Any]) -> tuple[float, float, Optional[float], bool]:
    """Return (effective, catalog, override, overridden) for a selected line."""
    catalog = item.get("catalog_price")
    if catalog is None:
        catalog = item.get("matched_price")
    catalog_price = _finite_number(catalog, "Catalog price", minimum=0.0)
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
        selected = []
        for item in items:
            decision = item.get("human_decision")
            if decision not in HUMAN_DECISIONS or not _verified_final_line_decision(
                cursor, order_id, item
            ):
                invalid.append(item.get("raw_text") or f"Item #{item['id']}")
                continue
            if decision == "SELECT":
                selected.append(item)
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

        frozen_lines: list[tuple[Dict[str, Any], float, float, Optional[float], bool]] = []
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
        subtotal = round(
            sum(round(price * _finite_number(item.get("extracted_quantity"), "Quantity", minimum=0.0), 2)
                for item, price, _catalog, _override, _overridden in frozen_lines),
            2,
        )
        discount = _money_amount(order["discount"], "Discount")
        if discount > subtotal + 1e-9:
            raise ValueError("Discount cannot exceed the approved order subtotal")
        grand_total = round(subtotal - discount, 2)

        for item, effective_price, catalog_price, price_override, price_overridden in frozen_lines:
            cursor.execute(
                """
                INSERT INTO approved_order_items (
                    order_id, original_item_id, product_id, product_name, quantity,
                    unit, price, confidence, model_recommendation_id, model_decision,
                    human_decision, human_selected_sku, human_actor, human_confirmed_at,
                    review_action_id, approval_actor, approval_action_id,
                    provenance_verified, is_manually_corrected, is_human_confirmed,
                    catalog_price, price_override, price_overridden,
                    price_override_actor, price_overridden_at, price_override_action_id,
                    approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SELECT', ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["id"],
                    item["matched_product_id"],
                    item["matched_product_name"],
                    item["extracted_quantity"],
                    item["extracted_unit"],
                    effective_price,
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
                    catalog_price,
                    price_override,
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
                subtotal,
                discount,
                grand_total,
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
                    "subtotal": subtotal,
                    "discount": discount,
                    "grand_total": grand_total,
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
    if action_type == "manual_item_added":
        try:
            expected = _canonical_action_payload(
                {
                    "selected_sku": item["product_id"],
                    "quantity": _finite_number(item["quantity"], "Manual quantity", minimum=0.000000001),
                    "unit": _require_text(item["unit"], "Manual unit"),
                }
            )
        except ValueError:
            return False
        if review_action["payload_json"] != expected:
            return False

    overridden = bool(item.get("price_overridden"))
    catalog = item.get("catalog_price")
    if catalog is None:
        catalog = item.get("price")
    try:
        catalog_price = _finite_number(catalog, "Snapshot catalog price", minimum=0.0)
        effective = _finite_number(item.get("price"), "Snapshot price", minimum=0.0)
    except ValueError:
        return False
    if not overridden:
        return item.get("price_override") is None and abs(effective - catalog_price) < 1e-9

    override = item.get("price_override")
    override_action_id = item.get("price_override_action_id")
    override_actor = item.get("price_override_actor")
    override_time = item.get("price_overridden_at")
    if override is None or not override_action_id or not override_actor or not override_time:
        return False
    try:
        amount = _money_amount(override, "Price")
    except ValueError:
        return False
    if abs(effective - amount) >= 1e-9:
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
        and override_action["payload_json"] == _canonical_action_payload({"price": amount})
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
