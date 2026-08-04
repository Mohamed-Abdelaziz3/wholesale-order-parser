"""Adversarial P0/P1 recovery tests at API and persistence boundaries."""

import json

import pytest
from fastapi.testclient import TestClient

from app import database
from app.catalog import load_catalog
from app.main import create_app
from tests.helpers import login


def model_item(**overrides):
    item = {
        "raw_text": "flash lemon",
        "extracted_product": "flash lemon",
        "extracted_quantity": 2.0,
        "extracted_unit": "piece",
        "matched_product": None,
        "recommendation_product": {
            "product_id": "CL010",
            "product_name": "Flash Lemon",
            "unit": "piece",
            "price": 10.0,
        },
        "recommendation_decision": "SELECT",
        "confidence": 0.62,
        "status": "needs_review",
        "candidates": [],
    }
    item.update(overrides)
    return item


def seed_order(db_path):
    return database.save_processed_order(
        "test",
        [model_item()],
        [],
        1.0,
        db_path,
    )


def review_command(action="review-1", actor="reviewer"):
    return {
        "actor": actor,
        "action_id": action,
        "final_decision": "SELECT",
        "selected_sku": "CL010",
        "quantity": 2.0,
        "unit": "piece",
    }


@pytest.fixture
def recovery(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    app = create_app(db_path)
    with TestClient(app) as client:
        login(client, "reviewer")
        yield client, db_path


@pytest.mark.parametrize(
    "unsafe_item",
    [
        model_item(
            matched_product={
                "product_id": "CL010",
                "product_name": "Flash Lemon",
                "unit": "piece",
                "price": 10.0,
            }
        ),
        model_item(is_human_confirmed=True),
        model_item(status="confirmed"),
        model_item(status="approved"),
        model_item(human_actor="fabricated"),
    ],
)
def test_model_ingestion_cannot_mint_human_or_approved_state(tmp_path, unsafe_item):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    with pytest.raises(ValueError):
        database.save_processed_order("unsafe", [unsafe_item], [], 1.0, db_path)
    with database.get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_ui_contract_payload_reaches_audit_snapshot_and_export(recovery):
    client, db_path = recovery
    page = client.get("/")
    assert page.status_code == 200
    for token in (
        "Human reviewer identity (required)",
        "final_decision",
        "selected_sku",
        "action_id",
        "/review",
    ):
        assert token in page.text

    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    review = client.post(
        f"/api/orders/{order_id}/items/{item_id}/review",
        json=review_command(),
    )
    assert review.status_code == 200, review.text
    login(client, "approver")
    approval = client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": "approver", "action_id": "approve-1"},
    )
    assert approval.status_code == 200, approval.text
    snapshot = approval.json()["snapshot"][0]
    assert snapshot["product_id"] == "CL010"
    assert snapshot["confidence"] == pytest.approx(0.62)
    assert snapshot["human_actor"] == "reviewer"
    assert snapshot["approval_actor"] == "approver"
    exported = client.post(f"/api/orders/{order_id}/export")
    assert exported.status_code == 200
    assert "62%" in exported.content.decode("utf-8-sig")
    events = database.get_order_by_id(order_id, db_path)["audit_events"]
    assert any(event["event_type"] == "human_selected_final_sku" for event in events)
    assert any(event["event_type"] == "order_approved" for event in events)
    assert any(event["event_type"] == "order_exported" for event in events)


@pytest.mark.parametrize("missing", ["actor", "final_decision"])
def test_missing_human_intent_fails_with_422(recovery, missing):
    client, db_path = recovery
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    payload = review_command()
    payload.pop(missing)
    response = client.post(
        f"/api/orders/{order_id}/items/{item_id}/review",
        json=payload,
    )
    assert response.status_code == 422
    assert not database.get_order_by_id(order_id, db_path)["items"][0]["is_human_confirmed"]


def test_duplicate_review_is_idempotent_and_conflict_is_rejected(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}
    kwargs = dict(
        actor="reviewer",
        action_id="stable-action",
        final_decision="SELECT",
        selected_sku="CL010",
        quantity=2,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    database.review_order_item(order_id, item_id, **kwargs)
    database.review_order_item(order_id, item_id, **kwargs)
    events = database.get_order_by_id(order_id, db_path)["audit_events"]
    assert len([event for event in events if event["event_type"] == "human_selected_final_sku"]) == 1
    with pytest.raises(ValueError, match="already used"):
        database.review_order_item(
            order_id,
            item_id,
            **{**kwargs, "selected_sku": "CL009"},
        )


def test_review_audit_failure_rolls_back_action_and_item(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}

    def fail_audit(*args, **kwargs):
        raise sqlite_failure

    sqlite_failure = RuntimeError("audit unavailable")
    monkeypatch.setattr(database, "log_audit_event_tx", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        database.review_order_item(
            order_id,
            item_id,
            actor="reviewer",
            action_id="rollback-review",
            final_decision="SELECT",
            selected_sku="CL010",
            quantity=2,
            unit="piece",
            catalog_lookup=catalog,
            db_path=db_path,
        )
    with database.get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM human_actions").fetchone()[0] == 0
    assert not database.get_order_by_id(order_id, db_path)["items"][0]["is_human_confirmed"]


def test_approval_audit_failure_rolls_back_snapshot_and_status(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}
    database.review_order_item(
        order_id,
        item_id,
        actor="reviewer",
        action_id="review-before-failure",
        final_decision="SELECT",
        selected_sku="CL010",
        quantity=2,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    original_logger = database.log_audit_event_tx

    def fail_approval_audit(cursor, order, event_type, *args, **kwargs):
        if event_type == "order_approved":
            raise RuntimeError("approval audit unavailable")
        return original_logger(cursor, order, event_type, *args, **kwargs)

    monkeypatch.setattr(database, "log_audit_event_tx", fail_approval_audit)
    with pytest.raises(RuntimeError, match="approval audit unavailable"):
        database.approve_order(
            order_id,
            actor="approver",
            action_id="rollback-approval",
            db_path=db_path,
        )
    order = database.get_order_by_id(order_id, db_path)
    assert order["status"] == "analyzed"
    assert order["approved_items"] == []
    with database.get_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM human_actions WHERE action_type='order_approval'"
        ).fetchone()[0] == 0


def test_export_rejects_tampered_provenance(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}
    database.review_order_item(
        order_id,
        item_id,
        actor="reviewer",
        action_id="review-tamper",
        final_decision="SELECT",
        selected_sku="CL010",
        quantity=2,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    database.approve_order(
        order_id,
        actor="approver",
        action_id="approve-tamper",
        db_path=db_path,
    )
    with database.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE approved_order_items SET approval_actor=NULL WHERE order_id=?",
            (order_id,),
        )
        conn.commit()
    with pytest.raises(ValueError, match="lacks verified human provenance"):
        database.record_export(order_id, db_path)


def test_later_model_output_cannot_overwrite_approved_snapshot(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = seed_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}
    database.review_order_item(
        order_id,
        item_id,
        actor="reviewer",
        action_id="review-stable",
        final_decision="SELECT",
        selected_sku="CL010",
        quantity=2,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    approved = database.approve_order(
        order_id,
        actor="approver",
        action_id="approve-stable",
        db_path=db_path,
    )
    before = json.dumps(approved["snapshot"], sort_keys=True)
    new_order_id = database.save_processed_order(
        "later model output",
        [model_item(confidence=0.99)],
        [],
        1.0,
        db_path,
    )
    assert new_order_id != order_id
    after = database.get_order_by_id(order_id, db_path)["approved_items"]
    assert json.dumps(after, sort_keys=True) == before
