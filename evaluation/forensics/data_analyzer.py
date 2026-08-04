import json
import csv
import hashlib
import os
import sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from decimal import Decimal

PROJECT_ROOT = Path(r"c:\Users\moham\OneDrive\Documents\Project @\wholesale-order-parser")
sys.path.insert(0, str(PROJECT_ROOT))

# Import application components for deterministic re-evaluation
from app.models import CatalogProduct, ExtractedItem
from app.matcher import ProductMatcher
from evaluation.score_predictions import load_unit_map, normalize_unit_str, align_line_items, compute_similarity

EVAL_DIR = PROJECT_ROOT / "evaluation"
FORENSICS_DIR = EVAL_DIR / "forensics"
FORENSICS_DIR.mkdir(parents=True, exist_ok=True)

def sha256_file(filepath):
    if not filepath.exists(): return None
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def main():
    # 1. Evidence Integrity Audit (Hashes)
    manifest_path = EVAL_DIR / "manifest.json"
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
        
    forensic_manifest = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "original_run_id": None, # Will extract later
        "original_prediction_hash": manifest.get("prediction_file_hash"),
        "confirmation": "No evaluated input was modified. Hashes verified.",
        "input_hashes": {},
        "output_hashes": {}
    }
    
    for rel_path, expected_hash in manifest['file_hashes'].items():
        actual_hash = sha256_file(PROJECT_ROOT / rel_path)
        forensic_manifest["input_hashes"][rel_path] = actual_hash
        if actual_hash != expected_hash:
            raise RuntimeError(f"Hash mismatch for {rel_path}")

    # Load Data
    from app.catalog import load_catalog
    catalog = load_catalog(str(EVAL_DIR / "data" / "synthetic_catalog_400.csv"))
        
    matcher = ProductMatcher(catalog)
    
    gt_by_id = {}
    with open(EVAL_DIR / "sealed" / "blind_ground_truth.jsonl", 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            gt_by_id[row['order_id']] = row
            
    preds_by_id = {}
    run_ids = set()
    pred_timestamps = []
    api_failed_orders = set()
    with open(EVAL_DIR / "results" / "blind_predictions.jsonl", 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            preds_by_id[row['order_id']] = row
            run_ids.add(row.get('run_id'))
            if row.get('prediction_timestamp'):
                pred_timestamps.append(datetime.fromisoformat(row['prediction_timestamp']))
            if row.get('api_error') is not None:
                api_failed_orders.add(row['order_id'])
                
    forensic_manifest["original_run_id"] = list(run_ids)[0]
    
    # 2. Dataset Realism Audit
    # We will identify questionable ground truth (e.g. expects exact match but message is generic)
    # 3. API Failure Analysis
    # 4. Pipeline-level Failure Decomposition
    # 6. Retrieval and Ranking Analysis
    
    taxonomy = [] # order_id, expected_product_code, expected_status, extracted_product, actual_product_code, primary_failure_stage, details
    candidate_recall = [] # order_id, expected_product_code, extracted_product, rank, score, in_top_1, in_top_3, in_top_5, in_top_10
    api_failures = []
    questionable_gt = []
    unit_map = load_unit_map()
    
    total_expected = 0
    total_api_success_expected = 0
    total_api_success_matched = 0
    
    total_extracted_items = 0
    total_correctly_extracted = 0 # Semantic match
    
    # Counterfactual accumulators
    cf_api_fixed_correct = 0
    cf_extraction_fixed_correct = 0
    cf_top3_selected_correct = 0
    cf_top5_selected_correct = 0
    cf_qty_unit_fixed_perfect = 0
    
    for order_id, gt_rec in gt_by_id.items():
        pred_rec = preds_by_id[order_id]
        is_api_failure = order_id in api_failed_orders
        
        if is_api_failure:
            api_failures.append({
                "order_id": order_id,
                "error_type": "API_ERROR",
                "details": pred_rec.get("api_error", ""),
                "model": pred_rec.get("actual_model_used"),
                "retry_count": pred_rec.get("retry_count")
            })
            
        exp_items = gt_rec.get("expected_items", [])
        pred_items = pred_rec.get("items", [])
        
        total_expected += len(exp_items)
        if not is_api_failure:
            total_api_success_expected += len(exp_items)
            total_extracted_items += len(pred_items)
            
        matched_pairs, unassigned_exp, unassigned_pred = align_line_items(exp_items, pred_items, unit_map)
        
        # Process unassigned expected (Omissions)
        for exp in unassigned_exp:
            exp_code = exp.get("expected_product_code")
            primary_stage = 1 if is_api_failure else 2
            details = "API Failure" if is_api_failure else "Item omitted by extraction"
            taxonomy.append({
                "order_id": order_id,
                "expected_product_code": exp_code,
                "expected_status": exp.get("expected_status"),
                "extracted_product": "",
                "actual_product_code": "",
                "primary_failure_stage": primary_stage,
                "details": details
            })
            if not is_api_failure:
                cf_extraction_fixed_correct += 1 # If extraction was perfect, this could be correct

        # Process matched pairs
        for exp, pred in matched_pairs:
            exp_status = exp.get("expected_status")
            exp_code = exp.get("expected_product_code")
            pred_matched_id = pred.get("matched_product_id")
            pred_status = pred.get("status")
            extracted_text = pred.get("extracted_product", "")
            
            # Recalculate candidates using ProductMatcher
            candidates = matcher.find_candidates(extracted_text, top_k=10, min_score=0.20)
            c_ids = [c.product_id for c in candidates]
            
            rank = -1
            if exp_code in c_ids:
                rank = c_ids.index(exp_code) + 1
                
            in_top_1 = rank == 1
            in_top_3 = 1 <= rank <= 3
            in_top_5 = 1 <= rank <= 5
            in_top_10 = 1 <= rank <= 10
            
            if not is_api_failure and exp_code:
                candidate_recall.append({
                    "order_id": order_id,
                    "expected_product_code": exp_code,
                    "extracted_product": extracted_text,
                    "rank": rank,
                    "in_top_1": int(in_top_1),
                    "in_top_3": int(in_top_3),
                    "in_top_5": int(in_top_5),
                    "in_top_10": int(in_top_10)
                })

            primary_stage = 16 # Other
            details = ""
            
            if is_api_failure:
                primary_stage = 1
                details = "API Error"
            elif exp_status == "matched":
                # Check extraction quality roughly via similarity
                from rapidfuzz import fuzz
                from app.normalizer import normalize_arabic
                gt_text = normalize_arabic(exp.get("extracted_product", ""))
                pred_norm = normalize_arabic(extracted_text)
                text_sim = fuzz.token_set_ratio(gt_text, pred_norm)
                
                if text_sim < 60:
                    primary_stage = 4
                    details = "Product mention extracted incorrectly"
                elif rank == -1:
                    primary_stage = 5
                    details = "Extracted OK, but correct item absent from candidates"
                    total_correctly_extracted += 1
                elif rank > 1:
                    primary_stage = 6
                    details = f"Correct item ranked {rank} (below incorrect)"
                    total_correctly_extracted += 1
                elif rank == 1 and pred_status != "confirmed":
                    primary_stage = 7 if pred_status != "ambiguous" else 8
                    details = "Correct item ranked 1st but low confidence/ambiguous"
                    total_correctly_extracted += 1
                elif rank == 1 and pred_status == "confirmed" and pred_matched_id == exp_code:
                    total_correctly_extracted += 1
                    total_api_success_matched += 1
                    
                    # Check qty/unit
                    exp_q = Decimal(str(exp.get("expected_quantity", 1.0)))
                    pred_q = Decimal(str(pred.get("extracted_quantity", 1.0)))
                    exp_u = normalize_unit_str(exp.get("expected_normalized_unit", ""), unit_map)
                    pred_u = normalize_unit_str(pred.get("extracted_unit", ""), unit_map)
                    
                    if exp_q != pred_q:
                        primary_stage = 11
                        details = f"Quantity failure: expected {exp_q}, got {pred_q}"
                    elif exp_u != pred_u:
                        primary_stage = 12
                        details = f"Unit failure: expected {exp_u}, got {pred_u}"
                    else:
                        primary_stage = 0 # SUCCESS
                        details = "Perfect match"
                        cf_qty_unit_fixed_perfect += 1
                else:
                    primary_stage = 14
                    details = "Scoring or alignment artefact"
                    
                # Counterfactuals
                if in_top_3: cf_top3_selected_correct += 1
                if in_top_5: cf_top5_selected_correct += 1
                cf_api_fixed_correct += 1 if (rank == 1 and pred_status == "confirmed") else 0
                cf_extraction_fixed_correct += 1 if in_top_5 else 0 # Assuming extraction fix would pull it to top 5 at least
                
            elif exp_status == "ambiguous":
                if pred_status in ("ambiguous", "low_confidence", "needs_review"):
                    primary_stage = 9
                    details = "Correctly identified ambiguous"
                else:
                    primary_stage = 6
                    details = "Failed to flag ambiguity (confident wrong match)"
            elif exp_status == "unmatched":
                if pred_status in ("not_found", "unmatched") or pred_matched_id is None:
                    primary_stage = 10
                    details = "Correctly identified out-of-catalog"
                else:
                    primary_stage = 6
                    details = "Failed to flag out-of-catalog (confident wrong match)"
                    
            if primary_stage > 0:
                taxonomy.append({
                    "order_id": order_id,
                    "expected_product_code": exp_code,
                    "expected_status": exp_status,
                    "extracted_product": extracted_text,
                    "actual_product_code": pred_matched_id,
                    "primary_failure_stage": primary_stage,
                    "details": details
                })
                
        # Process unassigned predicted (Spurious)
        for pred in unassigned_pred:
            if not is_api_failure:
                taxonomy.append({
                    "order_id": order_id,
                    "expected_product_code": None,
                    "expected_status": None,
                    "extracted_product": pred.get("extracted_product"),
                    "actual_product_code": pred.get("matched_product_id"),
                    "primary_failure_stage": 3,
                    "details": "Spurious additional extracted item"
                })

    # Save CSVs
    def write_csv(filename, data):
        if not data: return
        with open(FORENSICS_DIR / filename, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
            
    write_csv("failure_taxonomy.csv", taxonomy)
    write_csv("candidate_recall_at_k.csv", candidate_recall)
    write_csv("api_failure_analysis.csv", api_failures)
    
    # Compute metrics for forensic_metrics.json
    api_success_rate = 1.0 - (len(api_failed_orders) / 100.0)
    
    stage_counts = defaultdict(int)
    for t in taxonomy:
        stage_counts[t['primary_failure_stage']] += 1
        
    metrics = {
        "operational": {
            "total_orders": 100,
            "total_expected_items": total_expected,
            "api_error_rate": len(api_failed_orders) / 100.0,
            "overall_exact_product_accuracy": 0.1136, # from score_predictions
        },
        "api_successful_only": {
            "total_orders": 100 - len(api_failed_orders),
            "total_expected_items": total_api_success_expected,
            "exact_product_accuracy": total_api_success_matched / max(1, total_api_success_expected),
            "extraction_semantic_correctness": total_correctly_extracted / max(1, total_api_success_expected),
        },
        "failure_decomposition": dict(stage_counts),
        "counterfactual_oracles": {
            "api_fixed_accuracy": cf_api_fixed_correct / max(1, total_expected),
            "extraction_fixed_accuracy": cf_extraction_fixed_correct / max(1, total_expected),
            "top3_selected_accuracy": cf_top3_selected_correct / max(1, total_api_success_expected),
            "top5_selected_accuracy": cf_top5_selected_correct / max(1, total_api_success_expected),
            "qty_unit_fixed_accuracy": cf_qty_unit_fixed_perfect / max(1, total_api_success_expected),
        }
    }
    
    with open(FORENSICS_DIR / "forensic_metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
        
    with open(FORENSICS_DIR / "counterfactual_oracles.json", "w", encoding="utf-8") as f:
        json.dump(metrics["counterfactual_oracles"], f, indent=2)
        
    print("Data extraction complete.")

if __name__ == '__main__':
    main()
