"""Acceptance tests for customer data and pre-approval order flexibility.

These tests deliberately exercise the new write paths through HTTP.  They are
not merely CRUD tests: a manual line, cancellation, negotiated price, and
discount all sit beside the human-approval provenance chain and must therefore
remain safe under retries and concurrent requests.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import database
from app.main import create_app
from app.models import ExtractedItem, ExtractionResult
from tests.helpers import login

ACTOR = "flex-reviewer"
ALPHA_SKU = "A100"
BETA_SKU = "B200"
MANUAL_SKU = "C300"

CATALOG_CSV = b"""product_id,product_name,unit,price
A100,Alpha Wash,piece,10.00
B200,Beta Cancel,box,25.00
C300,Manual Add,piece,30.00
"""

REPRICED_CATALOG_CSV = b"""product_id,product_name,unit,price
A100,Alpha Changed,piece,999.00
B200,Beta Cancel,box,25.00
C300,Manual Add,piece,30.00
"""


class FlexExtractor:
    """A predictable two-line order, leaving all final decisions to the human."""

    def extract(self, _message: str) -> ExtractionResult:
        return ExtractionResult(
            items=[
                ExtractedItem(
                    raw_text="two alpha",
                    product_description="Alpha Wash",
                    quantity=2.0,
                    unit="piece",
                ),
                ExtractedItem(
                    raw_text="one beta",
                    product_description="Beta Cancel",
                    quantity=1.0,
                    unit="box",
                ),
            ],
            unresolved_text=[],
        )


@pytest.fixture
def flexible_order(tmp_path, monkeypatch):
    """One app, one isolated SQLite database, and a three-SKU catalog."""

    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    monkeypatch.setattr(main_module, "extractor", FlexExtractor())
    app = create_app(db_path)
    with TestClient(app) as client:
        login(client, ACTOR)
        uploaded = client.post(
            "/api/catalog/upload",
            files={"file": ("catalog.csv", CATALOG_CSV, "text/csv")},
        )
        assert uploaded.status_code == 200, uploaded.text
        yield client, db_path, app


def _new_order(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/process", json={"message": "flexibility test"})
    assert response.status_code == 200, response.text
    order = response.json()
    assert len(order["items"]) == 2
    return order


def _review_selected(
    client: TestClient,
    order_id: int,
    item_id: int,
    sku: str,
    quantity: float,
    unit: str,
    action_id: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/orders/{order_id}/items/{item_id}/review",
        json={
            "actor": ACTOR,
            "action_id": action_id,
            "final_decision": "SELECT",
            "selected_sku": sku,
            "quantity": quantity,
            "unit": unit,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _review_not_found(
    client: TestClient, order_id: int, item_id: int, action_id: str
) -> dict[str, Any]:
    response = client.post(
        f"/api/orders/{order_id}/items/{item_id}/review",
        json={
            "actor": ACTOR,
            "action_id": action_id,
            "final_decision": "NOT_FOUND",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _review_all_selected(client: TestClient) -> tuple[int, int, int]:
    order = _new_order(client)
    order_id = order["order_id"]
    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(
        client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"select-alpha-{order_id}"
    )
    _review_selected(
        client, order_id, beta_id, BETA_SKU, 1.0, "box", f"select-beta-{order_id}"
    )
    return order_id, alpha_id, beta_id


def _approve(client: TestClient, order_id: int, action_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/orders/{order_id}/approve",
        json={"actor": ACTOR, "action_id": action_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _item(order: dict[str, Any], item_id: int) -> dict[str, Any]:
    return next(item for item in order["items"] if item["id"] == item_id)


def _audit_with_action(client: TestClient, order_id: int, action_id: str) -> dict[str, Any]:
    response = client.get(f"/api/orders/{order_id}/audit")
    assert response.status_code == 200, response.text
    return next(event for event in response.json() if event["action_id"] == action_id)


def _json_request(
    client: TestClient, method: str, path: str, payload: dict[str, Any]
):
    """Issue JSON for every verb, including DELETE on older TestClient versions."""

    return client.request(method.upper(), path, json=payload)


def _thread_request(
    app,
    barrier: Barrier,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, str]:
    """Use a separate browser session per worker, sharing only app + database.

    The fixture keeps the application's lifespan open.  A bare TestClient here
    therefore gives each racing operator its own session cookie without also
    racing application startup and catalog seeding.
    """

    client = TestClient(app)
    try:
        login(client, ACTOR)
        barrier.wait(timeout=10)
        response = _json_request(client, method, path, payload)
        return response.status_code, response.text
    finally:
        client.close()


def _race(app, calls: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[int, str]]:
    barrier = Barrier(len(calls))
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [
            pool.submit(_thread_request, app, barrier, method, path, payload)
            for method, path, payload in calls
        ]
        results = [future.result(timeout=20) for future in futures]
    for status_code, text in results:
        assert status_code < 500, text
        assert "database is locked" not in text.lower(), text
    return results


def test_customer_details_are_session_bound_searchable_and_printed(flexible_order):
    client, _db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    payload = {
        "actor": ACTOR,
        "action_id": f"customer-{order_id}",
        "customer_name": "Nile Grocers",
        "customer_phone": "01012345678",
        "customer_address": "12 Market Street",
    }

    forged = client.put(
        f"/api/orders/{order_id}/customer",
        json={**payload, "actor": "forged-operator"},
    )
    assert forged.status_code == 403, forged.text

    updated = client.put(f"/api/orders/{order_id}/customer", json=payload)
    assert updated.status_code == 200, updated.text
    for field, expected in payload.items():
        if field not in {"actor", "action_id"}:
            assert updated.json()[field] == expected

    event = _audit_with_action(client, order_id, payload["action_id"])
    assert event["actor"] == ACTOR

    for query in ("Nile Grocers", "012345678"):
        found = client.get("/api/orders", params={"search": query})
        assert found.status_code == 200, found.text
        summary = next(row for row in found.json() if row["order_id"] == order_id)
        assert summary["customer_name"] == "Nile Grocers"
        assert summary["customer_phone"] == "01012345678"

    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"customer-a-{order_id}")
    _review_not_found(client, order_id, beta_id, f"customer-b-{order_id}")
    _approve(client, order_id, f"customer-approve-{order_id}")
    document = client.get(f"/api/orders/{order_id}/document")
    assert document.status_code == 200, document.text
    for value in ("Nile Grocers", "01012345678", "12 Market Street"):
        assert value in document.text


def test_customer_values_are_html_escaped_and_csv_safe(flexible_order):
    client, _db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    customer_name = '<img src=x onerror="alert(1)">'
    customer_phone = "=2+2"
    customer_address = "+unsafe-address"
    response = client.put(
        f"/api/orders/{order_id}/customer",
        json={
            "actor": ACTOR,
            "action_id": f"customer-escape-{order_id}",
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_address": customer_address,
        },
    )
    assert response.status_code == 200, response.text
    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"escape-a-{order_id}")
    _review_not_found(client, order_id, beta_id, f"escape-b-{order_id}")
    _approve(client, order_id, f"escape-approve-{order_id}")

    document = client.get(f"/api/orders/{order_id}/document")
    assert document.status_code == 200, document.text
    assert customer_name not in document.text
    assert "&lt;img" in document.text
    csv_text = client.post(f"/api/orders/{order_id}/export").content.decode("utf-8-sig")
    assert "'=2+2" in csv_text
    assert "'+unsafe-address" in csv_text


def test_manual_line_has_provenance_is_idempotent_and_cannot_bypass_approval(flexible_order):
    client, db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    action_id = f"manual-add-{order_id}"
    payload = {
        "actor": ACTOR,
        "action_id": action_id,
        "selected_sku": MANUAL_SKU,
        "quantity": 3.0,
        "unit": "piece",
    }

    added = client.post(f"/api/orders/{order_id}/items", json=payload)
    assert added.status_code == 200, added.text
    manual = next(item for item in added.json()["items"] if item["is_manual_line"])
    manual_id = manual["id"]
    assert manual["recommendation_decision"] == "MANUAL"
    assert manual["confidence"] == pytest.approx(0.0)
    assert manual["human_decision"] == "SELECT"
    assert manual["is_human_confirmed"] is True
    assert manual["human_actor"] == ACTOR
    assert manual["review_action_id"] == action_id

    repeated = client.post(f"/api/orders/{order_id}/items", json=payload)
    assert repeated.status_code == 200, repeated.text
    assert sum(item["is_manual_line"] for item in repeated.json()["items"]) == 1
    conflict = client.post(
        f"/api/orders/{order_id}/items", json={**payload, "quantity": 4.0}
    )
    assert conflict.status_code == 400, conflict.text

    action = None
    with database.get_connection(db_path) as conn:
        action = conn.execute(
            "SELECT * FROM human_actions WHERE action_id=?", (action_id,)
        ).fetchone()
    assert action is not None
    assert action["order_id"] == order_id
    assert action["item_id"] == manual_id
    assert action["actor"] == ACTOR
    audit = _audit_with_action(client, order_id, action_id)
    assert audit["event_type"] == "human_added_line"
    assert audit["actor"] == ACTOR

    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"manual-a-{order_id}")
    _review_not_found(client, order_id, beta_id, f"manual-b-{order_id}")
    snapshot = _approve(client, order_id, f"manual-approve-{order_id}")["snapshot"]
    frozen_manual = next(row for row in snapshot if row["original_item_id"] == manual_id)
    assert frozen_manual["product_id"] == MANUAL_SKU
    assert frozen_manual["is_human_confirmed"] is True
    assert frozen_manual["human_decision"] == "SELECT"
    assert frozen_manual["review_action_id"] == action_id

    # An action record alone is not enough: removing the explicit decision
    # must make approval fail closed.  Resolve every model line first so the
    # rejected manual line is the only reason approval cannot proceed.
    unsafe = _new_order(client)
    unsafe_order_id = unsafe["order_id"]
    unsafe_payload = {
        "actor": ACTOR,
        "action_id": f"unsafe-manual-{unsafe_order_id}",
        "selected_sku": MANUAL_SKU,
        "quantity": 1.0,
        "unit": "piece",
    }
    unsafe_added = client.post(f"/api/orders/{unsafe_order_id}/items", json=unsafe_payload)
    assert unsafe_added.status_code == 200, unsafe_added.text
    unsafe_manual_id = next(
        item["id"] for item in unsafe_added.json()["items"] if item["is_manual_line"]
    )
    for item in unsafe["items"]:
        _review_not_found(client, unsafe_order_id, item["id"], f"unsafe-nf-{item['id']}")
    with database.get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE order_items
            SET is_human_confirmed=0, human_decision=NULL, human_selected_sku=NULL
            WHERE id=?
            """,
            (unsafe_manual_id,),
        )
        conn.commit()
    rejected = client.post(
        f"/api/orders/{unsafe_order_id}/approve",
        json={"actor": ACTOR, "action_id": f"unsafe-approve-{unsafe_order_id}"},
    )
    assert rejected.status_code == 400, rejected.text
    assert database.get_order_by_id(unsafe_order_id, db_path)["approved_items"] == []


