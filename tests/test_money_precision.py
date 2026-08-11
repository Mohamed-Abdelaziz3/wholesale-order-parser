"""Pilot-grade exact-money and SQLite migration coverage."""

import sqlite3
from decimal import Decimal

import pytest

from app import database
from app.models import CatalogProduct
from app.money import line_total, money_sum, parse_money, to_storage


def test_decimal_rounding_and_repeated_addition_are_exact():
    assert to_storage(money_sum(["0.10", "0.20"])) == "0.30"
    assert to_storage(money_sum(["0.01"] * 1000)) == "10.00"
    # 19.99 × 2.5 is exactly 49.975, which the pilot policy rounds HALF_UP.
    assert to_storage(line_total("19.99", "2.5")) == "49.98"
    assert to_storage(
        money_sum(
            [line_total("19.99", "2.5"), line_total("0.10", "3"), line_total("0.20", "1")]
        )
    ) == "50.48"
    assert to_storage(parse_money("-0", "Price")) == "0.00"


@pytest.mark.parametrize("value", ["1.001", "-0.01", "NaN", "not-money"])
def test_new_money_input_rejects_ambiguous_or_invalid_values(value):
    with pytest.raises(ValueError):
        parse_money(value, "Price")


def test_fresh_database_uses_text_for_every_commercial_amount(tmp_path):
    db_path = tmp_path / "money.db"
    database.init_db(db_path)
    expected = {
        "catalog_products": {"price"},
        "orders": {"discount"},
        "order_items": {"matched_price", "recommendation_price", "catalog_price", "price_override"},
        "approved_order_items": {"price", "catalog_price", "price_override", "line_total"},
        "approved_order_financials": {"subtotal", "discount", "grand_total"},
    }
    with sqlite3.connect(db_path) as conn:
        for table, fields in expected.items():
            types = {row[1]: row[2].upper() for row in conn.execute(f"PRAGMA table_info({table})")}
            assert {types[field] for field in fields} == {"TEXT"}


def test_legacy_real_money_is_reported_then_preserved_without_rounding(tmp_path):
    db_path = tmp_path / "legacy-real.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE catalog_products (
                product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0.0, sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO catalog_products VALUES ('LEGACY', 'Legacy price', '', 'piece', 19.995, 0, '2026-01-01')"
        )
        conn.commit()

    preflight = database.preflight_money_migration(db_path)
    assert preflight["needs_migration"] is True
    assert any(row["value"] == "19.995" for row in preflight["legacy_values_over_two_places"])

    database.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT price FROM catalog_products WHERE product_id='LEGACY'").fetchone()[0]
        column = next(row for row in conn.execute("PRAGMA table_info(catalog_products)") if row[1] == "price")
    assert stored == "19.995"  # migration did not silently turn it into 20.00
    assert column[2].upper() == "TEXT"
    assert Decimal(stored) == Decimal("19.995")


def test_historical_higher_scale_override_remains_exportable_with_its_evidence(tmp_path):
    """A pre-pilot immutable snapshot is readable, not revalidated as new input."""
    db_path = tmp_path / "historical-approved.db"
    database.init_db(db_path)
    order_id = database.save_processed_order(
        "synthetic legacy order",
        [
            {
                "raw_text": "1 piece product",
                "extracted_product": "Product",
                "extracted_quantity": 1,
                "extracted_unit": "piece",
                "recommendation_product": {
                    "product_id": "SKU-1",
                    "product_name": "Product",
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
        "SKU-1": CatalogProduct(
            product_id="SKU-1",
            product_name="Product",
            aliases=[],
            unit="piece",
            price="10.00",
        )
    }
    database.review_order_item(
        order_id,
        item_id,
        actor="operator",
        action_id="review-historical-override",
        final_decision="SELECT",
        selected_sku="SKU-1",
        quantity=1,
        unit="piece",
        catalog_lookup=lookup,
        db_path=db_path,
    )
    database.override_order_item_price(
        order_id,
        item_id,
        actor="operator",
        action_id="override-historical-override",
        price="19.99",
        db_path=db_path,
    )
    database.approve_order(
        order_id,
        actor="operator",
        action_id="approve-historical-override",
        db_path=db_path,
    )

    # Simulate a snapshot/action record that was legitimately created before
    # the current two-decimal ingress invariant.  It is frozen evidence, not a
    # new operator command, so exporting it must not turn its 19.995 into 20.00
    # or reject it for being historically higher-scale.
    with database.session(db_path, immediate=True) as conn:
        conn.execute(
            """
            UPDATE approved_order_items
            SET price='19.995', price_override='19.995'
            WHERE order_id=?
            """,
            (order_id,),
        )
        conn.execute(
            """
            UPDATE human_actions SET payload_json=?
            WHERE action_id='override-historical-override'
            """,
            ('{"price":"19.995"}',),
        )

    exported = database.record_export(order_id, db_path)
    assert exported["snapshot"][0]["price"] == "19.995"
    assert exported["snapshot"][0]["price_override"] == "19.995"
