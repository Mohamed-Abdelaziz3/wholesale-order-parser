"""Regression coverage for mandatory human review and immutable evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from app import database
from app.catalog import load_catalog
from app.routing import route_rr_k_outcome, rr_k_auto_accept_enabled

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("decision", "confidence", "provider_error", "valid_output", "expected_decision"),
    [
        ("SELECT", 0.95, False, True, "SELECT"),
        ("SELECT", 0.85, False, True, "SELECT"),
        ("SELECT", 0.86, False, True, "SELECT"),
        ("SELECT", 0.60, False, True, "SELECT"),
        ("REVIEW", 0.50, False, True, "REVIEW"),
        ("NOT_FOUND", 0.95, False, True, "NOT_FOUND"),
        ("SELECT", None, True, True, "PROVIDER_ERROR"),
        ("unexpected", 0.90, False, False, "INVALID_OUTPUT"),
        ("SELECT", 1.2, False, True, "INVALID_OUTPUT"),
    ],
)
def test_every_outcome_is_mandatory_human_review(
    monkeypatch,
    decision,
    confidence,
    provider_error,
    valid_output,
    expected_decision,
):
    monkeypatch.setenv("RR_K_AUTO_ACCEPT_ENABLED", "true")
    route = route_rr_k_outcome(
        decision,
        confidence,
        provider_error=provider_error,
        valid_output=valid_output,
    )
    assert route.decision == expected_decision
    assert route.review_status == "needs_review"
    assert rr_k_auto_accept_enabled() is False


def create_recommended_order(db_path, *, confidence=0.62, decision="SELECT"):
    recommendation = {
        "product_id": "CL010",
        "product_name": "Flash Lemon",
        "unit": "piece",
        "price": 10.0,
    }
    return database.save_processed_order(
        "test order",
        [
            {
                "raw_text": "flash lemon",
                "extracted_product": "flash lemon",
                "extracted_quantity": 2.0,
                "extracted_unit": "piece",
                "recommendation_product": recommendation if decision == "SELECT" else None,
                "recommendation_decision": decision,
                "confidence": confidence,
                "status": "needs_review",
                "candidates": [recommendation],
            }
        ],
        [],
        1.0,
        db_path,
    )


@pytest.mark.parametrize(
    ("decision", "confidence"),
    [
        ("SELECT", 0.85),
        ("SELECT", 0.99),
        ("SELECT", 0.42),
        ("REVIEW", 0.60),
        ("NOT_FOUND", 0.95),
        ("PROVIDER_ERROR", 0.0),
        ("INVALID_OUTPUT", 0.0),
    ],
)
def test_persisted_outcomes_cannot_approve_or_export_without_human(
    tmp_path,
    decision,
    confidence,
):
    db_path = tmp_path / f"{decision}-{confidence}.db"
    database.init_db(db_path)
    order_id = create_recommended_order(
        db_path,
        confidence=confidence,
        decision=decision,
    )
    with pytest.raises(ValueError, match="unresolved items"):
        database.approve_order(
            order_id,
            actor="approver",
            action_id=f"blocked-{decision}-{confidence}",
            db_path=db_path,
        )
    with pytest.raises(ValueError, match="Only approved orders"):
        database.record_export(order_id, db_path)


def test_model_confidence_and_recommendation_survive_human_correction(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    order_id = create_recommended_order(db_path)
    item_id = database.get_order_by_id(order_id, db_path)["items"][0]["id"]
    catalog = {item.product_id: item for item in load_catalog()}
    database.review_order_item(
        order_id,
        item_id,
        actor="reviewer-1",
        action_id="review-correction-1",
        final_decision="SELECT",
        selected_sku="CL009",
        quantity=2.0,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    approved = database.approve_order(
        order_id,
        actor="approver-1",
        action_id="approval-1",
        db_path=db_path,
    )
    snapshot = approved["snapshot"][0]
    assert snapshot["product_id"] == "CL009"
    assert snapshot["model_recommendation_id"] == "CL010"
    assert snapshot["confidence"] == pytest.approx(0.62)
    assert snapshot["human_actor"] == "reviewer-1"
    assert snapshot["approval_actor"] == "approver-1"


def test_not_found_can_be_excluded_while_other_lines_approve(tmp_path):
    db_path = tmp_path / "orders.db"
    database.init_db(db_path)
    first = create_recommended_order(db_path)
    first_item = database.get_order_by_id(first, db_path)["items"][0]["id"]
    # Add a second line to the same test order as a model-only NOT_FOUND.
    now = database.utc_now_iso()
    with database.get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO order_items (
                order_id, raw_text, extracted_product, extracted_quantity, extracted_unit,
                recommendation_decision, confidence, status, candidates_json, created_at, updated_at
            ) VALUES (?, 'unknown', 'unknown', 1, 'piece', 'NOT_FOUND', 0, 'needs_review', '[]', ?, ?)
            """,
            (first, now, now),
        )
        second_item = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    catalog = {item.product_id: item for item in load_catalog()}
    database.review_order_item(
        first,
        first_item,
        actor="reviewer",
        action_id="select-one",
        final_decision="SELECT",
        selected_sku="CL010",
        quantity=2,
        unit="piece",
        catalog_lookup=catalog,
        db_path=db_path,
    )
    database.confirm_not_found(
        first,
        second_item,
        actor="reviewer",
        action_id="exclude-two",
        db_path=db_path,
    )
    approved = database.approve_order(
        first,
        actor="approver",
        action_id="approve-with-exclusion",
        db_path=db_path,
    )
    assert [row["original_item_id"] for row in approved["snapshot"]] == [first_item]


def test_final_immutable_evidence_bindings_remain_untouched():
    manifest_path = (
        PROJECT_ROOT
        / "evaluation/reranker_benchmark/integrity_repair/"
        "RR_K_V1_3_EVAL_FINAL_IMMUTABLE_MANIFEST.json"
    )
    if not manifest_path.is_file():
        pytest.skip(
            "restricted immutable evaluation evidence is intentionally not "
            "distributed in the public repository"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for binding in manifest["artifacts"].values():
        path = PROJECT_ROOT / binding["path"]
        assert path.is_file(), binding["path"]
        assert path.stat().st_size == binding["bytes"], binding["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == binding["sha256"], binding["path"]
