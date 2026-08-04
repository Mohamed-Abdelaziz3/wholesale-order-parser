import json
import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.score_predictions import align_line_items, load_unit_map

BENCHMARK_DIR = PROJECT_ROOT / "evaluation" / "retrieval_benchmark"
BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

def determine_category(exp, pred):
    if not pred:
        return "extraction_omission"
    if not exp:
        return "hallucination_or_extra"
        
    exp_status = exp.get("expected_status")
    if exp_status == "matched":
        return "api_successful_in_catalog"
    elif exp_status == "ambiguous":
        return "questionable_ambiguous"
    elif exp_status == "out_of_catalog":
        return "out_of_catalog"
    return "unknown"

def main():
    gt_by_id = {}
    with open(PROJECT_ROOT / "evaluation" / "sealed" / "blind_ground_truth.jsonl", 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            gt_by_id[row['order_id']] = row
            
    preds_by_id = {}
    with open(PROJECT_ROOT / "evaluation" / "results" / "blind_predictions.jsonl", 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            preds_by_id[row['order_id']] = row

    unit_map = load_unit_map()
    
    queries = []
    
    for order_id, gt_rec in gt_by_id.items():
        pred_rec = preds_by_id.get(order_id, {})
        is_api_failure = pred_rec.get('api_error') is not None
        
        exp_items = gt_rec.get("expected_items", [])
        pred_items = pred_rec.get("items", []) if not is_api_failure else []
        
        # Add identifiers for alignment traceability
        for i, exp in enumerate(exp_items):
            exp["_exp_id"] = f"{order_id}_exp_{i}"
        for i, pred in enumerate(pred_items):
            pred["_pred_id"] = f"{order_id}_pred_{i}"
            
        matched_pairs, unassigned_exp, unassigned_pred = align_line_items(exp_items, pred_items, unit_map)
        
        for exp, pred in matched_pairs:
            cat = determine_category(exp, pred)
            queries.append({
                "order_id": order_id,
                "expected_item_identifier": exp["_exp_id"],
                "extracted_item_identifier": pred["_pred_id"],
                "extracted_text": pred.get("extracted_product", ""),
                "extracted_quantity": pred.get("extracted_quantity"),
                "extracted_unit": pred.get("extracted_unit"),
                "raw_text": pred.get("raw_text", ""),
                "expected_product_code": exp.get("expected_product_code"),
                "inclusion_category": cat,
                "alignment_evidence": "matched_pair"
            })
            
        for exp in unassigned_exp:
            cat = "api_failure" if is_api_failure else "extraction_omission"
            queries.append({
                "order_id": order_id,
                "expected_item_identifier": exp["_exp_id"],
                "extracted_item_identifier": None,
                "extracted_text": "",
                "extracted_quantity": None,
                "extracted_unit": None,
                "raw_text": "",
                "expected_product_code": exp.get("expected_product_code"),
                "inclusion_category": cat,
                "alignment_evidence": "unassigned_exp"
            })
            
        for pred in unassigned_pred:
            queries.append({
                "order_id": order_id,
                "expected_item_identifier": None,
                "extracted_item_identifier": pred["_pred_id"],
                "extracted_text": pred.get("extracted_product", ""),
                "extracted_quantity": pred.get("extracted_quantity"),
                "extracted_unit": pred.get("extracted_unit"),
                "raw_text": pred.get("raw_text", ""),
                "expected_product_code": None,
                "inclusion_category": "hallucination_or_extra",
                "alignment_evidence": "unassigned_pred"
            })
            
    # Sort queries by order_id then by expected_item_identifier
    queries.sort(key=lambda x: (x["order_id"], x["expected_item_identifier"] or "Z"))
    
    out_file = BENCHMARK_DIR / "benchmark_queries.jsonl"
    with open(out_file, 'w', encoding='utf-8') as f:
        for q in queries:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
            
    # Hash it
    hasher = hashlib.sha256()
    with open(out_file, 'rb') as f:
        hasher.update(f.read())
    
    print(f"Generated {len(queries)} queries.")
    print(f"Hash of benchmark_queries.jsonl: {hasher.hexdigest()}")
    
    # Save hash to manifest
    manifest_path = BENCHMARK_DIR / "benchmark_manifest.json"
    manifest = {}
    if manifest_path.exists():
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
            
    manifest["benchmark_queries_hash"] = hasher.hexdigest()
    
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

if __name__ == "__main__":
    main()
