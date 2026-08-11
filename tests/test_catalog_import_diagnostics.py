"""Pilot-grade catalog ingress validation and diagnostics."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.catalog as catalog_module
import app.main as main_module
from app import database
from app.catalog import CatalogError, parse_catalog_bytes
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from tests.helpers import login


class _NoopExtractor:
    def extract(self, message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text=message,
                    product_description="Widget",
                    quantity=1,
                    unit="piece",
                )
            ]
        )


@pytest.fixture
def catalog_client(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", _NoopExtractor())
    with TestClient(create_app(db_path)) as client:
        login(client, "catalog-operator")
        yield client


def test_catalog_parser_preserves_decimal_price_and_reports_mapping():
    parsed = parse_catalog_bytes(
        b"product_id,product_name,unit,price,merchant_note\n"
        b"SKU-1,Widget,piece,19.99,keep this local\n"
    )

    assert parsed.products[0].price == Decimal("19.99")
    assert isinstance(parsed.products[0].price, Decimal)
    assert parsed.detected_column_mapping == {
        "product_id": "product_id",
        "product_name": "product_name",
        "unit": "unit",
        "price": "price",
    }
    assert parsed.unmapped_source_columns == ["merchant_note"]
    assert parsed.diagnostics.model_dump() == {
        "data_row_count": 1,
        "missing_required_columns": [],
        "blank_row_count": 0,
        "malformed_row_count": 0,
        "duplicate_sku_count": 0,
        "missing_sku_count": 0,
        "missing_name_count": 0,
        "missing_price_count": 0,
        "invalid_price_count": 0,
        "missing_unit_count": 0,
        "row_limit_exceeded": False,
        "malformed_row_examples": [],
    }


def test_explicit_zero_catalog_price_is_valid_but_a_missing_price_is_not():
    parsed = parse_catalog_bytes(
        b"product_id,product_name,unit,price\nZERO,Free sample,piece,0\n"
    )

    assert parsed.products[0].price == Decimal("0.00")


@pytest.mark.parametrize(
    ("source", "diagnostic_field"),
    [
        (b"product_id,product_name,unit\nA,Widget,piece\n", "missing_required_columns"),
        (b"product_id,product_name,unit,price\nA,Widget,piece,\n", "missing_price_count"),
        (b"product_id,product_name,unit,price\nA,Widget,piece,free\n", "invalid_price_count"),
        (b"product_id,product_name,unit,price\nA,Widget,piece,-1.00\n", "invalid_price_count"),
        (b"product_id,product_name,unit,price\nA,Widget,piece,1.999\n", "invalid_price_count"),
        (b"product_id,product_name,unit,price\nA,Widget,,10.00\n", "missing_unit_count"),
    ],
)
def test_catalog_rejects_missing_or_noncanonical_commercial_input(source, diagnostic_field):
    with pytest.raises(CatalogError) as error:
        parse_catalog_bytes(source)

    diagnostics = error.value.diagnostics.model_dump()
    value = diagnostics[diagnostic_field]
    if diagnostic_field == "missing_required_columns":
        assert value == ["price"]
    else:
        assert value == 1
    assert "price" in str(error.value).lower() or "وحدة" in str(error.value)


def test_catalog_validation_collects_bounded_row_evidence_without_partial_import():
    source = (
        b"product_id,product_name,unit,price,operator_comment\n"
        b"A,One,piece,19.99,ok\n"
        b"A,Two,piece,-1.00,duplicate and negative\n"
        b"B,,piece,,missing name and price\n"
        b"C,Three,,10.001,missing unit and scale\n"
    )

    with pytest.raises(CatalogError) as error:
        parse_catalog_bytes(source)

    diagnostics = error.value.diagnostics
    assert diagnostics.data_row_count == 4
    assert diagnostics.malformed_row_count == 3
    assert diagnostics.duplicate_sku_count == 1
    assert diagnostics.missing_name_count == 1
    assert diagnostics.missing_price_count == 1
    assert diagnostics.invalid_price_count == 2
    assert diagnostics.missing_unit_count == 1
    assert [example.line_number for example in diagnostics.malformed_row_examples] == [3, 4, 5]
    assert error.value.unmapped_source_columns == ["operator_comment"]


def test_catalog_row_limit_is_rejected_instead_of_truncated(monkeypatch):
    monkeypatch.setattr(catalog_module, "MAX_CATALOG_ROWS", 2)
    source = (
        b"product_id,product_name,unit,price\n"
        b"A,One,piece,1\nB,Two,piece,2\nC,Three,piece,3\n"
    )

    with pytest.raises(CatalogError) as error:
        parse_catalog_bytes(source)

    assert error.value.diagnostics.row_limit_exceeded is True
    assert error.value.diagnostics.data_row_count == 3
    assert "row limit exceeded" in str(error.value)


def test_upload_surfaces_diagnostics_and_leaves_current_catalog_intact(catalog_client):
    before = catalog_client.get("/api/catalog").json()
    response = catalog_client.post(
        "/api/catalog/upload",
        files={
            "file": (
                "bad.csv",
                b"product_id,product_name,unit,price,ignored\n"
                b"A,One,piece,19.99,x\nA,Two,piece,broken,y\n",
                "text/csv",
            )
        },
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["detected_column_mapping"]["price"] == "price"
    assert body["unmapped_source_columns"] == ["ignored"]
    assert body["diagnostics"]["duplicate_sku_count"] == 1
    assert body["diagnostics"]["invalid_price_count"] == 1
    assert catalog_client.get("/api/catalog").json() == before


def test_upload_keeps_decimal_catalog_state_and_stable_numeric_api(catalog_client):
    response = catalog_client.post(
        "/api/catalog/upload",
        files={
            "file": (
                "good.csv",
                b"product_id,product_name,unit,price\nA,Widget,piece,0.10\n",
                "text/csv",
            )
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diagnostics"]["malformed_row_count"] == 0
    product = catalog_client.app.state.catalog_state.lookup["A"]
    assert product.price == Decimal("0.10")
    assert isinstance(product.price, Decimal)
    # Existing frontend/API clients continue receiving a JSON number only at
    # the response boundary; the catalog/matcher never calculates with it.
    assert catalog_client.get("/api/catalog").json()[0]["price"] == 0.1
