#!/usr/bin/env python3
"""
Leakage-Resistant Live Prediction Generator for Wholesale Order Evaluation.

Executes blind order messages through the real pipeline (GeminiExtractor + ProductMatcher).
Stores isolated prediction records and enforces strict anti-leakage phase barriers.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_predictions")

def get_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def verify_manifest_hashes(manifest_path: Path):
    if not manifest_path.exists():
        raise RuntimeError(f"Manifest file missing: {manifest_path}. Run generate_manifest.py first.")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
        
    for rel_path, expected_hash in manifest.get("file_hashes", {}).items():
        if "sealed" in rel_path:
            continue  # prediction module does not touch sealed files
        full_p = PROJECT_ROOT / rel_path
        actual_hash = get_file_sha256(full_p)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"HASH MISMATCH DETECTED for {rel_path}!\n"
                f"Expected: {expected_hash}\n"
                f"Actual:   {actual_hash}\n"
                "Execution aborted to prevent unrecorded modifications."
            )
    logger.info("Manifest source and input hashes verified successfully.")

def parse_args():
    parser = argparse.ArgumentParser(description="Run live evaluation predictions.")
    parser.add_argument("--catalog", required=True, type=str, help="Path to synthetic catalog CSV")
    parser.add_argument("--orders", required=True, type=str, help="Path to blind orders JSONL")
    parser.add_argument("--output", required=True, type=str, help="Path to output predictions JSONL")
    parser.add_argument("--db", required=True, type=str, help="Path to isolated SQLite database")
    parser.add_argument("--run-id", required=True, type=str, help="Unique run identifier")
    
    args = parser.parse_args()
    
    # Strict Anti-Leakage Check: reject sealed ground truth paths
    for arg_val in [args.catalog, args.orders, args.output, args.db]:
        if "sealed" in arg_val.lower():
            raise ValueError(f"STRICT ANTI-LEAKAGE FAILURE: Access to sealed ground truth is prohibited! Path: {arg_val}")
            
    return args

def compute_prompt_sha256() -> str:
    from app.extractor import EXTRACTION_PROMPT
    return hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest()

def run_predictions():
    args = parse_args()
    
    catalog_path = Path(args.catalog).resolve()
    orders_path = Path(args.orders).resolve()
    output_path = Path(args.output).resolve()
    db_path = Path(args.db).resolve()
    run_id = args.run_id
    
    # 1. Verify Manifest Hashes
    manifest_path = PROJECT_ROOT / "evaluation" / "manifest.json"
    verify_manifest_hashes(manifest_path)
    
    # 2. Set environment variables for isolated catalog and DB
    os.environ["CATALOG_PATH"] = str(catalog_path)
    
    # Import app components AFTER CATALOG_PATH is set
    from app.catalog import load_catalog
    from app.matcher import ProductMatcher
    from app.extractor import GeminiExtractor
    from app import database
    
    # Initialize DB (remove old eval DB if exists for fresh run)
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception as e:
            logger.warning(f"Could not remove old DB file: {e}")
    database.init_db(str(db_path))
    
    catalog = load_catalog(str(catalog_path))
    matcher = ProductMatcher(catalog)
    logger.info(f"Loaded {len(catalog)} products from synthetic catalog.")
    
    extractor = GeminiExtractor()
    prompt_hash = compute_prompt_sha256()
    
    # Read blind orders
    orders = []
    with open(orders_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                orders.append(json.loads(line))
                
    logger.info(f"Loaded {len(orders)} blind orders to process for run_id={run_id}.")
    
    # Process orders with retry and backoff
    predictions = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    for idx, order_rec in enumerate(orders):
        order_id = order_rec["order_id"]
        raw_msg = order_rec["raw_message"]
        
        logger.info(f"[{idx+1}/{len(orders)}] Processing order_id={order_id}...")
        
        start_t = time.perf_counter()
        prediction_record = None
        max_retries = 3
        retry_count = 0
        last_error = None
        actual_model_used = extractor.candidate_models[0]
        
        for attempt in range(max_retries + 1):
            try:
                extraction_res = extractor.extract(raw_msg)
                line_results = matcher.match_items(extraction_res.items)
                
                elapsed_ms = (time.perf_counter() - start_t) * 1000.0
                
                items_serialized = []
                for lr in line_results:
                    items_serialized.append({
                        "raw_text": lr.raw_text,
                        "extracted_product": lr.extracted_product,
                        "extracted_quantity": lr.extracted_quantity,
                        "extracted_unit": lr.extracted_unit,
                        "matched_product_id": lr.matched_product.product_id if lr.matched_product else None,
                        "matched_product_name": lr.matched_product.product_name if lr.matched_product else None,
                        "confidence": lr.confidence,
                        "status": lr.status,
                        "reason": lr.reason,
                        "candidates": [
                            {
                                "product_id": c.product_id,
                                "product_name": c.product_name,
                                "score": c.score,
                                "unit": c.unit,
                                "price": c.price
                            } for c in lr.candidates
                        ]
                    })
                    
                prediction_record = {
                    "run_id": run_id,
                    "order_id": order_id,
                    "prediction_timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_requested": extractor.candidate_models[0],
                    "actual_model_used": actual_model_used,
                    "temperature": 0.1,
                    "max_output_tokens": 2048,
                    "extraction_prompt_sha256": prompt_hash,
                    "processing_time_ms": round(elapsed_ms, 2),
                    "retry_count": retry_count,
                    "api_error": None,
                    "raw_message": raw_msg,
                    "items": items_serialized,
                    "unresolved_text": extraction_res.unresolved_text
                }
                break
                
            except Exception as e:
                retry_count += 1
                last_error = str(e)
                logger.warning(f"Order {order_id} attempt {attempt+1} failed: {e}")
                if attempt < max_retries:
                    sleep_s = 2.0 ** attempt
                    time.sleep(sleep_s)
                else:
                    elapsed_ms = (time.perf_counter() - start_t) * 1000.0
                    prediction_record = {
                        "run_id": run_id,
                        "order_id": order_id,
                        "prediction_timestamp": datetime.now(timezone.utc).isoformat(),
                        "model_requested": extractor.candidate_models[0],
                        "actual_model_used": actual_model_used,
                        "temperature": 0.1,
                        "max_output_tokens": 2048,
                        "extraction_prompt_sha256": prompt_hash,
                        "processing_time_ms": round(elapsed_ms, 2),
                        "retry_count": retry_count,
                        "api_error": last_error,
                        "raw_message": raw_msg,
                        "items": [],
                        "unresolved_text": [raw_msg]
                    }

        predictions.append(prediction_record)

    # Write predictions to disk
    with open(output_path, "w", encoding="utf-8") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")
            
    logger.info(f"Wrote {len(predictions)} prediction records to {output_path}")
    
    # Compute prediction file hash and update manifest.json
    pred_hash = get_file_sha256(output_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    manifest_data["prediction_file_hash"] = pred_hash
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)
        
    logger.info(f"Updated manifest with prediction file hash: {pred_hash}")

if __name__ == "__main__":
    run_predictions()