def test_cancelled_lines_are_retained_but_excluded_from_totals_document_and_csv(flexible_order):
    client, _db_path, _app = flexible_order
    order_id, alpha_id, beta_id = _review_all_selected(client)
    cancel_action = f"cancel-{order_id}"
    cancelled = _json_request(
        client,
        "delete",
        f"/api/orders/{order_id}/items/{beta_id}",
        {"actor": ACTOR, "action_id": cancel_action},
    )
    assert cancelled.status_code == 200, cancelled.text
    order = cancelled.json()
    beta = _item(order, beta_id)
    assert beta["status"] == "cancelled_by_human"
    assert beta["human_decision"] == "CANCELLED"
    assert beta["is_human_confirmed"] is True
    assert len(order["items"]) == 2, "cancellation must be soft, not a hard delete"
    assert order["total_items"] == 1
    assert order["total_confirmed"] == 1
    assert order["subtotal"] == pytest.approx(20.0)
    assert order["grand_total"] == pytest.approx(20.0)
    event = _audit_with_action(client, order_id, cancel_action)
    assert event["actor"] == ACTOR

    snapshot = _approve(client, order_id, f"cancel-approve-{order_id}")["snapshot"]
    assert [row["original_item_id"] for row in snapshot] == [alpha_id]
    assert beta_id not in {row["original_item_id"] for row in snapshot}

    document = client.get(f"/api/orders/{order_id}/document")
    assert document.status_code == 200, document.text
    assert "Alpha Wash" in document.text
    assert "Beta Cancel" not in document.text
    exported = client.post(f"/api/orders/{order_id}/export")
    assert exported.status_code == 200, exported.text
    csv_text = exported.content.decode("utf-8-sig")
    assert "Alpha Wash" in csv_text
    assert "Beta Cancel" not in csv_text

    locked = _json_request(
        client,
        "delete",
        f"/api/orders/{order_id}/items/{alpha_id}",
        {"actor": ACTOR, "action_id": f"cancel-after-approval-{order_id}"},
    )
    assert locked.status_code == 400, locked.text


