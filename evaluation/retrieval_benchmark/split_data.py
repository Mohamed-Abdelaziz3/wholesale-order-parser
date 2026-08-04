import json
import random
import hashlib
from collections import defaultdict
from pathlib import Path

BENCHMARK_DIR = Path("evaluation/retrieval_benchmark")
GT_PATH = Path("evaluation/sealed/blind_ground_truth.jsonl")

def get_order_strata(gt_path):
    strata = {}
    with open(gt_path, 'r', encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            order_id = row['order_id']
            tags = tuple(sorted(row.get('scenario_tags', [])))
            num_items = len(row.get('expected_items', []))
            
            # Simple stratification key based on tags and size
            if num_items <= 3:
                size_bin = "small"
            elif num_items <= 7:
                size_bin = "medium"
            else:
                size_bin = "large"
                
            strata[order_id] = (tags, size_bin)
    return strata

def main():
    strata = get_order_strata(GT_PATH)
    
    # We want to ensure deterministic splits.
    # We read all valid order IDs from queries.
    order_ids = set()
    queries_path = BENCHMARK_DIR / "benchmark_queries.jsonl"
    with open(queries_path, 'r', encoding='utf-8') as f:
        for line in f:
            q = json.loads(line)
            order_ids.add(q['order_id'])
            
    # Group order IDs by strata
    groups = defaultdict(list)
    for oid in order_ids:
        groups[strata[oid]].append(oid)
        
    random.seed(42)  # Fixed seed
    
    dev_ids = []
    eval_ids = []
    
    for stratum, oids in sorted(groups.items()):
        oids.sort()  # Ensure determinism before shuffle
        random.shuffle(oids)
        n_dev = max(1, int(len(oids) * 0.3)) if len(oids) > 1 else (1 if random.random() < 0.3 else 0)
        
        dev_ids.extend(oids[:n_dev])
        eval_ids.extend(oids[n_dev:])
        
    # Final sort
    dev_ids.sort()
    eval_ids.sort()
    
    split_manifest = {
        "seed": 42,
        "dev_split": dev_ids,
        "eval_split": eval_ids
    }
    
    manifest_path = BENCHMARK_DIR / "split_manifest.json"
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(split_manifest, f, indent=2)
        
    # Also create the safety split manifest? It's the same split since it's by order_id!
    print(f"Split created. Dev orders: {len(dev_ids)}, Eval orders: {len(eval_ids)}")
    
if __name__ == "__main__":
    main()
