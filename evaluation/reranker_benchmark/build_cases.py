import os
import sys
import json
import hashlib
from pathlib import Path
import csv
from rapidfuzz import fuzz

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RERANKER_DIR = PROJECT_ROOT / "evaluation" / "reranker_benchmark"
DATA_DIR = RERANKER_DIR / "data"
SEALED_DIR = RERANKER_DIR / "sealed"
PRED_DIR = RERANKER_DIR / "predictions"
REPAIR_DIR = PROJECT_ROOT / "evaluation" / "retrieval_benchmark" / "evidence_repair"

EXPECTED_HASHES = {
    "evaluation/retrieval_benchmark/benchmark_manifest.json": "f5c92fce2c731e8f447546c179f4a057ca38dc7f7fdf7dee8ed372a826d0ab05",
    "evaluation/retrieval_benchmark/split_manifest.json": "69ed638f1f5fd297cfa284fd508af95e88e04bfee024f5e9e6af3e4d5ae9a564",
    "evaluation/retrieval_benchmark/benchmark_queries.jsonl": "5fcba52e47925f28efb45b161eb5758656f320069e429bcbc4467ef2f4a29d5a",
    "evaluation/retrieval_benchmark/query_results.csv": "a312c18a60b45a3fb20ee1fe09f8d61f0627bb550f2f59bfc171ada29f9effce",
    "evaluation/retrieval_benchmark/method_metrics.csv": "589e9a4c5f00e3860397bbe032ab3807e089b607eaa027a6422db9f663e4ef93",
    "evaluation/retrieval_benchmark/RETRIEVAL_BENCHMARK_REPORT.md": "9df464fcfc58f947d56b24ca4e8f1186596658f7192f526bdaef533af82e1b30",
    "evaluation/results/blind_predictions.jsonl": "58d170442336feb2dca024edb634f17cd1e78eff25b7870d7272b5d4ae7a3a03",
    "evaluation/sealed/blind_ground_truth.jsonl": "e099d45b9bdf3709fc360ba3f62b77849ab04c144a0e1fbfac2bbec07aaad647",
    "evaluation/data/synthetic_catalog_400.csv": "61d7f39fd0ccd25cbd6f82f526d2902569b0fde16fac5ae008dae9e4cc373ec4"
}

REPAIR_HASHES = {
    "evaluation/retrieval_benchmark/evidence_repair/method_j_top10_candidates.jsonl": "df044f0ffd50aaab306a7a09b0b91a586a5b4f70c6126444f3abae0ff9a1819c",
    "evaluation/retrieval_benchmark/evidence_repair/recovered_frozen_config.json": "165fdd53fef70b3890fada8ba55ed9c43e5793cde66a80dfefbbb1c68354601c",
    "evaluation/retrieval_benchmark/evidence_repair/reproduced_query_results.csv": "c1813c76c0221e5609b918aff4e738a62d873f71150a1fc4d720552e70802b24",
    "evaluation/retrieval_benchmark/evidence_repair/reproduction_environment.json": "aa633a91451b3a2ba12763fcb42a33e295fc89ec2ae8e3189eef8b12bdb5c0b9"
}

def get_file_hash(filepath):
    if not filepath.exists():
        return "MISSING"
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def verify_evidence():
    print("Verifying Phase 0 frozen evidence hashes and Phase 1.5 repaired evidence hashes...")
    all_hashes = {**EXPECTED_HASHES, **REPAIR_HASHES}
    for rel_path, expected_h in all_hashes.items():
        full_path = PROJECT_ROOT / rel_path
        actual_h = get_file_hash(full_path)
        if actual_h != expected_h:
            raise RuntimeError(f"Hash mismatch for {rel_path}! Expected {expected_h}, got {actual_h}")
    print("All Phase 0 and Phase 1.5 evidence hashes verified successfully.")

def check_frozen_top5_availability():
    repaired_candidates_path = REPAIR_DIR / "method_j_top10_candidates.jsonl"
    repaired_config_path = REPAIR_DIR / "recovered_frozen_config.json"
    
    if not repaired_candidates_path.exists() or not repaired_config_path.exists():
        raise RuntimeError(
            "CRITICAL EVALUATION GATE FAILURE: Repaired Method J Top-10 candidates or recovered configuration artifact "
            "is MISSING. Per Correction 1 (DO NOT REGENERATE THE FROZEN TOP-5), build_cases.py must not rerun Method J "
            "retrieval or recompute embeddings/scores. Failing execution as mandated by user instructions."
        )
        
    actual_cands_h = get_file_hash(repaired_candidates_path)
    expected_cands_h = REPAIR_HASHES["evaluation/retrieval_benchmark/evidence_repair/method_j_top10_candidates.jsonl"]
    if actual_cands_h != expected_cands_h:
        raise RuntimeError(
            f"CRITICAL EVALUATION GATE FAILURE: Hash mismatch for {repaired_candidates_path}! "
            f"Expected {expected_cands_h}, got {actual_cands_h}"
        )

