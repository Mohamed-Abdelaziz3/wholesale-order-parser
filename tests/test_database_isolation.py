"""Sentinel proving offline persistence workflows cannot mutate the runtime database."""

import hashlib
import inspect
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.catalog import load_catalog
from app.main import create_app


def _fingerprint(path: Path) -> tuple[str, int] | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def test_runtime_database_is_byte_identical_after_isolated_workflow(tmp_path):
    runtime_db = Path(database.DB_PATH).resolve()
    before = _fingerprint(runtime_db)

    public_functions = (
        database.get_connection,
        database.init_db,
        database.save_processed_order,
        database.log_audit_event,
        database.get_order_by_id,
        database.update_order_item,
        database.confirm_not_found,
        database.approve_order,
        database.record_export,
    )
    for function in public_functions:
        parameter = inspect.signature(function).parameters.get("db_path")
        assert parameter is not None
        assert parameter.default is None, f"{function.__name__} captured a database path"

    isolated_db = tmp_path / "isolated-orders.db"
    database.init_db(isolated_db)
    test_app = create_app(isolated_db)
    with TestClient(test_app) as client:
        assert client.get("/api/health").status_code == 200

    recommendation = {
        "product_id": "CL010",
        "product_name": "Flash Lemon",
        "unit": "piece",
        "price": 10.0,
    }
    order_id = database.save_processed_order(
        original_text="isolation sentinel",
        items_data=[
            {
                "raw_text": "flash lemon",
                "extracted_product": "flash lemon",
                "extracted_quantity": 2.0,
                "extracted_unit": "piece",
                "recommendation_product": recommendation,
                "recommendation_decision": "SELECT",
                "confidence": 0.62,
                "status": "needs_review",
                "candidates": [recommendation],
            }
        ],
        unresolved_text=[],
        processing_time_ms=1.0,
        db_path=isolated_db,
    )
    item_id = database.get_order_by_id(order_id, isolated_db)["items"][0]["id"]
    catalog_lookup = {item.product_id: item for item in load_catalog()}
    database.update_order_item(
        order_id,
        item_id,
        {
            "actor": "isolation-reviewer",
            "action_id": "isolation-review-1",
            "final_decision": "SELECT",
            "selected_sku": "CL010",
            "quantity": 2.0,
            "unit": "piece",
        },
        catalog_lookup,
        isolated_db,
    )
    database.approve_order(
        order_id,
        actor="isolation-approver",
        action_id="isolation-approve-1",
        db_path=isolated_db,
    )
    exported = database.record_export(order_id, isolated_db)
    assert exported["snapshot"][0]["product_id"] == "CL010"

    assert _fingerprint(runtime_db) == before
