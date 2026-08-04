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
    # Replace any gsk_ token
    res = re.sub(r"gsk_[a-zA-Z0-9]{20,}", "[REDACTED_GROQ_KEY]", res)
    # Redact Authorization headers
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

def main():
    parser = argparse.ArgumentParser(description="Run Groq GPT-OSS 120B bounded-choice reranker (RR-K).")
    parser.add_argument("--split", type=str, required=True, choices=["dev", "eval"], help="Split to run on.")
    parser.add_argument("--query-rep", type=str, default="mention_qty", choices=["mention", "mention_qty", "mention_context"])
    parser.add_argument("--max-retries", type=int, default=5, help="Max retry attempts per case.")
    parser.add_argument("--base-delay", type=float, default=1.5, help="Base delay between requests in seconds.")
    args = parser.parse_args()
    
    check_anti_leakage(args.split)
    
    # Enforce gate: never run EVAL without approved configuration
    if args.split == "eval":
        frozen_config_path = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "frozen_config.json"
        if not frozen_config_path.exists():
            raise RuntimeError("MANDATORY GATE FAILURE: Cannot run RR-K on EVAL before frozen configuration is created and approved!")
        with open(frozen_config_path, "r", encoding="utf-8") as f:
            fc = json.load(f)
        if fc.get("status") != "FROZEN_FOR_EVALUATION" and fc.get("selected_api_model") != "RR-K":
            raise RuntimeError("MANDATORY GATE FAILURE: Cannot run RR-K on EVAL until RR-K configuration is explicitly frozen and approved!")
            
    input_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "data" / f"{args.split}_cases.jsonl"
    check_anti_leakage(str(input_file))
    if not input_file.exists():
        print(f"ERROR: Input case snapshot {input_file} does not exist.")
        sys.exit(1)
        
    out_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "predictions" / f"rr_k_{args.split}_predictions.jsonl"
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
        
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY environment variable not set.")
        sys.exit(1)
        
    try:
        import groq
    except ImportError:
        print("ERROR: groq Python SDK is not installed.")
        sys.exit(1)
        
    client = groq.Groq(api_key=api_key)
    model_name = "openai/gpt-oss-120b"
    
    # Verify model availability
    try:
        avail_models = [m.id for m in client.models.list().data]
        if model_name not in avail_models:
            raise RuntimeError(f"Requested model {model_name} is not available on Groq API. Available: {avail_models}")
        print(f"Verified model availability: {model_name} (Requested: {model_name}, Actual Provider ID: {model_name})")
    except Exception as e:
        print(redact_secret(f"ERROR verifying Groq model availability: {e}", api_key))
        sys.exit(1)
        
    prompt_file = PROJECT_ROOT / "evaluation" / "reranker_benchmark" / "groq_prompt.txt"
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_template = f.read()
    prompt_hash = hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    
    new_preds = []
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
                "method_id": "RR-K",
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
            new_preds.append(p_rec)
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
                        {"role": "user", "content": prompt_text}
                    ],
                    response_format={"type": "json_schema", "json_schema": schema},
                    temperature=0.0
                )
                actual_model_used = resp.model or model_name
                content = resp.choices[0].message.content
                resp_data = json.loads(content)
                
                # Strict schema validation
                if not isinstance(resp_data, dict):
                    raise ValueError(f"Response is not a JSON object: {content}")
                sel = resp_data.get("selected_code")
                if sel not in valid_codes:
                    raise ValueError(f"selected_code '{sel}' not in valid choices {valid_codes}. Fuzzy matching repair is prohibited.")
                conf = resp_data.get("confidence")
                if not isinstance(conf, (int, float)) or not math.isfinite(conf) or not (0.0 <= conf <= 1.0):
                    raise ValueError(f"confidence '{conf}' is not a valid float in [0.0, 1.0]")
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
                    # Check if retry-after in exception text
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
            print(f"[{order_id}:{item_id}] -> SUCCESS ({actual_model_used}, sel={resp_data['selected_code']}, {elapsed_ms}ms, in={in_tokens}, out={out_tokens})", flush=True)
            p_rec = {
                "method_id": "RR-K",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": actual_model_used,
                "prompt_sha256": prompt_hash,
                "schema_sha256": schema_hash,
                "decision": resp_data["selected_code"],
                "selected_code": None if resp_data["selected_code"] in ["REVIEW", "NOT_FOUND"] else resp_data["selected_code"],
                "confidence": float(resp_data["confidence"]),
                "reason_code": resp_data["reason_code"],
                "status": "COMPLETED",
                "processing_time_ms": elapsed_ms,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "total_tokens": tot_tokens,
                "reasoning_tokens": reas_tokens
            }
            new_preds.append(p_rec)
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
        else:
            print(f"[{order_id}:{item_id}] -> FAILED after {args.max_retries} retries: {err_msg}", flush=True)
            p_rec = {
                "method_id": "RR-K",
                "order_id": order_id,
                "item_id": item_id,
                "model_requested": model_name,
                "actual_model_used": "NONE",
                "status": "INCOMPLETE",
                "error": err_msg,
                "processing_time_ms": elapsed_ms
            }
            new_preds.append(p_rec)
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(p_rec, ensure_ascii=False) + "\n")
                
    # Deduplicate and ensure idempotent final state
    all_records = []
    if out_file.exists():
        with open(out_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    all_records.append(json.loads(line))
                    
    dedup = {}
    for r in all_records:
        iid = r.get("item_id")
        if iid not in dedup:
            dedup[iid] = r
        else:
            # Supersede INCOMPLETE with COMPLETED, or take latest COMPLETED
            if dedup[iid].get("status") != "COMPLETED" and r.get("status") == "COMPLETED":
                dedup[iid] = r
            elif dedup[iid].get("status") == "COMPLETED" and r.get("status") == "COMPLETED":
                dedup[iid] = r
                
    final_records = list(dedup.values())
    with open(out_file, "w", encoding="utf-8") as f:
        for r in final_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    print(f"Finished processing. Total unique records in {out_file}: {len(final_records)}")

if __name__ == "__main__":
    main()
