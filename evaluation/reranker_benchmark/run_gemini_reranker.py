import os
import sys
import json
import time
import argparse
import hashlib
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")

def check_anti_leakage(path_str):
    if "/sealed/" in path_str.replace("\\", "/") or "/sealed" in path_str.replace("\\", "/"):
        raise PermissionError(f"ANTI-LEAKAGE VIOLATION: Attempted to access sealed directory: {path_str}")

PRICING_SNAPSHOT = {
    "source": "Google AI Studio Pricing Snapshot (2026-07-25)",
    "input_cost_per_m_tokens": 1.25,
    "output_cost_per_m_tokens": 5.00,
    "currency": "USD"
}

def estimate_cost(input_tokens, output_tokens):
    in_cost = (input_tokens / 1_000_000.0) * PRICING_SNAPSHOT["input_cost_per_m_tokens"]
    out_cost = (output_tokens / 1_000_000.0) * PRICING_SNAPSHOT["output_cost_per_m_tokens"]
    return round(in_cost + out_cost, 6)

def format_candidates(candidates):
    lines = []
    for c in candidates:
        pid = c.get("product_id", "")
        name = c.get("product_name", "")
        cat = c.get("category", "")
        pack = c.get("pack_size", "")
        lines.append(f"- {pid}: {name} (Category: {cat}, Pack: {pack})")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Run Gemini bounded-choice reranker.")
    parser.add_argument("--split", type=str, required=True, choices=["dev", "eval"], help="Split to run on.")
    parser.add_argument("--query-rep", type=str, default="mention", choices=["mention", "mention_qty", "mention_context"])
    args = parser.parse_args()
    
    check_anti_leakage(args.split)
    
    # Enforce Correction 3 & Mandatory Clarification: Never call Gemini on EVAL before prompt and config are frozen
    if args.split == "eval":
        frozen_config_path = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "frozen_config.json"
        if not frozen_config_path.exists():
            raise RuntimeError("MANDATORY GATE FAILURE: Cannot call Gemini on EVAL before prompt and model configuration are frozen!")
            
    input_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "data" / f"{args.split}_cases.jsonl"
    check_anti_leakage(str(input_file))
    if not input_file.exists():
        print(f"ERROR: Input case snapshot {input_file} does not exist.")
        sys.exit(1)
        
    out_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions" / f"rr_g_{args.split}_predictions.jsonl"
    check_anti_leakage(str(out_file))
    
    completed_items = set()
    existing_preds = []
    if out_file.exists():
        with open(out_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    p = json.loads(line)
                    if p.get("status") == "COMPLETED":
                        completed_items.add(p.get("item_id"))
                        existing_preds.append(p)
        print(f"Resumable execution: found {len(completed_items)} completed items in {out_file}.")
        
    cases = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                c = json.loads(line)
                if c.get("item_id") not in completed_items:
                    cases.append(c)
                    
    print(f"Remaining cases to process: {len(cases)}")
    if not cases:
        print("All cases already processed.")
        return
        
    # Enforce Correction 2: Explicit API Model Config
    model_name = os.environ.get("GEMINI_RERANKER_MODEL", "gemini-3.5-flash")
    print(f"Using explicit API model: {model_name} (Requested: {model_name})")
    
    prompt_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "gemini_prompt.txt"
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_template = f.read()
        
    prompt_hash = hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    
    # Check if google.generativeai is available and setup key/model pool
    try:
        import google.generativeai as genai
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        api_keys = [env_key.strip()] if env_key and env_key.strip() else []
        if not api_keys:
            raise ValueError("No API keys found.")
        candidate_models = [model_name, "gemini-flash-latest", "gemini-1.5-flash", "gemini-2.0-flash-lite"]
        api_pool = [(k, m) for m in candidate_models for k in api_keys]
        api_available = True
        print(f"Initialized API pool with {len(api_keys)} keys across {len(candidate_models)} models ({len(api_pool)} total slots).")
    except Exception as e:
        print(f"WARNING: Gemini API initialization failed ({e}). Per user instructions, failed/incomplete model will be marked INCOMPLETE.")
        api_available = False
        api_pool = []
        
    new_preds = []
    for case in cases:
        order_id = case.get("order_id")
        item_id = case.get("item_id")
        candidates = case.get("candidates", [])
        
        if not candidates:
            new_preds.append({
                "method_id": "RR-G",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": model_name,
                "selected_code": None,
                "confidence": 0.0,
                "reason_codes": ["NO_CANDIDATES"],
                "status": "COMPLETED",
                "processing_time_ms": 0.0,
                "estimated_cost_usd": 0.0
            })
            continue
            
        if not api_available:
            new_preds.append({
                "method_id": "RR-G",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": "NONE",
                "status": "INCOMPLETE",
                "error": "API unavailable or missing key."
            })
            continue
            
        # Format prompt
        mention = case.get("extracted_text") or case.get("extracted_product") or ""
        qty = str(case.get("extracted_quantity", ""))
        unit = str(case.get("extracted_unit", ""))
        context = str(case.get("surrounding_context", ""))
        cands_str = format_candidates(candidates)
        
        prompt_text = prompt_template.replace("{{extracted_product}}", mention)
        prompt_text = prompt_text.replace("{{extracted_quantity}}", qty)
        prompt_text = prompt_text.replace("{{extracted_unit}}", unit)
        prompt_text = prompt_text.replace("{{context}}", context)
        prompt_text = prompt_text.replace("{{candidates_list}}", cands_str)
        
        start_t = time.time()
        success = False
        resp_data = None
        err_msg = ""
        actual_model_used = model_name
        
        for k, m in api_pool:
            try:
                genai.configure(api_key=k)
                mod = genai.GenerativeModel(
                    model_name=m,
                    generation_config={"temperature": 0.0, "response_mime_type": "application/json"}
                )
                response = mod.generate_content(prompt_text)
                resp_text = response.text
                resp_data = json.loads(resp_text)
                success = True
                actual_model_used = m
                break
            except Exception as e:
                err_msg = str(e)
                continue
                
        elapsed_ms = round((time.time() - start_t) * 1000.0, 2)
        if success:
            print(f"[{order_id}:{item_id}] -> SUCCESS ({actual_model_used}, {elapsed_ms}ms)", flush=True)
        else:
            print(f"[{order_id}:{item_id}] -> FAILED across all pool slots: {err_msg}", flush=True)
        
        if success and resp_data:
            # Estimate tokens roughly if metadata unavailable
            in_tokens = len(prompt_text) // 4
            out_tokens = len(str(resp_data)) // 4
            cost = estimate_cost(in_tokens, out_tokens)
            
            sel_code = resp_data.get("product_code")
            # Enforce candidate constraint
            valid_ids = {c["product_id"] for c in candidates}
            if sel_code and sel_code not in valid_ids:
                sel_code = None
                
            p_rec = {
                "method_id": "RR-G",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": actual_model_used,
                "prompt_sha256": prompt_hash,
                "selected_code": sel_code,
                "confidence": float(resp_data.get("confidence", 0.0)),
                "reason_codes": resp_data.get("reason_codes", []),
                "status": "COMPLETED",
                "processing_time_ms": elapsed_ms,
                "estimated_cost_usd": cost,
                "pricing_snapshot": PRICING_SNAPSHOT["source"]
            }
            new_preds.append(p_rec)
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
        else:
            p_rec = {
                "method_id": "RR-G",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": "NONE",
                "status": "INCOMPLETE",
                "error": f"Failed across all pool slots: {err_msg}",
                "processing_time_ms": elapsed_ms
            }
            new_preds.append(p_rec)
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
            
    all_preds = existing_preds + new_preds
    print(f"Completed processing {len(new_preds)} new records. Total records in {out_file}: {len(all_preds)}")
    
    if args.split == "eval":
        from evaluation.reranker_benchmark.cascade_utils import generate_derived_predictions
        frozen_config_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "frozen_config.json"
        if frozen_config_file.exists():
            with open(frozen_config_file, "r", encoding="utf-8") as f:
                fc = json.load(f)
            generate_derived_predictions("eval", fc)

if __name__ == "__main__":
    main()