def test_not_found_and_cancelled_are_distinct_final_states(flexible_order):
    client, _db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_not_found(client, order_id, alpha_id, f"not-found-{order_id}")
    cancelled = _json_request(
        client,
        "delete",
        f"/api/orders/{order_id}/items/{beta_id}",
        {"actor": ACTOR, "action_id": f"cancel-distinct-{order_id}"},
    )
    assert cancelled.status_code == 200, cancelled.text
    state = cancelled.json()
    not_found = _item(state, alpha_id)
    cancelled_line = _item(state, beta_id)
    assert (not_found["status"], not_found["human_decision"]) == (
        "not_found_confirmed",
        "NOT_FOUND",
    )
    assert (cancelled_line["status"], cancelled_line["human_decision"]) == (
        "cancelled_by_human",
        "CANCELLED",
    )


def test_price_override_is_audited_and_frozen_across_catalog_repricing(flexible_order):
    client, _db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"price-a-{order_id}")
    _review_not_found(client, order_id, beta_id, f"price-b-{order_id}")

    override_action = f"override-{order_id}"
    overridden = client.put(
        f"/api/orders/{order_id}/items/{alpha_id}/price",
        json={"actor": ACTOR, "action_id": override_action, "price": 7.5},
    )
    assert overridden.status_code == 200, overridden.text
    line = _item(overridden.json(), alpha_id)
    assert line["catalog_price"] == pytest.approx(10.0)
    assert line["price_override"] == pytest.approx(7.5)
    assert line["price_overridden"] is True
    assert line["matched_product"]["price"] == pytest.approx(7.5)
    audit = _audit_with_action(client, order_id, override_action)
    assert audit["event_type"] == "price_overridden"
    assert audit["actor"] == ACTOR

    snapshot = _approve(client, order_id, f"price-approve-{order_id}")["snapshot"]
    frozen = next(row for row in snapshot if row["original_item_id"] == alpha_id)
    assert frozen["catalog_price"] == pytest.approx(10.0)
    assert frozen["price_override"] == pytest.approx(7.5)
    assert frozen["price_overridden"] is True
    assert frozen["price"] == pytest.approx(7.5)

    reprice = client.post(
        "/api/catalog/upload",
        files={"file": ("repriced.csv", REPRICED_CATALOG_CSV, "text/csv")},
    )
    assert reprice.status_code == 200, reprice.text
    reloaded = client.get(f"/api/orders/{order_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert _item(reloaded.json(), alpha_id)["matched_product"]["price"] == pytest.approx(7.5)
    document = client.get(f"/api/orders/{order_id}/document")
    assert document.status_code == 200, document.text
    assert "7.50" in document.text
    assert "999.00" not in document.text


def test_discount_is_applied_once_and_the_document_uses_frozen_financials(flexible_order):
    client, db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    alpha_id, beta_id = (item["id"] for item in order["items"])
    _review_selected(client, order_id, alpha_id, ALPHA_SKU, 2.0, "piece", f"discount-a-{order_id}")
    _review_not_found(client, order_id, beta_id, f"discount-b-{order_id}")

    action_id = f"discount-{order_id}"
    payload = {"actor": ACTOR, "action_id": action_id, "discount": 3.5}
    discounted = client.put(f"/api/orders/{order_id}/discount", json=payload)
    assert discounted.status_code == 200, discounted.text
    assert discounted.json()["discount"] == pytest.approx(3.5)
    assert discounted.json()["subtotal"] == pytest.approx(20.0)
    assert discounted.json()["grand_total"] == pytest.approx(16.5)
    audit = _audit_with_action(client, order_id, action_id)
    assert audit["actor"] == ACTOR

    repeated = client.put(f"/api/orders/{order_id}/discount", json=payload)
    assert repeated.status_code == 200, repeated.text
    conflict = client.put(
        f"/api/orders/{order_id}/discount", json={**payload, "discount": 4.0}
    )
    assert conflict.status_code == 400, conflict.text

    _approve(client, order_id, f"discount-approve-{order_id}")
    approved = client.get(f"/api/orders/{order_id}")
    assert approved.status_code == 200, approved.text
    assert approved.json()["discount"] == pytest.approx(3.5)
    assert approved.json()["subtotal"] == pytest.approx(20.0)
    assert approved.json()["grand_total"] == pytest.approx(16.5)

    # A later write to a live order column must not rewrite the approved note.
    # The approved financial snapshot is the source of truth for the document.
    with database.get_connection(db_path) as conn:
        conn.execute("UPDATE orders SET discount=0 WHERE id=?", (order_id,))
        conn.commit()
    document = client.get(f"/api/orders/{order_id}/document")
    assert document.status_code == 200, document.text
    assert "3.50" in document.text
    assert "16.50" in document.text


def test_new_mutations_refuse_forged_actors(flexible_order):
    client, _db_path, _app = flexible_order
    order = _new_order(client)
    order_id = order["order_id"]
    alpha_id, beta_id = (item["id"] for item in order["items"])

    requests = [
        (
            "post",
            f"/api/orders/{order_id}/items",
            {
                "actor": "forged-operator",
                "action_id": f"forged-add-{order_id}",
                "selected_sku": MANUAL_SKU,
                "quantity": 1.0,
                "unit": "piece",
            },
        ),
        (
            "delete",
            f"/api/orders/{order_id}/items/{beta_id}",
            {"actor": "forged-operator", "action_id": f"forged-cancel-{order_id}"},
        ),
        (
            "put",
            f"/api/orders/{order_id}/items/{alpha_id}/price",
            {"actor": "forged-operator", "action_id": f"forged-price-{order_id}", "price": 7.5},
        ),
        (
            "put",
            f"/api/orders/{order_id}/discount",
            {"actor": "forged-operator", "action_id": f"forged-discount-{order_id}", "discount": 3.5},
        ),
    ]
    for method, path, payload in requests:
        response = _json_request(client, method, path, payload)
        assert response.status_code == 403, f"{method.upper()} {path}: {response.text}"

    unchanged = client.get(f"/api/orders/{order_id}").json()
    assert len(unchanged["items"]) == 2
    assert _item(unchanged, beta_id)["status"] != "cancelled_by_human"
    assert unchanged["discount"] == pytest.approx(0.0)


def test_concurrent_flexibility_writes_are_serializable_and_leave_no_partial_state(flexible_order):
    client, db_path, app = flexible_order

    # Add-line vs approve: either command may win, but a successful add must
    # appear exactly once in the snapshot and a rejected add must leave no row.
    order_id, alpha_id, beta_id = _review_all_selected(client)
    add_action = f"race-add-{order_id}"
    approve_action = f"race-approve-{order_id}"
    add_result, approve_result = _race(
        app,
        [
            (
                "post",
                f"/api/orders/{order_id}/items",
                {
                    "actor": ACTOR,
                    "action_id": add_action,
                    "selected_sku": MANUAL_SKU,
                    "quantity": 1.0,
                    "unit": "piece",
                },
            ),
            (
                "post",
                f"/api/orders/{order_id}/approve",
                {"actor": ACTOR, "action_id": approve_action},
            ),
        ],
    )
    assert add_result[0] in {200, 400}
    assert approve_result[0] == 200
    raced = database.get_order_by_id(order_id, db_path)
    assert raced["status"] == "approved"
    snapshot_ids = [row["original_item_id"] for row in raced["approved_items"]]
    assert len(snapshot_ids) == len(set(snapshot_ids))
    manual_rows = [item for item in raced["items"] if item.get("recommendation_decision") == "MANUAL"]
    if add_result[0] == 200:
        assert len(manual_rows) == 1
        assert manual_rows[0]["id"] in snapshot_ids
    else:
        assert manual_rows == []

    # Cancel vs approve: whichever commits first determines whether Beta is in
    # the one immutable snapshot; neither path may half-cancel or double-copy.
    order_id, alpha_id, beta_id = _review_all_selected(client)
    cancel_action = f"race-cancel-{order_id}"
    cancel_result, approve_result = _race(
        app,
        [
            (
                "delete",
                f"/api/orders/{order_id}/items/{beta_id}",
                {"actor": ACTOR, "action_id": cancel_action},
            ),
            (
                "post",
                f"/api/orders/{order_id}/approve",
                {"actor": ACTOR, "action_id": f"race-cancel-approve-{order_id}"},
            ),
        ],
    )
    assert cancel_result[0] in {200, 400}
    assert approve_result[0] == 200
    raced = database.get_order_by_id(order_id, db_path)
    snapshot_ids = [row["original_item_id"] for row in raced["approved_items"]]
    assert len(snapshot_ids) == len(set(snapshot_ids))
    beta = next(item for item in raced["items"] if item["id"] == beta_id)
    if cancel_result[0] == 200:
        assert beta["status"] == "cancelled_by_human"
        assert beta_id not in snapshot_ids
    else:
        assert beta["status"] == "human_selected"
        assert beta_id in snapshot_ids

    # Two valid price commands serialize as two auditable decisions; the last
    # committed one wins, and that one is what approval freezes.
    order_id, alpha_id, beta_id = _review_all_selected(client)
    first_action = f"race-price-one-{order_id}"
    second_action = f"race-price-two-{order_id}"
    override_results = _race(
        app,
        [
            (
                "put",
                f"/api/orders/{order_id}/items/{alpha_id}/price",
                {"actor": ACTOR, "action_id": first_action, "price": 7.5},
            ),
            (
                "put",
                f"/api/orders/{order_id}/items/{alpha_id}/price",
                {"actor": ACTOR, "action_id": second_action, "price": 8.5},
            ),
        ],
    )
    assert [result[0] for result in override_results] == [200, 200]
    raced = database.get_order_by_id(order_id, db_path)
    alpha = next(item for item in raced["items"] if item["id"] == alpha_id)
    assert alpha["price_override"] in {7.5, 8.5}
    price_events = [
        event
        for event in raced["audit_events"]
        if event["action_id"] in {first_action, second_action}
    ]
    assert {event["action_id"] for event in price_events} == {first_action, second_action}
    approved = _approve(client, order_id, f"race-price-approve-{order_id}")
    frozen = next(row for row in approved["snapshot"] if row["original_item_id"] == alpha_id)
    assert frozen["price"] == pytest.approx(alpha["price_override"])

    # Two different approval action IDs racing must still create one snapshot
    # per source line and one order_approved audit event.
    order_id, alpha_id, beta_id = _review_all_selected(client)
    approvals = _race(
        app,
        [
            (
                "post",
                f"/api/orders/{order_id}/approve",
                {"actor": ACTOR, "action_id": f"race-approve-one-{order_id}"},
            ),
            (
                "post",
                f"/api/orders/{order_id}/approve",
                {"actor": ACTOR, "action_id": f"race-approve-two-{order_id}"},
            ),
        ],
    )
    # A different id is a different command, not an idempotent replay.  One
    # wins and seals the order; the other must be rejected rather than quietly
    # being accepted as if it represented the first operator action.
    approval_statuses = [result[0] for result in approvals]
    assert sorted(approval_statuses) == [200, 400]
    raced = database.get_order_by_id(order_id, db_path)
    snapshot_ids = [row["original_item_id"] for row in raced["approved_items"]]
    assert sorted(snapshot_ids) == sorted([alpha_id, beta_id])
    assert len(snapshot_ids) == len(set(snapshot_ids))
    approved_events = [event for event in raced["audit_events"] if event["event_type"] == "order_approved"]
    assert len(approved_events) == 1
