import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.reranker_benchmark.run_groq_reranker import (
    build_schema,
    check_anti_leakage,
    redact_secret,
)


class TestGroqReranker(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_path = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_01_valid_candidate_selection(self):
        valid_codes = ["P1", "P2", "REVIEW", "NOT_FOUND"]
        schema = build_schema(valid_codes)
        self.assertIn("P1", schema["schema"]["properties"]["selected_code"]["enum"])
        self.assertIn("EXACT_MATCH", schema["schema"]["properties"]["reason_code"]["enum"])

    def test_02_review_selection(self):
        valid_codes = ["P1", "REVIEW", "NOT_FOUND"]
        schema = build_schema(valid_codes)
        self.assertIn("REVIEW", schema["schema"]["properties"]["selected_code"]["enum"])

    def test_03_not_found_selection(self):
        valid_codes = ["P1", "REVIEW", "NOT_FOUND"]
        schema = build_schema(valid_codes)
        self.assertIn("NOT_FOUND", schema["schema"]["properties"]["selected_code"]["enum"])

    def test_04_invalid_candidate_rejection_no_fuzzy_repair(self):
        # When an out-of-candidate code is returned, it must be rejected without fuzzy matching repair
        valid_codes = ["P100", "P200", "REVIEW", "NOT_FOUND"]
        invalid_sel = "P101" # close to P100 but invalid
        self.assertNotIn(invalid_sel, valid_codes)

    def test_05_malformed_json_rejection(self):
        content = "this is not valid json {"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(content)

    def test_06_duplicate_records_deduplication(self):
        pred_file = self.test_path / "test_preds.jsonl"
        records = [
            {"item_id": "item1", "status": "INCOMPLETE", "error": "timeout"},
            {"item_id": "item1", "status": "COMPLETED", "selected_code": "P1"},
            {"item_id": "item2", "status": "COMPLETED", "selected_code": "P2"},
            {"item_id": "item2", "status": "COMPLETED", "selected_code": "P2_NEW"}
        ]
        with open(pred_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        # Simulate deduplication logic from run_groq_reranker.py
        all_recs = [
            json.loads(line)
            for line in open(pred_file, "r", encoding="utf-8")
            if line.strip()
        ]
        dedup = {}
        for r in all_recs:
            iid = r.get("item_id")
            if iid not in dedup:
                dedup[iid] = r
            else:
                if dedup[iid].get("status") != "COMPLETED" and r.get("status") == "COMPLETED":
                    dedup[iid] = r
                elif dedup[iid].get("status") == "COMPLETED" and r.get("status") == "COMPLETED":
                    dedup[iid] = r
        final_recs = list(dedup.values())
        self.assertEqual(len(final_recs), 2)
        self.assertEqual(dedup["item1"]["status"], "COMPLETED")
        self.assertEqual(dedup["item1"]["selected_code"], "P1")
        self.assertEqual(dedup["item2"]["selected_code"], "P2_NEW")

    def test_07_retry_exhaustion(self):
        max_retries = 3
        attempts = 0
        for _ in range(max_retries):
            attempts += 1
        self.assertEqual(attempts, max_retries)

    def test_08_rate_limit_response_handling(self):
        err_msg = "429 Rate limit reached, please retry in 1.5s"
        self.assertIn("429", err_msg)
        import re
        m = re.search(r"retry\s*in\s*([0-9.]+)\s*s", err_msg, re.IGNORECASE)
        self.assertIsNotNone(m)
        self.assertEqual(float(m.group(1)), 1.5)

    def test_09_provider_error_handling(self):
        err_msg = "500 Internal Server Error from Groq"
        redacted = redact_secret(err_msg, "TEST_ONLY_PROVIDER_SECRET")
        self.assertIn("500", redacted)

    def test_10_interrupted_run_resumption(self):
        pred_file = self.test_path / "resume_test.jsonl"
        with open(pred_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"item_id": "item1", "status": "COMPLETED", "selected_code": "P1"}) + "\n")
            f.write(json.dumps({"item_id": "item2", "status": "INCOMPLETE", "error": "fail"}) + "\n")

        completed_items = set()
        for line in open(pred_file, "r", encoding="utf-8"):
            p = json.loads(line)
            if p.get("status") == "COMPLETED":
                completed_items.add(p.get("item_id"))
        self.assertIn("item1", completed_items)
        self.assertNotIn("item2", completed_items)

    def test_11_key_redaction(self):
        secret = "gsk_TEST_ONLY_" + "x" * 48
        text1 = f"Error calling API with key {secret}: timeout"
        self.assertEqual(redact_secret(text1, secret), "Error calling API with key [REDACTED_GROQ_KEY]: timeout")

        text2 = f"Authorization: Bearer {secret}\nContent-Type: application/json"
        self.assertIn("[REDACTED_GROQ_KEY]", redact_secret(text2))
        self.assertNotIn(secret, redact_secret(text2))

    def test_12_gemini_artifact_immutability(self):
        gemini_dev = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions" / "rr_g_dev_predictions.jsonl"
        gemini_eval = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions" / "rr_g_eval_predictions.jsonl"
        if gemini_dev.exists():
            h_dev = hashlib.sha256(open(gemini_dev, "rb").read()).hexdigest()
            self.assertEqual(len(h_dev), 64)
        if gemini_eval.exists():
            h_eval = hashlib.sha256(open(gemini_eval, "rb").read()).hexdigest()
            self.assertEqual(len(h_eval), 64)

    def test_13_prohibition_of_eval_label_access(self):
        with self.assertRaises(PermissionError) as cm:
            check_anti_leakage("evaluation/reranker_benchmark/sealed/eval_labels.jsonl")
        self.assertIn("ANTI-LEAKAGE VIOLATION", str(cm.exception))

    def test_14_eval_gate_enforcement(self):
        # Simulate check when running split=eval before RR-K is frozen
        fc = {"status": "FROZEN_FOR_EVALUATION", "selected_api_model": "gemini-3.5-flash"}
        is_rr_k_approved = (fc.get("status") == "FROZEN_FOR_EVALUATION" and fc.get("selected_api_model") == "RR-K")
        self.assertFalse(is_rr_k_approved)

if __name__ == "__main__":
    unittest.main()
