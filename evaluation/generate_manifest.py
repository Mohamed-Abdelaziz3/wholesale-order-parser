#!/usr/bin/env python3
"""
Manifest Generator for Synthetic Blind Evaluation.
Hashes all frozen production source files, catalog, blind dataset, unit map, and sealed ground truth.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "evaluation" / "manifest.json"

FILES_TO_HASH = [
    "app/catalog.py",
    "app/database.py",
    "app/extractor.py",
    "app/main.py",
    "app/matcher.py",
    "app/models.py",
    "app/normalizer.py",
    "evaluation/data/synthetic_catalog_400.csv",
    "evaluation/data/blind_orders.jsonl",
    "evaluation/sealed/blind_ground_truth.jsonl",
    "evaluation/unit_normalization_map.json",
]

def hash_file(rel_path: str) -> str:
    full_path = PROJECT_ROOT / rel_path
    if not full_path.exists():
        raise FileNotFoundError(f"File not found for hashing: {full_path}")
    h = hashlib.sha256()
    with open(full_path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_git_info() -> dict:
    info = {"git_available": False, "commit_hash": None, "working_tree_clean": None}
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True)
        if res.returncode == 0:
            info["git_available"] = True
            info["commit_hash"] = res.stdout.strip()
            status_res = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, text=True)
            info["working_tree_clean"] = len(status_res.stdout.strip()) == 0
    except Exception:
        pass
    return info

def generate_manifest() -> dict:
    git_info = get_git_info()
    file_hashes = {}
    for rel in FILES_TO_HASH:
        file_hashes[rel] = hash_file(rel)
        
    manifest = {
        "random_seed": 42,
        "git": git_info,
        "file_hashes": file_hashes,
        "catalog_size": 400,
        "blind_order_count": 100,
        "prediction_file_hash": None,
    }
    
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        
    print(f"Manifest created successfully at {MANIFEST_PATH}")
    return manifest

if __name__ == "__main__":
    generate_manifest()
