import os
import sys
import json
import time
import argparse
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.reranker_benchmark.attribute_contradictions import AttributeContradictionDetector

def check_anti_leakage(path_str):
    if "/sealed/" in path_str.replace("\\", "/") or "/sealed" in path_str.replace("\\", "/"):
        raise PermissionError(f"ANTI-LEAKAGE VIOLATION: Attempted to access sealed directory: {path_str}")

def get_ram_gb():
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3), 2)
    except Exception:
        return 0.0

def build_query_text(case, rep_type="mention"):
    mention = case.get("extracted_text") or case.get("extracted_product") or ""
    qty = case.get("extracted_quantity", "")
    unit = case.get("extracted_unit", "")
    context = case.get("surrounding_context", "")
    
    if rep_type == "mention":
        return str(mention)
    elif rep_type == "mention_qty":
        return f"{qty} {unit} {mention}".strip()
    elif rep_type == "mention_context":
        return f"{qty} {unit} {mention} [Context: {context}]".strip()
    else:
        return str(mention)

def build_candidate_text(cand):
    pid = cand.get("product_id", "")
    name = cand.get("product_name", "")
    cat = cand.get("category", "")
    pack = cand.get("pack_size", "")
    return f"{pid} - {name} ({cat} - {pack})".strip()

MODELS_CONFIG = {
    "RR-C": {
        "name": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "revision": "main",
        "trust_remote_code": False,
        "license": "Apache-2.0"
    },
    "RR-D": {
        "name": "BAAI/bge-reranker-v2-m3",
        "revision": "main",
        "trust_remote_code": False,
        "license": "MIT"
    },
    "RR-E": {
        "name": "jinaai/jina-reranker-v2-base-multilingual",
        "revision": "main",
        "trust_remote_code": False, # Do not blindly enable trust_remote_code=True per Correction 6
        "license": "CC-BY-NC-4.0"
    }
}

def run_model(method_id, cases, split, query_rep, detector=None):
    config = MODELS_CONFIG.get(method_id)
    if not config:
        return []
    
    print(f"\n--- Running {method_id} ({config['name']}) on split={split} with query_rep={query_rep} ---")
    start_time = time.time()
    start_ram = get_ram_gb()
    
    try:
        from sentence_transformers import CrossEncoder
        import torch
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Loading CrossEncoder on device={device}...")
        model = CrossEncoder(
            config["name"],
            revision=config["revision"],
            trust_remote_code=config["trust_remote_code"],
            device=device
        )
    except Exception as e:
        print(f"ERROR: Failed to load model {method_id} ({config['name']}): {e}")
        print("Per user instructions, marking as INCOMPLETE without silent substitution.")
        return [{"status": "INCOMPLETE", "method_id": method_id, "error": str(e)}]
        
    predictions = []
    for idx, case in enumerate(cases):
        order_id = case.get("order_id")
        item_id = case.get("item_id", idx)
        query_str = build_query_text(case, rep_type=query_rep)
        candidates = case.get("candidates", [])
        
        if not candidates:
            predictions.append({
                "method_id": method_id,
                "order_id": order_id,
                "item_id": item_id,
                "query_rep": query_rep,
                "selected_code": None,
                "top_1_score": 0.0,
                "candidates_scored": [],
                "status": "COMPLETED"
            })
            continue
            
        pairs = [[query_str, build_candidate_text(cand)] for cand in candidates]
        try:
            scores = model.predict(pairs)
        except Exception as e:
            predictions.append({
                "method_id": method_id,
                "order_id": order_id,
                "item_id": item_id,
                "status": "INCOMPLETE",
                "error": f"Prediction failed: {e}"
            })
            continue
            
        scored_cands = []
        for cand, score in zip(candidates, scores):
            base_s = float(score)
            if detector and method_id == "RR-H":
                res = detector.score_candidate(query_str, cand.get("product_name", ""), base_s)
                final_s = res["final_score"]
                contra_info = res["reasons"]
            else:
                final_s = base_s
                contra_info = []
                
            scored_cands.append({
                "product_id": cand.get("product_id"),
                "product_name": cand.get("product_name"),
                "base_score": base_s,
                "final_score": final_s,
                "contradiction_reasons": contra_info,
                "original_rank": cand.get("rank", 999)
            })
            
        scored_cands.sort(key=lambda x: x["final_score"], reverse=True)
        top_cand = scored_cands[0] if scored_cands else None
        
        predictions.append({
            "method_id": method_id,
            "order_id": order_id,
            "item_id": item_id,
            "query_rep": query_rep,
            "selected_code": top_cand["product_id"] if top_cand else None,
            "top_1_score": top_cand["final_score"] if top_cand else 0.0,
            "candidates_scored": scored_cands,
            "status": "COMPLETED"
        })
        
    end_time = time.time()
    end_ram = get_ram_gb()
    print(f"Completed {len(cases)} cases in {round(end_time - start_time, 2)}s. RAM delta: {round(end_ram - start_ram, 2)} GB.")
    return predictions

def main():
    parser = argparse.ArgumentParser(description="Run local cross-encoder rerankers sequentially.")
    parser.add_argument("--split", type=str, required=True, choices=["dev", "eval"], help="Split to run on (dev or eval).")
    parser.add_argument("--query-rep", type=str, default="mention", choices=["mention", "mention_qty", "mention_context"], help="Query representation type.")
    parser.add_argument("--models", nargs="+", default=["RR-C", "RR-D", "RR-E"], help="Models to execute.")
    args = parser.parse_args()
    
    check_anti_leakage(args.split)
    
    # Enforce Correction 3 & Clarifications: Never run EVAL before config is frozen
    if args.split == "eval":
        frozen_config_path = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "frozen_config.json"
        if not frozen_config_path.exists():
            raise RuntimeError("MANDATORY GATE FAILURE: Cannot run EVAL predictions before calibration and config are frozen in frozen_config.json!")
            
    input_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "data" / f"{args.split}_cases.jsonl"
    check_anti_leakage(str(input_file))
    
    if not input_file.exists():
        print(f"ERROR: Input case snapshot {input_file} does not exist. Run build_cases.py first.")
        sys.exit(1)
        
    cases = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
                
    print(f"Loaded {len(cases)} unlabeled cases from {input_file}.")
    detector = AttributeContradictionDetector()
    
    out_dir = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    for m in args.models:
        preds = run_model(m, cases, args.split, args.query_rep, detector=detector)
        out_file = out_dir / f"{m.lower().replace('-', '_')}_{args.split}_predictions.jsonl"
        check_anti_leakage(str(out_file))
        with open(out_file, "w", encoding="utf-8") as f:
            for p in preds:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"Saved {len(preds)} prediction records to {out_file}")

if __name__ == "__main__":
    main()
