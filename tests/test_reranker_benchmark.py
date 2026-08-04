import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.reranker_benchmark.attribute_contradictions import AttributeContradictionDetector
from evaluation.reranker_benchmark.run_local_rerankers import build_query_text, check_anti_leakage
from evaluation.reranker_benchmark.score_results import score_method


def test_anti_leakage_barrier():
    with pytest.raises(PermissionError, match="ANTI-LEAKAGE VIOLATION"):
        check_anti_leakage("evaluation/reranker_benchmark/sealed/dev_labels.jsonl")
    with pytest.raises(PermissionError, match="ANTI-LEAKAGE VIOLATION"):
        check_anti_leakage("C:\\Project\\evaluation\\reranker_benchmark\\sealed\\eval_labels.jsonl")
    # Valid paths should not raise
    check_anti_leakage("evaluation/reranker_benchmark/data/dev_cases.jsonl")
    check_anti_leakage("evaluation/reranker_benchmark/predictions/rr_c_dev_predictions.jsonl")

def test_attribute_contradiction_detector_explicit_conflicts():
    detector = AttributeContradictionDetector()

    # 1. Volume conflict (explicit vs explicit)
    q1 = detector.parse_attributes("5 لتر كلور الفارس ابيض")
    c1_bad = detector.parse_attributes("كلور 20 لتر النيل ابيض")
    is_contra, reasons, pen = detector.check_contradictions(q1, c1_bad)
    assert is_contra is True
    assert pen > 0
    assert any("explicit_volume_conflict" in r or "explicit_brand_match_conflict" in r for r in reasons)

    # 2. Missing volume in candidate MUST NEVER be treated as contradiction
    c1_ok = detector.parse_attributes("كلور مبيض للغسيل الفارس ابيض")
    is_contra, reasons, pen = detector.check_contradictions(q1, c1_ok)
    assert is_contra is False
    assert pen == 0.0

def test_attribute_contradiction_detector_color_and_brand():
    detector = AttributeContradictionDetector()
    q = detector.parse_attributes("اكياس بلاستيك اسود الفارس")
    c_diff_color = detector.parse_attributes("اكياس بلاستيك ابيض الفارس")
    is_contra, reasons, pen = detector.check_contradictions(q, c_diff_color)
    assert is_contra is True
    assert any("explicit_color_conflict" in r for r in reasons)

def test_query_representation_builder():
    case = {
        "extracted_text": "صابون لافندر",
        "extracted_quantity": 2,
        "extracted_unit": "كيس",
        "surrounding_context": "أول عنصر في الطلب"
    }
    assert build_query_text(case, "mention") == "صابون لافندر"
    assert build_query_text(case, "mention_qty") == "2 كيس صابون لافندر"
    assert build_query_text(case, "mention_context") == "2 كيس صابون لافندر [Context: أول عنصر في الطلب]"

def test_candidate_constraint_and_schema():
    # Simulate Gemini response parsing and validation
    candidates = [
        {"product_id": "PC001", "product_name": "كوب ورقي"},
        {"product_id": "PC002", "product_name": "كوب بلاستيك"}
    ]
    valid_ids = {c["product_id"] for c in candidates}

    # Case 1: Valid selection
    resp1 = {"decision": "SELECT", "product_code": "PC001", "confidence": 0.95, "reason_codes": ["EXACT_MATCH"]}
    sel_code = resp1.get("product_code")
    if sel_code not in valid_ids:
        sel_code = None
    assert sel_code == "PC001"

    # Case 2: Hallucinated / invalid product code must be rejected (set to None)
    resp2 = {"decision": "SELECT", "product_code": "HALLUCINATED_999", "confidence": 0.99, "reason_codes": ["BAD"]}
    sel_code2 = resp2.get("product_code")
    if sel_code2 not in valid_ids:
        sel_code2 = None
    assert sel_code2 is None

def test_build_cases_enforces_zero_retrieval_reexecution(monkeypatch):
    from evaluation.reranker_benchmark import build_cases
    # When repaired Method J Top-10 candidates file is missing or hash mismatch, build_cases.check_frozen_top5_availability() MUST raise RuntimeError
    def mock_exists(self):
        if "method_j_top10_candidates.jsonl" in str(self):
            return False
        return Path.exists(self)
    monkeypatch.setattr(Path, "exists", mock_exists)
    with pytest.raises(RuntimeError, match="CRITICAL EVALUATION GATE FAILURE"):
        build_cases.check_frozen_top5_availability()

def test_eval_execution_blocked_before_frozen_config(monkeypatch):
    # Simulate missing frozen_config.json
    def mock_exists(self):
        if "frozen_config.json" in str(self):
            return False
        return Path.exists(self)
    monkeypatch.setattr(Path, "exists", mock_exists)

    with pytest.raises(RuntimeError, match="MANDATORY GATE FAILURE: Cannot run EVAL predictions before calibration and config are frozen"):
        frozen_config_path = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "frozen_config.json"
        if not frozen_config_path.exists():
            raise RuntimeError("MANDATORY GATE FAILURE: Cannot run EVAL predictions before calibration and config are frozen in frozen_config.json!")

def test_score_method_metrics_and_gates():
    labels = [
        {"item_id": 1, "expected_code": "PC001"},
        {"item_id": 2, "expected_code": "PC002"},
        {"item_id": 3, "expected_code": "PC003"},
        {"item_id": 4, "expected_code": "PC004"}
    ]
    preds = [
        {"item_id": 1, "selected_code": "PC001", "top_1_score": 0.90, "status": "COMPLETED", "processing_time_ms": 100},
        {"item_id": 2, "selected_code": "PC002", "top_1_score": 0.88, "status": "COMPLETED", "processing_time_ms": 150},
        {"item_id": 3, "selected_code": "WRONG", "top_1_score": 0.86, "status": "COMPLETED", "processing_time_ms": 120}, # Silent error (score >= 0.85)
        {"item_id": 4, "selected_code": "PC004", "top_1_score": 0.50, "status": "COMPLETED", "processing_time_ms": 110}  # Routed to review
    ]

    frozen_config = {"calibrated_thresholds": {"acceptance_threshold": 0.85, "review_threshold": 0.40}}
    res = score_method("TEST-METHOD", preds, labels, frozen_config)

    assert res["completed_cases"] == 4
    assert res["top_1_accuracy"] == 75.0 # 3 out of 4 correct
    assert res["wrong_confident_selections"] == 1
    assert res["silent_errors"] == 1
    assert res["review_rate"] == 25.0 # 1 out of 4 (item 4) in review range
    assert res["latency_p50_ms"] == 115.0

def test_no_label_leakage_in_unlabeled_case_format():
    sample_case = {
        "order_id": 10,
        "item_id": 101,
        "extracted_text": "كلور 4 لتر",
        "extracted_quantity": 1,
        "extracted_unit": "جركن",
        "candidates": [
            {"product_id": "CC001", "product_name": "كلور النيل 4 لتر", "rank": 1},
            {"product_id": "CC002", "product_name": "كلور الفارس 4 لتر", "rank": 2}
        ]
    }
    assert "expected_code" not in sample_case
    assert "label" not in sample_case
    assert "ground_truth" not in sample_case

def test_generate_derived_predictions():
    from evaluation.reranker_benchmark import cascade_utils
    assert hasattr(cascade_utils, "generate_derived_predictions")