def load_catalog(filepath):
    catalog = {}
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            catalog[row["product_id"]] = row
    return catalog

def save_jsonl(filepath, records):
    with open(filepath, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Saved {len(records)} records to {filepath} (SHA-256: {get_file_hash(filepath)})")

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEALED_DIR.mkdir(parents=True, exist_ok=True)
    PRED_DIR.mkdir(parents=True, exist_ok=True)
    
    verify_evidence()
    check_frozen_top5_availability()
    
    catalog_path = PROJECT_ROOT / "evaluation" / "data" / "synthetic_catalog_400.csv"
    catalog = load_catalog(catalog_path)
    print(f"Loaded {len(catalog)} products from frozen catalog.")
    
    queries_path = PROJECT_ROOT / "evaluation" / "retrieval_benchmark" / "benchmark_queries.jsonl"
    queries_map = {}
    with open(queries_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if line.strip():
                queries_map[idx] = json.loads(line)
                
    repaired_cands_path = REPAIR_DIR / "method_j_top10_candidates.jsonl"
    repaired_records = []
    with open(repaired_cands_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                repaired_records.append(json.loads(line))
                
    print(f"Loaded {len(repaired_records)} repaired candidate records.")
    
    dev_cases, eval_cases = [], []
    dev_labels, eval_labels = [], []
    rr_a_dev, rr_a_eval = [], []
    rr_b_dev, rr_b_eval = [], []
    
    for r in repaired_records:
        qid = r["query_id"]
        if qid not in queries_map:
            raise RuntimeError(f"Query ID {qid} not found in benchmark_queries.jsonl!")
            
        q_obj = queries_map[qid]
        if r["query_text"] != q_obj.get("extracted_text", "") or r["order_id"] != q_obj.get("order_id"):
            raise RuntimeError(f"Discrepancy between repaired record and query record for query ID {qid}!")
            
        # Only evaluate over queries with successful in-catalog target SKUs
        if q_obj.get("inclusion_category") != "api_successful_in_catalog" or not q_obj.get("expected_product_code"):
            continue
            
        expected_code = q_obj["expected_product_code"]
        if expected_code not in catalog:
            raise RuntimeError(f"Expected code {expected_code} for query ID {qid} not found in catalog!")
            
        top5_cands_raw = r["candidates"][:5]
        top5_cands = []
        for c in top5_cands_raw:
            p_code = c["product_code"]
            if p_code not in catalog:
                raise RuntimeError(f"Candidate product code {p_code} in query ID {qid} not found in catalog!")
            top5_cands.append({
                "rank": c["rank"],
                "product_id": p_code,
                "product_code": p_code,
                "product_name": c["product_name"],
                "category": c["category"],
                "pack_size": c["pack_size"],
                "score": c["final_method_j_score"],
                "final_method_j_score": c["final_method_j_score"],
                "raw_bm25plus_score": c.get("raw_bm25plus_score", 0.0),
                "raw_tfidf_score": c.get("raw_tfidf_score", 0.0),
                "raw_e5_cosine_score": c.get("raw_e5_cosine_score", 0.0),
                "norm_bm25plus_score": c.get("norm_bm25plus_score", 0.0),
                "norm_tfidf_score": c.get("norm_tfidf_score", 0.0),
                "norm_e5_cosine_score": c.get("norm_e5_cosine_score", 0.0),
                "lexical_fusion_score": c.get("lexical_fusion_score", 0.0)
            })
            
        unlabeled_case = {
            "query_id": qid,
            "item_id": qid,
            "order_id": r["order_id"],
            "split": r["split"],
            "extracted_text": r["query_text"],
            "extracted_quantity": q_obj.get("extracted_quantity", ""),
            "extracted_unit": q_obj.get("extracted_unit", ""),
            "surrounding_context": q_obj.get("raw_text", ""),
            "candidates": top5_cands
        }
        
        # Verify strict label isolation in unlabeled cases
        for forbidden in ["expected_code", "expected_product_code", "expected_item_identifier", "label", "ground_truth", "is_correct"]:
            assert forbidden not in unlabeled_case, f"Label leakage in case {qid}: {forbidden}"
            for c in top5_cands:
                assert forbidden not in c, f"Label leakage in candidate for case {qid}: {forbidden}"
                
        sealed_label = {
            "item_id": qid,
            "query_id": qid,
            "order_id": r["order_id"],
            "split": r["split"],
            "expected_code": expected_code,
            "expected_item_identifier": q_obj.get("expected_item_identifier"),
            "inclusion_category": q_obj.get("inclusion_category")
        }
        
        # RR-A Baseline: Frozen retrieval order (rank 1 candidate)
        rr_a_pred = {
            "method_id": "RR-A",
            "order_id": r["order_id"],
            "item_id": qid,
            "query_rep": "mention",
            "selected_code": top5_cands[0]["product_id"] if top5_cands else None,
            "top_1_score": float(top5_cands[0]["score"]) if top5_cands else 0.0,
            "candidates_scored": [
                {
                    "product_id": c["product_id"],
                    "product_name": c["product_name"],
                    "base_score": float(c["score"]),
                    "final_score": float(c["score"]),
                    "contradiction_reasons": [],
                    "original_rank": c["rank"]
                } for c in top5_cands
            ],
            "status": "COMPLETED",
            "processing_time_ms": 0.0,
            "estimated_cost_usd": 0.0
        }
        
        # RR-B Baseline: RapidFuzz diagnostic
        scored_cands_b = []
        for c in top5_cands:
            cand_text = f"{c['product_id']} - {c['product_name']} ({c['category']} - {c['pack_size']})".strip()
            fuzz_score = fuzz.WRatio(r["query_text"], cand_text) / 100.0
            scored_cands_b.append({
                "product_id": c["product_id"],
                "product_name": c["product_name"],
                "base_score": float(fuzz_score),
                "final_score": float(fuzz_score),
                "contradiction_reasons": [],
                "original_rank": c["rank"]
            })
        scored_cands_b.sort(key=lambda x: (-x["final_score"], x["original_rank"]))
        
        rr_b_pred = {
            "method_id": "RR-B",
            "order_id": r["order_id"],
            "item_id": qid,
            "query_rep": "mention",
            "selected_code": scored_cands_b[0]["product_id"] if scored_cands_b else None,
            "top_1_score": float(scored_cands_b[0]["final_score"]) if scored_cands_b else 0.0,
            "candidates_scored": scored_cands_b,
            "status": "COMPLETED",
            "processing_time_ms": 0.0,
            "estimated_cost_usd": 0.0
        }
        
        if r["split"] == "dev":
            dev_cases.append(unlabeled_case)
            dev_labels.append(sealed_label)
            rr_a_dev.append(rr_a_pred)
            rr_b_dev.append(rr_b_pred)
        elif r["split"] == "eval":
            eval_cases.append(unlabeled_case)
            eval_labels.append(sealed_label)
            rr_a_eval.append(rr_a_pred)
            rr_b_eval.append(rr_b_pred)
            
    # Verify authoritative counts and split isolation
    print(f"\n--- Verification Report ---")
    print(f"Verified authoritative DEV query count: {len(dev_cases)} (in-catalog successful queries per frozen split artifacts, differing from estimated 72)")
    print(f"Verified authoritative EVAL query count: {len(eval_cases)} (matches authoritative denominator 148)")
    
    if len(eval_cases) != 148:
        raise RuntimeError(f"Expected 148 EVAL cases, got {len(eval_cases)}!")
        
    dev_orders = {c["order_id"] for c in dev_cases}
    eval_orders = {c["order_id"] for c in eval_cases}
    overlap = dev_orders & eval_orders
    if overlap:
        raise RuntimeError(f"Order overlap between DEV and EVAL splits: {overlap}")
    print("Verified zero order overlap between DEV and EVAL splits.")
    
    print("\n--- Saving Artifacts ---")
    save_jsonl(DATA_DIR / "dev_cases.jsonl", dev_cases)
    save_jsonl(DATA_DIR / "eval_cases.jsonl", eval_cases)
    save_jsonl(SEALED_DIR / "dev_labels.jsonl", dev_labels)
    save_jsonl(SEALED_DIR / "eval_labels.jsonl", eval_labels)
    save_jsonl(PRED_DIR / "rr_a_dev_predictions.jsonl", rr_a_dev)
    save_jsonl(PRED_DIR / "rr_a_eval_predictions.jsonl", rr_a_eval)
    save_jsonl(PRED_DIR / "rr_b_dev_predictions.jsonl", rr_b_dev)
    save_jsonl(PRED_DIR / "rr_b_eval_predictions.jsonl", rr_b_eval)
    print("\nCase building and baseline prediction generation completed successfully.")

if __name__ == "__main__":
    main()

