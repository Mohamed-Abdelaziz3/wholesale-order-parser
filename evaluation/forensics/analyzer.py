import json
import hashlib
import os
import csv
from collections import defaultdict
from datetime import datetime

EVAL_DIR = r"c:\Users\moham\OneDrive\Documents\Project @\wholesale-order-parser\evaluation"
FORENSICS_DIR = os.path.join(EVAL_DIR, "forensics")

def sha256_file(filepath):
    if not os.path.exists(filepath): return None
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def main():
    # Load manifest
    manifest_path = os.path.join(EVAL_DIR, "manifest.json")
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
        
    print("Hashes from manifest:")
    print(json.dumps(manifest['file_hashes'], indent=2))
    
    current_hashes = {}
    for rel_path, expected_hash in manifest['file_hashes'].items():
        abs_path = os.path.join(r"c:\Users\moham\OneDrive\Documents\Project @\wholesale-order-parser", rel_path)
        current_hashes[rel_path] = sha256_file(abs_path)
        if current_hashes[rel_path] != expected_hash:
            print(f"HASH MISMATCH for {rel_path}!")
        else:
            print(f"Hash match for {rel_path}: {expected_hash}")
            
    # Load predictions
    preds_path = os.path.join(EVAL_DIR, "results", "blind_predictions.jsonl")
    preds = []
    with open(preds_path, 'r', encoding='utf-8') as f:
        for line in f:
            preds.append(json.loads(line))
            
    # Load ground truth
    gt_path = os.path.join(EVAL_DIR, "sealed", "blind_ground_truth.jsonl")
    gt = {}
    with open(gt_path, 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            gt[row['order_id']] = row

    print(f"Loaded {len(preds)} predictions and {len(gt)} ground truths.")

    # Dump a quick summary of the predictions and candidates
    num_candidates_found = sum(len(item.get('candidates', [])) for p in preds for item in p.get('items', []))
    print(f"Total candidates found in predictions array: {num_candidates_found}")

    for p in preds[:5]:
        print(f"Order {p['order_id']} items:")
        for item in p.get('items', []):
            print(f"  - Extracted: {item.get('extracted_product')} | Candidates: {len(item.get('candidates', []))}")
            print(f"    Reason: {item.get('reason')}")

if __name__ == '__main__':
    main()
