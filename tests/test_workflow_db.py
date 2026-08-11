"""Offline API workflow tests using one explicit temporary database per test."""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import database
from app.catalog import load_catalog
from app.extractor import MockExtractor
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from tests.helpers import login, process_payload

MESSAGE = "ابعتلي 6 فلاش ليمون"
PREDEFINED = {
    MESSAGE: ExtractionResult(
        items=[
            ExtractedItem(
                raw_text="6 فلاش ليمون",
                product_description="فلاش ليمون",
                quantity=6.0,
                unit="قطعة",
            )
        ]
    ),
    "عايز 5 مناديل سحرية فضائية": ExtractionResult(
        items=[
            ExtractedItem(
                raw_text="5 مناديل سحرية فضائية",
                product_description="مناديل سحرية فضائية",
                quantity=5.0,
                unit="قطعة",
            )
        ]
    ),
    "عايز تمن باكو اكواب فوم كبيره": ExtractionResult(
        items=[
            ExtractedItem(
                raw_text="تمن باكو اكواب فوم كبيره",
                product_description="اكواب فوم كبيره",
                quantity=8.0,
                unit="باكو",
            )
        ]
    ),
}


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", MockExtractor(predefined=PREDEFINED))
    app = create_app(db_path)
    with TestClient(app) as client:
        login(client, "workflow-reviewer")
        yield client, db_path


def review_payload(sku="CL010", quantity=6.0, unit="قطعة", action="review-1"):
    return {
        "actor": "workflow-reviewer",
        "action_id": action,
        "final_decision": "SELECT",
        "selected_sku": sku,
        "quantity": quantity,
        "unit": unit,
    }


def approval_payload(action="approve-1"):
    return {"actor": "workflow-approver", "action_id": action}


def approve(client: TestClient, order_id, payload=None):
    """Approve as the approver identity.

    Actions are attributed to the authenticated session, so switching actor
    means switching session. The fixture signs back in as the reviewer
    afterwards so later review calls keep working.
    """
    payload = payload or approval_payload()
    login(client, payload["actor"])
    try:
        return client.post(f"/api/orders/{order_id}/approve", json=payload)
    finally:
        login(client, "workflow-reviewer")


def process_one(client: TestClient, message: str = MESSAGE):
    response = client.post("/api/process", json=process_payload(message))
    assert response.status_code == 200, response.text
    return response.json()


def test_analysis_persistence_and_audit(workflow):
    client, db_path = workflow
    result = process_one(client)
    order = database.get_order_by_id(result["order_id"], db_path)
    assert order["original_text"] == MESSAGE
    assert order["status"] == "needs_review"
    assert any(event["event_type"] == "analysis_completed" for event in order["audit_events"])


def test_item_review_and_actor_audit(workflow):
    client, _ = workflow
    result = process_one(client)
    response = client.post(
        f"/api/orders/{result['order_id']}/items/{result['items'][0]['id']}/review",
        json=review_payload(quantity=10.0, unit="قطعة"),
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    # The raw customer request is immutable evidence; the reviewer decision is
    # carried in dedicated final-commercial fields.
    assert item["extracted_quantity"] == 6.0
    assert item["final_quantity"] == 10.0
    assert item["human_actor"] == "workflow-reviewer"
    audit = client.get(f"/api/orders/{result['order_id']}/audit").json()
    selection = next(event for event in audit if event["event_type"] == "human_selected_final_sku")
    assert selection["actor"] == "workflow-reviewer"
    assert selection["action_id"] == "review-1"


@pytest.mark.parametrize(
    "payload",
    [
        review_payload(quantity=-5.0, action="bad-quantity"),
        review_payload(unit="   ", action="bad-unit"),
        {key: value for key, value in review_payload().items() if key != "actor"},
        {key: value for key, value in review_payload().items() if key != "final_decision"},
    ],
)
def test_review_validation_fails_closed(workflow, payload):
    client, _ = workflow
    result = process_one(client)
    response = client.post(
        f"/api/orders/{result['order_id']}/items/{result['items'][0]['id']}/review",
        json=payload,
    )
    assert response.status_code in (400, 422)


def test_unreviewed_order_cannot_approve(workflow):
    client, _ = workflow
    result = process_one(client, "عايز 5 مناديل سحرية فضائية")
    response = approve(client, result["order_id"])
    assert response.status_code == 400
    assert "unresolved items" in response.json()["detail"]


def test_review_approve_idempotency_and_edit_lock(workflow):
    client, db_path = workflow
    result = process_one(client, "عايز تمن باكو اكواب فوم كبيره")
    item_id = result["items"][0]["id"]
    review = client.post(
        f"/api/orders/{result['order_id']}/items/{item_id}/review",
        json=review_payload("PK017", 8.0, "باكو"),
    )
    assert review.status_code == 200, review.text
    command = approval_payload()
    first = approve(client, result["order_id"], command)
    second = approve(client, result["order_id"], command)
    assert first.status_code == second.status_code == 200
    assert first.json()["approved_at"] == second.json()["approved_at"]
    assert first.json()["snapshot"][0]["product_id"] == "PK017"
    order = database.get_order_by_id(result["order_id"], db_path)
    assert len([e for e in order["audit_events"] if e["event_type"] == "order_approved"]) == 1
    locked = client.post(
        f"/api/orders/{result['order_id']}/items/{item_id}/review",
        json=review_payload("PK017", 20.0, "باكو", "after-approval"),
    )
    assert locked.status_code == 400


def test_approved_only_csv_export_uses_snapshot(workflow):
    client, db_path = workflow
    result = process_one(client)
    order_id = result["order_id"]
    assert client.post(f"/api/orders/{order_id}/export").status_code == 400
    client.post(
        f"/api/orders/{order_id}/items/{result['items'][0]['id']}/review",
        json=review_payload(),
    )
    approve(client, order_id)

    # Mutating the working item directly must not alter approved export bytes.
    with database.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE order_items SET matched_product_id='CL009', matched_product_name='mutated'"
            " WHERE order_id=?",
            (order_id,),
        )
        conn.commit()
    exported = client.post(f"/api/orders/{order_id}/export")
    assert exported.status_code == 200, exported.text
    csv_text = exported.content.decode("utf-8-sig")
    assert "CL010" in csv_text
    assert "mutated" not in csv_text
    assert "Model confidence (advisory)" in csv_text


def test_catalog_is_read_only_and_no_inventory_is_deducted(workflow):
    client, _ = workflow
    catalog_path = Path(__file__).resolve().parents[1] / "catalog.csv"
    before = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    result = process_one(client)
    client.post(
        f"/api/orders/{result['order_id']}/items/{result['items'][0]['id']}/review",
        json=review_payload(),
    )
    approve(client, result["order_id"])
    client.post(f"/api/orders/{result['order_id']}/export")
    assert hashlib.sha256(catalog_path.read_bytes()).hexdigest() == before
    assert all(not hasattr(product, "stock_quantity") for product in load_catalog())
