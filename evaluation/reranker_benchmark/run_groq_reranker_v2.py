import os
import sys
import json
import time
import math
import random
import argparse
import hashlib
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.reranker_benchmark.attribute_contradictions import AttributeContradictionDetector

def check_anti_leakage(path_str):
    norm_path = path_str.replace("\\", "/")
    if "/sealed/" in norm_path or "/sealed" in norm_path:
        raise PermissionError(f"ANTI-LEAKAGE VIOLATION: Attempted to access sealed directory: {path_str}")

REASON_CODES = [
    "EXACT_MATCH",
    "HIGH_CONFIDENCE_MATCH",
    "PARTIAL_MATCH",
    "AMBIGUOUS_CANDIDATES",
    "MISSING_ATTRIBUTES",
    "NO_MATCHING_CANDIDATE",
    "OUT_OF_CATALOG",
    "PRICE_OR_PACK_MISMATCH"
]

def redact_secret(text, secret_key=None):
    if not text:
        return text
    res = str(text)
    if secret_key and len(secret_key) > 5:
        res = res.replace(secret_key, "[REDACTED_GROQ_KEY]")
    res = re.sub(r"gsk_[a-zA-Z0-9]{20,}", "[REDACTED_GROQ_KEY]", res)
    res = re.sub(r"(Authorization\s*:\s*Bearer\s+)[^\s'\"]+", r"\1[REDACTED_GROQ_KEY]", res, flags=re.IGNORECASE)
    return res

def format_candidates(candidates):
    lines = []
    for c in candidates:
        pid = c.get("product_id", "")
        name = c.get("product_name", "")
        cat = c.get("category", "")
        pack = c.get("pack_size", "")
        lines.append(f"- {pid}: {name} (Category: {cat}, Pack: {pack})")
    return "\n".join(lines)

def build_schema(valid_codes):
    return {
        "name": "RerankDecision",
        "schema": {
            "type": "object",
            "properties": {
                "selected_code": {
                    "type": "string",
                    "enum": valid_codes
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0
                },
                "reason_code": {
                    "type": "string",
                    "enum": REASON_CODES
                }
            },
            "required": ["selected_code", "confidence", "reason_code"],
            "additionalProperties": False
        }
    }

