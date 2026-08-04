import os
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RERANKER_DIR = PROJECT_ROOT / "evaluation" / "reranker_benchmark"
DATA_DIR = RERANKER_DIR / "data"
PRED_DIR = RERANKER_DIR / "predictions"

from evaluation.reranker_benchmark.run_local_rerankers import AttributeContradictionDetector

def load_jsonl(filepath):
    if not filepath.exists():
        return []
    data = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data

def save_jsonl(filepath, records):
    with open(filepath, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Saved {len(records)} records to {filepath}")

def generate_derived_predictions(split, frozen_config):
    print(f"\n--- Generating Derived/Cascade Predictions (RR-F, RR-H, RR-I, RR-J) for split: {split.upper()} ---")
    cases_file = DATA_DIR / f"{split}_cases.jsonl"
    cases = load_jsonl(cases_file)
    if not cases:
        print(f"No cases found in {cases_file}. Skipping derived generation for {split}.")
        return

    best_local = frozen_config.get("selected_local_model", "RR-D")
    best_local_id = best_local.lower().replace("-", "_")
    local_pred_file = PRED_DIR / f"{best_local_id}_{split}_predictions.jsonl"
    local_preds = load_jsonl(local_pred_file)
    if not local_preds:
        print(f"WARNING: No local predictions found at {local_pred_file}. Cannot generate derived methods.")
        return
    local_map = {p["item_id"]: p for p in local_preds if "item_id" in p}

    gemini_pred_file = PRED_DIR / f"rr_g_{split}_predictions.jsonl"
    gemini_preds = load_jsonl(gemini_pred_file)
    gemini_map = {p["item_id"]: p for p in gemini_preds if "item_id" in p}

    fusion_weights = frozen_config.get("fusion_weights", {"retrieval_weight": 0.4, "local_weight": 0.6})
    lw = float(fusion_weights.get("local_weight", 0.6))
    rw = float(fusion_weights.get("retrieval_weight", 0.4))
    thresholds = frozen_config.get("calibrated_thresholds", {
        "acceptance_threshold": 0.85,
        "review_threshold": 0.40,
        "score_margin_threshold": 0.15
    })
    accept_th = float(thresholds.get("acceptance_threshold", 0.85))
    margin_th = float(thresholds.get("score_margin_threshold", 0.15))

    detector = AttributeContradictionDetector()

    rr_f_preds, rr_h_preds, rr_i_preds, rr_j_preds = [], [], [], []

    for case in cases:
        iid = case["item_id"]
        order_id = case["order_id"]
        candidates = case.get("candidates", [])
        l_pred = local_map.get(iid)
        g_pred = gemini_map.get(iid)

        if not l_pred or l_pred.get("status") != "COMPLETED":
            inc = {"method_id": "UNKNOWN", "order_id": order_id, "item_id": iid, "status": "INCOMPLETE", "error": "Missing local prediction"}
            for p_list, m_id in [(rr_f_preds, "RR-F"), (rr_h_preds, "RR-H"), (rr_i_preds, "RR-I"), (rr_j_preds, "RR-J")]:
                p_copy = dict(inc)
                p_copy["method_id"] = m_id
                p_list.append(p_copy)
            continue

        # 1. RR-F: Local + Retrieval Fusion
        local_scored = l_pred.get("candidates_scored", [])
        l_score_map = {c["product_id"]: float(c.get("final_score", c.get("base_score", 0.0))) for c in local_scored}
        r_score_map = {c["product_id"]: float(c.get("final_method_j_score", c.get("score", 0.0))) for c in candidates}

        l_vals = list(l_score_map.values())
        r_vals = list(r_score_map.values())
        min_l, max_l = (min(l_vals), max(l_vals)) if l_vals else (0.0, 0.0)
        min_r, max_r = (min(r_vals), max(r_vals)) if r_vals else (0.0, 0.0)

        scored_cands_f = []
        for c in candidates:
            pid = c["product_id"]
            ls = l_score_map.get(pid, 0.0)
            rs = r_score_map.get(pid, 0.0)
            norm_l = (ls - min_l) / (max_l - min_l) if max_l > min_l else 0.0
            norm_r = (rs - min_r) / (max_r - min_r) if max_r > min_r else 0.0
            fused = lw * norm_l + rw * norm_r
            scored_cands_f.append({
                "product_id": pid,
                "product_name": c.get("product_name", ""),
                "base_score": ls,
                "final_score": round(fused, 6),
                "contradiction_reasons": [],
                "original_rank": c.get("rank", 999)
            })
        scored_cands_f.sort(key=lambda x: (-x["final_score"], x["original_rank"]))
        top_f = scored_cands_f[0] if scored_cands_f else None
        rr_f_preds.append({
            "method_id": "RR-F",
            "order_id": order_id,
            "item_id": iid,
            "query_rep": l_pred.get("query_rep", "mention"),
            "selected_code": top_f["product_id"] if top_f else None,
            "top_1_score": top_f["final_score"] if top_f else 0.0,
            "candidates_scored": scored_cands_f,
            "status": "COMPLETED",
            "processing_time_ms": l_pred.get("processing_time_ms", 0.0),
            "estimated_cost_usd": 0.0
        })

        # 2. RR-H: Local + Contradiction Penalties
        scored_cands_h = []
        for c in local_scored:
            res = detector.score_candidate(case.get("extracted_text", ""), c.get("product_name", ""), c.get("base_score", 0.0))
            scored_cands_h.append({
                "product_id": c["product_id"],
                "product_name": c.get("product_name", ""),
                "base_score": c.get("base_score", 0.0),
                "final_score": res["final_score"],
                "contradiction_reasons": res.get("reasons", []),
                "original_rank": c.get("original_rank", 999)
            })
        scored_cands_h.sort(key=lambda x: (-x["final_score"], x["original_rank"]))
        top_h = scored_cands_h[0] if scored_cands_h else None
        rr_h_preds.append({
            "method_id": "RR-H",
            "order_id": order_id,
            "item_id": iid,
            "query_rep": l_pred.get("query_rep", "mention"),
            "selected_code": top_h["product_id"] if top_h else None,
            "top_1_score": top_h["final_score"] if top_h else 0.0,
            "candidates_scored": scored_cands_h,
            "status": "COMPLETED",
            "processing_time_ms": l_pred.get("processing_time_ms", 0.0),
            "estimated_cost_usd": 0.0
        })

        # 3. RR-I: Cost-aware Cascade
        top_l_score = float(l_pred.get("top_1_score", 0.0))
        if len(local_scored) >= 2:
            margin = float(local_scored[0]["final_score"]) - float(local_scored[1]["final_score"])
        else:
            margin = 1.0

        if top_l_score >= accept_th and margin >= margin_th:
            p_i = dict(l_pred)
            p_i["method_id"] = "RR-I"
            p_i["cascade_tier"] = "TIER_1_LOCAL_AUTO_ACCEPT"
            rr_i_preds.append(p_i)
        else:
            if g_pred and g_pred.get("status") == "COMPLETED":
                p_i = dict(g_pred)
                p_i["method_id"] = "RR-I"
                p_i["cascade_tier"] = "TIER_2_GEMINI_ESCALATION"
                rr_i_preds.append(p_i)
            else:
                p_i = dict(l_pred)
                p_i["method_id"] = "RR-I"
                p_i["cascade_tier"] = "TIER_1_LOCAL_FALLBACK"
                rr_i_preds.append(p_i)

        # 4. RR-J: Local-only Safe Cascade
        p_j = dict(l_pred)
        p_j["method_id"] = "RR-J"
        p_j["cascade_tier"] = "TIER_1_LOCAL_SAFE_CASCADE"
        rr_j_preds.append(p_j)

    save_jsonl(PRED_DIR / f"rr_f_{split}_predictions.jsonl", rr_f_preds)
    save_jsonl(PRED_DIR / f"rr_h_{split}_predictions.jsonl", rr_h_preds)
    save_jsonl(PRED_DIR / f"rr_i_{split}_predictions.jsonl", rr_i_preds)
    save_jsonl(PRED_DIR / f"rr_j_{split}_predictions.jsonl", rr_j_preds)
    print(f"Derived predictions for {split.upper()} generated successfully.")
