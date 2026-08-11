"""Fail-closed unit safety checks for the assisted pilot."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import database
from app.catalog import CatalogError, parse_catalog_bytes
from app.extractor import _build_result
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from app.units import (
    UNIT_CHECK_EQUIVALENT,
    UNIT_CHECK_HUMAN_OVERRIDE,
    UNIT_CHECK_MISMATCH,
    units_equivalent,
)
from tests.helpers import login, process_payload

CATALOG = b"""product_id,product_name,unit,price
P1,Widget,piece,10.00
C1,Carton Widget,carton,100.00
"""


class UnitExtractor:
    """A deterministic extraction fixture that preserves the requested unit."""

    def extract(self, message: str) -> ExtractionResult:
        units = {
            "carton order": "carton",
            "missing unit order": "",
            "unknown unit order": "bundle",
            "arabic plural order": "قطع",
            "english alias order": "pcs",
        }
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text=message,
                    product_description="Widget",
                    quantity=3.0,
                    unit=units[message],
                )
            ]
        )


@pytest.fixture
def unit_client(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", UnitExtractor())
    app = create_app(db_path)
    with TestClient(app) as client:
        login(client, "unit-reviewer")
        uploaded = client.post(
            "/api/catalog/upload",
            files={"file": ("catalog.csv", CATALOG, "text/csv")},
        )
        assert uploaded.status_code == 200, uploaded.text
        yield client, db_path


def _process(client: TestClient, message: str) -> dict:
    response = client.post("/api/process", json=process_payload(message))
    assert response.status_code == 200, response.text
    return response.json()


def _review_payload(item: dict, *, action_id: str, override: bool = False, note: str | None = None) -> dict:
    return {
        "actor": "unit-reviewer",
        "action_id": action_id,
        "final_decision": "SELECT",
        "selected_sku": "P1",
        "quantity": 3.0,
        # The reviewer always enters a quantity in the selected catalog unit.
        "unit": "piece",
        "unit_resolution": "HUMAN_OVERRIDE" if override else None,
        "unit_resolution_note": note,
    }


@pytest.mark.parametrize(
    ("requested", "catalog"),
    [
        ("قطعة", "قطع"),
        ("قطع", "قطعة"),
        ("pcs", "piece"),
        ("PIECES", "pc"),
        ("carton", "carton"),
    ],
)
def test_only_deterministic_aliases_are_equivalent(requested, catalog):
    assert units_equivalent(requested, catalog)


def test_unknown_or_packaging_units_are_not_converted():
    assert not units_equivalent("carton", "piece")
    assert not units_equivalent("bundle", "piece")
    assert not units_equivalent("dozen", "piece")


def test_arabic_piece_plural_and_english_alias_are_safe_equivalences(unit_client):
    client, _ = unit_client
    for number, message in enumerate(("arabic plural order", "english alias order"), start=1):
        order = _process(client, message)
        item = order["items"][0]
        assert item["unit_check_status"] == UNIT_CHECK_EQUIVALENT
        reviewed = client.post(
            f"/api/orders/{order['order_id']}/items/{item['id']}/review",
            json=_review_payload(item, action_id=f"safe-alias-{number}"),
        )
        assert reviewed.status_code == 200, reviewed.text
        saved = reviewed.json()["items"][0]
        assert saved["extracted_unit"] == item["extracted_unit"]
        assert saved["requested_unit"] == item["extracted_unit"]
        assert saved["final_unit"] == "piece"
        assert saved["unit_check_status"] == UNIT_CHECK_EQUIVALENT


def test_carton_vs_piece_requires_explicit_audited_override(unit_client):
    client, _ = unit_client
    order = _process(client, "carton order")
    item = order["items"][0]
    assert item["extracted_unit"] == "carton"
    assert item["requested_unit"] == "carton"
    assert item["unit_check_status"] == UNIT_CHECK_MISMATCH
    assert item["requires_unit_resolution"] is True

    rejected = client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json=_review_payload(item, action_id="carton-without-override"),
    )
    assert rejected.status_code == 400
    assert "does not convert units" in rejected.json()["detail"]

    reviewed = client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json=_review_payload(
            item,
            action_id="carton-explicit-override",
            override=True,
            note="Confirmed the final piece quantity with the customer.",
        ),
    )
    assert reviewed.status_code == 200, reviewed.text
    saved = reviewed.json()["items"][0]
    assert saved["extracted_unit"] == "carton"
    assert saved["requested_unit"] == "carton"
    assert saved["final_quantity"] == 3.0
    assert saved["final_unit"] == "piece"
    assert saved["unit_check_status"] == UNIT_CHECK_HUMAN_OVERRIDE
    assert saved["requires_unit_resolution"] is False

    audit = client.get(f"/api/orders/{order['order_id']}/audit")
    assert audit.status_code == 200
    resolution = next(event for event in audit.json() if event["event_type"] == "unit_mismatch_resolved")
    assert resolution["action_id"] == "carton-explicit-override"
    assert resolution["actor"] == "unit-reviewer"

    approved = client.post(
        f"/api/orders/{order['order_id']}/approve",
        json={"actor": "unit-reviewer", "action_id": "approve-carton-override"},
    )
    assert approved.status_code == 200, approved.text
    snapshot = approved.json()["snapshot"][0]
    assert snapshot["requested_unit"] == "carton"
    assert snapshot["catalog_unit"] == "piece"
    assert snapshot["unit_check_status"] == UNIT_CHECK_HUMAN_OVERRIDE
    assert snapshot["unit_resolution_note"] == "Confirmed the final piece quantity with the customer."

    exported = client.post(f"/api/orders/{order['order_id']}/export")
    assert exported.status_code == 200, exported.text
    assert b"P1" in exported.content


@pytest.mark.parametrize("message", ["missing unit order", "unknown unit order"])
def test_missing_or_unknown_requested_unit_requires_override(unit_client, message):
    client, _ = unit_client
    order = _process(client, message)
    item = order["items"][0]
    assert item["requires_unit_resolution"] is True
    rejected = client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json=_review_payload(item, action_id=f"{message}-rejected"),
    )
    assert rejected.status_code == 400
    accepted = client.post(
        f"/api/orders/{order['order_id']}/items/{item['id']}/review",
        json=_review_payload(
            item,
            action_id=f"{message}-override",
            override=True,
            note="Reviewer verified the final catalog-unit quantity.",
        ),
    )
    assert accepted.status_code == 200, accepted.text


def test_approval_is_blocked_while_a_unit_mismatch_is_unresolved(unit_client):
    client, _ = unit_client
    order = _process(client, "carton order")
    response = client.post(
        f"/api/orders/{order['order_id']}/approve",
        json={"actor": "unit-reviewer", "action_id": "approve-unresolved-unit"},
    )
    assert response.status_code == 400
    assert "unresolved" in response.json()["detail"].lower()


def test_extractor_and_catalog_do_not_default_missing_units_to_piece():
    extraction = _build_result(
        {"items": [{"raw_text": "3 widgets", "product_description": "Widget", "quantity": 3}]}
    )
    assert extraction.items[0].unit == ""
    with pytest.raises(CatalogError, match="unit"):
        parse_catalog_bytes(b"product_id,product_name,price\nP1,Widget,10.00\n")