def get_api_key():
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    key_path = Path(r"C:\Users\moham\.gemini\antigravity-ide\brain\eb731c0e-f9d2-4b1e-9afd-4f5ab618d726\scratch\key.dat")
    if key_path.exists():
        key = key_path.read_text("utf-8").strip()
        os.environ["GROQ_API_KEY"] = key
        try:
            key_path.unlink()
        except Exception:
            pass
        return key
    return None

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        
    parser = argparse.ArgumentParser(description="Run Groq GPT-OSS 120B RR-K-v2 ablation.")
    parser.add_argument("--config-id", type=str, required=True, choices=["RR-K-v2-A", "RR-K-v2-B"], help="Config ID.")
    parser.add_argument("--reasoning-effort", type=str, required=True, choices=["low", "medium", "high"], help="Groq reasoning effort.")
    parser.add_argument("--split", type=str, default="dev", choices=["dev"], help="Split to run on (dev only).")
    parser.add_argument("--max-retries", type=int, default=5, help="Max retries.")
    parser.add_argument("--base-delay", type=float, default=1.5, help="Delay between requests.")
    args = parser.parse_args()
    
    check_anti_leakage(args.split)
    if args.split != "dev":
        raise RuntimeError("MANDATORY GATE FAILURE: Held-out EVAL execution is strictly prohibited during DEV ablation!")
        
    input_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "data" / "dev_cases.jsonl"
    check_anti_leakage(str(input_file))
    if not input_file.exists():
        print(f"ERROR: Input case snapshot {input_file} does not exist.")
        sys.exit(1)
        
    out_filename = "rr_k_v2_a_dev_predictions.jsonl" if args.config_id == "RR-K-v2-A" else "rr_k_v2_b_dev_predictions.jsonl"
    if args.config_id == "RR-K-v2-A":
        out_filename = "rr_k_v2_a_dev_predictions.jsonl"
    else:
        out_filename = "rr_k_v2_b_dev_predictions.jsonl"
    out_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions" / out_filename
    check_anti_leakage(str(out_file))
    
    completed_items = set()
    if out_file.exists():
        with open(out_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    p = json.loads(line)
                    if p.get("status") == "COMPLETED":
                        completed_items.add(p.get("item_id"))
        print(f"Resumable execution ({args.config_id}): found {len(completed_items)} completed items in {out_file}.")
        
    cases = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                c = json.loads(line)
                if c.get("item_id") not in completed_items:
                    cases.append(c)
                    
    print(f"[{args.config_id} | effort={args.reasoning_effort}] Remaining cases to process: {len(cases)}")
    if not cases:
        print("All cases already processed.")
        return
        
    api_key = get_api_key()
    if not api_key:
        print("ERROR: GROQ_API_KEY environment variable not set and scratch/key.dat not found.")
        sys.exit(1)
        
    try:
        import groq
    except ImportError:
        print("ERROR: groq Python SDK is not installed.")
        sys.exit(1)
        
    client = groq.Groq(api_key=api_key)
    model_name = "openai/gpt-oss-120b"
    detector = AttributeContradictionDetector()
    
    prompt_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "groq_prompt_v2.txt"
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_template = f.read()
    prompt_hash = hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    
    for case_idx, case in enumerate(cases):
        order_id = case.get("order_id")
        item_id = case.get("item_id")
        candidates = case.get("candidates", [])
        
        cand_ids = [str(c.get("product_id", "")) for c in candidates if c.get("product_id")]
        valid_codes = cand_ids + ["REVIEW", "NOT_FOUND"]
        schema = build_schema(valid_codes)
        schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()
        
        if not candidates:
            p_rec = {
                "method_id": args.config_id,
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": model_name,
                "prompt_sha256": prompt_hash,
                "schema_sha256": schema_hash,
                "decision": "NOT_FOUND",
                "selected_code": None,
                "confidence": 1.0,
                "reason_code": "NO_MATCHING_CANDIDATE",
                "status": "COMPLETED",
                "processing_time_ms": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0
            }
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
            continue
            
        mention = case.get("extracted_text") or case.get("extracted_product") or ""
        qty = str(case.get("extracted_quantity", ""))
        unit = str(case.get("extracted_unit", ""))
        context = str(case.get("surrounding_context", ""))
        cands_str = format_candidates(candidates)
        
        prompt_text = prompt_template.replace("{{extracted_product}}", mention)\
                                     .replace("{{extracted_quantity}}", qty)\
                                     .replace("{{extracted_unit}}", unit)\
                                     .replace("{{context}}", context)\
                                     .replace("{{candidates_list}}", cands_str)
                                     
        start_t = time.time()
        success = False
        resp_data = None
        err_msg = ""
        actual_model_used = model_name
        in_tokens = 0
        out_tokens = 0
        tot_tokens = 0
        reas_tokens = 0
        
        for attempt in range(1, args.max_retries + 1):
            try:
                if args.base_delay > 0:
                    time.sleep(args.base_delay)
                    
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": "You are a precise JSON-outputting assistant for wholesale food catalog matching."},
                        {"role": "user", "content": "You are an expert wholesale food and beverage distribution catalog matcher in Egypt.\nYour task is to select the single best matching product from a provided list of Top-5 candidate products for a customer order line item, or decide if manual review or rejection is required.\n\n" + prompt_text}
                    ],
                    response_format={"type": "json_schema", "json_schema": schema},
                    temperature=0.0,
                    reasoning_effort=args.reasoning_effort
                )
                actual_model_used = resp.model or model_name
                content = resp.choices[0].message.content
                resp_data = json.loads(content)
                
                if not isinstance(resp_data, dict):
                    raise ValueError(f"Response is not a JSON object: {content}")
                sel = resp_data.get("selected_code")
                if sel not in valid_codes:
                    raise ValueError(f"selected_code '{sel}' not in valid choices {valid_codes}.")
                conf = resp_data.get("confidence")
                if not isinstance(conf, (int, float)) or not math.isfinite(conf) or not (0.0 <= conf <= 1.0):
                    raise ValueError(f"confidence '{conf}' is not valid float.")
                rc = resp_data.get("reason_code")
                if rc not in REASON_CODES:
                    raise ValueError(f"reason_code '{rc}' is not in REASON_CODES")
                    
                if resp.usage:
                    in_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
                    out_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
                    tot_tokens = getattr(resp.usage, "total_tokens", 0) or (in_tokens + out_tokens)
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    if details:
                        reas_tokens = getattr(details, "reasoning_tokens", 0) or 0
                        
                success = True
                break
            except Exception as e:
                err_msg = redact_secret(str(e), api_key)
                is_rate_limit = "429" in err_msg or "rate" in err_msg.lower() or "limit" in err_msg.lower()
                if is_rate_limit:
                    wait_s = min(60.0, (2 ** attempt) + random.uniform(0.5, 1.5))
                    m_retry = re.search(r"retry\s*in\s*([0-9.]+)\s*s", err_msg, re.IGNORECASE)
                    if m_retry:
                        try:
                            wait_s = float(m_retry.group(1)) + random.uniform(0.5, 1.5)
                        except ValueError:
                            pass
                    print(f"[{order_id}:{item_id}] Rate limit hit on attempt {attempt}/{args.max_retries}. Waiting {wait_s:.2f}s...", flush=True)
                    time.sleep(wait_s)
                else:
                    wait_s = min(30.0, (2 ** attempt) + random.uniform(0.1, 0.5))
                    print(f"[{order_id}:{item_id}] Error on attempt {attempt}/{args.max_retries}: {err_msg}. Waiting {wait_s:.2f}s...", flush=True)
                    time.sleep(wait_s)
                    
        elapsed_ms = round((time.time() - start_t) * 1000.0, 2)
        if success and resp_data:
            sel = resp_data.get("selected_code")
            conf = float(resp_data.get("confidence", 0.0))
            rc = resp_data.get("reason_code", "EXACT_MATCH")
            
            # --- DETERMINISTIC PYTHON-SIDE CONTRADICTION GUARDS ---
            q_text = f"{qty} {unit} {mention} {case.get('extracted_product', '')} {context}"
            q_attrs = detector.parse_attributes(q_text)
            
            if sel not in ["REVIEW", "NOT_FOUND"] and sel is not None:
                selected_cand = next((c for c in candidates if str(c.get("product_id")) == str(sel)), None)
                if selected_cand:
                    c_text = " ".join([str(selected_cand.get(k, "")) for k in ["product_name", "brand", "category", "size", "unit", "pack_size", "aliases"] if selected_cand.get(k)])
                    c_attrs = detector.parse_attributes(c_text)
                    is_contra, reasons, pen = detector.check_contradictions(q_attrs, c_attrs)
                    if is_contra:
                        print(f"[{order_id}:{item_id}] Deterministic Guard Triggered on {sel}: {reasons}. Overriding to REVIEW.", flush=True)
                        sel = "REVIEW"
                        conf = min(conf, 0.39)
                        rc = "PRICE_OR_PACK_MISMATCH" if any("volume" in r or "weight" in r or "dimension" in r or "pack" in r for r in reasons) else "AMBIGUOUS_CANDIDATES"
            elif sel == "NOT_FOUND":
                # Guard against false NOT_FOUND when brand or size was simply omitted in query
                if not q_attrs["brands"] and len(candidates) > 0:
                    print(f"[{order_id}:{item_id}] Deterministic Guard Triggered on NOT_FOUND (Omitted Brand in query). Overriding to REVIEW.", flush=True)
                    sel = "REVIEW"
                    conf = min(conf, 0.39)
                    rc = "MISSING_ATTRIBUTES"
            
            if sel == "REVIEW":
                dec = "REVIEW"
                sel_code = None
            elif sel == "NOT_FOUND":
                dec = "NOT_FOUND"
                sel_code = None
            else:
                dec = "SELECT"
                sel_code = sel
                
            p_rec = {
                "method_id": args.config_id,
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": actual_model_used,
                "prompt_sha256": prompt_hash,
                "schema_sha256": schema_hash,
                "decision": dec,
                "selected_code": sel_code,
                "confidence": round(conf, 4),
                "reason_code": rc,
                "status": "COMPLETED",
                "processing_time_ms": elapsed_ms,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": tot_tokens,
                "reasoning_tokens": reas_tokens,
                "retries_attempted": attempt - 1,
                "provider_errors": []
            }
        else:
            p_rec = {
                "method_id": args.config_id,
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": actual_model_used,
                "prompt_sha256": prompt_hash,
                "schema_sha256": schema_hash,
                "decision": "REVIEW",
                "selected_code": None,
                "confidence": 0.0,
                "reason_code": "AMBIGUOUS_CANDIDATES",
                "status": "INCOMPLETE",
                "error": err_msg,
                "processing_time_ms": elapsed_ms,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": tot_tokens,
                "reasoning_tokens": reas_tokens,
                "retries_attempted": args.max_retries,
                "provider_errors": [err_msg]
            }
            print(f"[{order_id}:{item_id}] FAILED after {args.max_retries} retries: {err_msg}", flush=True)
            
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
            
        if (case_idx + 1) % 5 == 0 or case_idx == len(cases) - 1:
            print(f"[{args.config_id}] Progress: {case_idx + 1}/{len(cases)} cases completed.", flush=True)
            
    if "GROQ_API_KEY" in os.environ:
        del os.environ["GROQ_API_KEY"]
    print(f"[{args.config_id}] Done processing all cases.")

if __name__ == "__main__":
    main()
