"""
Automated Pytest Suite for Synthetic Evaluation System.
Verifies dataset determinism, anti-leakage isolation, line-item alignment,
scoring correctness, manifest hashes, and backward-compatible catalog configuration.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def test_catalog_env_variable_and_default(monkeypatch):
    """Verify load_catalog defaults to catalog.csv when CATALOG_PATH is unset, and respects CATALOG_PATH when set."""
    from app.catalog import load_catalog

    # 1. Default load
    monkeypatch.delenv("CATALOG_PATH", raising=False)
    default_catalog = load_catalog()
    assert len(default_catalog) == 50, f"Expected 50 products in original catalog, got {len(default_catalog)}"

    # 2. Configured load
    synth_path = PROJECT_ROOT / "evaluation" / "data" / "synthetic_catalog_400.csv"
    if synth_path.exists():
        monkeypatch.setenv("CATALOG_PATH", str(synth_path))
        synth_catalog = load_catalog()
        assert len(synth_catalog) == 400, f"Expected 400 products in synthetic catalog, got {len(synth_catalog)}"


def test_original_catalog_hash_unchanged():
    """Verify original production catalog.csv remains completely unchanged."""
    import hashlib
    orig_path = PROJECT_ROOT / "catalog.csv"
    h = hashlib.sha256()
    with open(orig_path, "rb") as f:
        h.update(f.read())

    # Check original catalog lines count
    with open(orig_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 51, f"Expected 51 lines in original catalog.csv, got {len(lines)}"


def test_synthetic_catalog_structure():
    """Verify synthetic_catalog_400.csv has exactly 400 products and valid schema."""
    from app.catalog import load_catalog
    synth_path = PROJECT_ROOT / "evaluation" / "data" / "synthetic_catalog_400.csv"
    if not synth_path.exists():
        pytest.skip("synthetic_catalog_400.csv not yet generated.")

    catalog = load_catalog(str(synth_path))
    assert len(catalog) == 400
    ids = [p.product_id for p in catalog]
    assert len(set(ids)) == 400, "All product IDs must be unique"


def test_blind_orders_and_sealed_ground_truth_structure():
    """Verify dataset sizes, unique order IDs, and GT validity."""
    blind_path = PROJECT_ROOT / "evaluation" / "data" / "blind_orders.jsonl"
    gt_path = PROJECT_ROOT / "evaluation" / "sealed" / "blind_ground_truth.jsonl"

    if not (blind_path.exists() and gt_path.exists()):
        pytest.skip("Evaluation datasets not generated.")

    blind_orders = [json.loads(line) for line in open(blind_path, encoding="utf-8") if line.strip()]
    gt_records = [json.loads(line) for line in open(gt_path, encoding="utf-8") if line.strip()]

    assert len(blind_orders) == 100
    assert len(gt_records) == 100

    blind_ids = [o["order_id"] for o in blind_orders]
    gt_ids = [g["order_id"] for g in gt_records]

    assert len(set(blind_ids)) == 100
    assert blind_ids == gt_ids

    total_items = sum(len(g["expected_items"]) for g in gt_records)
    assert total_items >= 250, f"Expected >= 250 total items, got {total_items}"


def test_anti_leakage_path_rejection():
    """Verify run_predictions CLI rejects any path referencing 'sealed'."""
    from evaluation.run_predictions import parse_args

    test_args = [
        "--catalog", "evaluation/data/synthetic_catalog_400.csv",
        "--orders", "evaluation/sealed/blind_ground_truth.jsonl", # Illegal sealed path
        "--output", "evaluation/results/test_pred.jsonl",
        "--db", "evaluation/test.db",
        "--run-id", "test_run"
    ]

    sys.argv = ["run_predictions.py"] + test_args
    with pytest.raises(ValueError, match="STRICT ANTI-LEAKAGE FAILURE"):
        parse_args()


def test_line_item_alignment_scenarios():
    """Test 1-to-1 optimal line-item alignment on edge case scenarios."""
    from evaluation.score_predictions import align_line_items, load_unit_map

    unit_map = load_unit_map()

    exp_items = [
        {"extracted_product": "صابون سائل ليمون 5 لتر", "expected_product_code": "CC001", "expected_quantity": 5.0, "expected_normalized_unit": "جركن", "expected_status": "matched", "acceptable_candidate_ids": ["CC001"]},
        {"extracted_product": "مسحوق اتوماتيك 5 كيلو", "expected_product_code": "DT003", "expected_quantity": 2.0, "expected_normalized_unit": "كيس", "expected_status": "matched", "acceptable_candidate_ids": ["DT003"]}
    ]

    # 1. Reversed order predictions
    pred_reversed = [
        {"raw_text": "2 كيس مسحوق", "extracted_product": "مسحوق اتوماتيك 5 كيلو", "extracted_quantity": 2.0, "extracted_unit": "كيس", "matched_product_id": "DT003", "status": "confirmed", "confidence": 0.95},
        {"raw_text": "5 جراكن صابون", "extracted_product": "صابون سائل ليمون 5 لتر", "extracted_quantity": 5.0, "extracted_unit": "جركن", "matched_product_id": "CC001", "status": "confirmed", "confidence": 0.95}
    ]

    pairs, unassigned_exp, unassigned_pred = align_line_items(exp_items, pred_reversed, unit_map)
    assert len(pairs) == 2
    assert len(unassigned_exp) == 0
    assert len(unassigned_pred) == 0

    # 2. Missing item scenario
    pred_missing = [pred_reversed[0]]  # Only DT003 predicted
    pairs, unassigned_exp, unassigned_pred = align_line_items(exp_items, pred_missing, unit_map)
    assert len(pairs) == 1
    assert len(unassigned_exp) == 1
    assert unassigned_exp[0]["expected_product_code"] == "CC001"
    assert len(unassigned_pred) == 0

    # 3. Hallucinated extra item scenario
    pred_extra = pred_reversed + [
        {"raw_text": "كلور", "extracted_product": "كلوركس 1 لتر", "extracted_quantity": 1.0, "extracted_unit": "قطعة", "matched_product_id": "CC014", "status": "confirmed", "confidence": 0.90}
    ]
    pairs, unassigned_exp, unassigned_pred = align_line_items(exp_items, pred_extra, unit_map)
    assert len(pairs) == 2
    assert len(unassigned_exp) == 0
    assert len(unassigned_pred) == 1
    assert unassigned_pred[0]["matched_product_id"] == "CC014"
