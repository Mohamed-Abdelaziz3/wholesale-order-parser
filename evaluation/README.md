# Synthetic Blind Evaluation Suite Documentation

Rigorous, leakage-resistant synthetic blind evaluation harness for `wholesale-order-parser` testing product-matching and order-extraction reliability beyond the 50-product demo catalog.

## Architecture & Anti-Leakage Controls

The evaluation suite is divided into three completely independent stages enforced via command-line phase barriers:

1. **Dataset Generation (`evaluation/generate_dataset.py`)**
   - Generates a 400-product synthetic catalog across 14 categories (`synthetic_catalog_400.csv`).
   - Generates 20 dev orders and 100 blind orders (`blind_orders.jsonl`).
   - Generates sealed ground truth (`evaluation/sealed/blind_ground_truth.jsonl`).
   - Applies strict dataset quality audit verifying product uniqueness, category coverage, ≥35% close variants, and ≥60% independent vocabulary non-leakage.
   - Fixed random seed: `42`.

2. **Source & Data Freezing (`evaluation/generate_manifest.json`)**
   - Records SHA256 hashes for all production application source files (`app/*.py`), catalog, blind dataset, unit map, and sealed ground truth in `evaluation/manifest.json`.

3. **Live Prediction Generation (`evaluation/run_predictions.py`)**
   - Receives ONLY synthetic catalog path (`--catalog`), blind orders (`--orders`), output path (`--output`), isolated DB path (`--db`), and run ID (`--run-id`).
   - Explicitly rejects any path pointing to `evaluation/sealed`.
   - Has zero import or file read access to sealed ground truth.
   - Verifies manifest hashes before running.
   - Executes real `GeminiExtractor` and `ProductMatcher` against isolated database (`evaluation/eval_orders.db`).
   - Records prediction file SHA256 in `manifest.json`.

4. **Scoring & Reporting (`evaluation/score_predictions.py`)**
   - Verifies all source, dataset, and prediction hashes before running.
   - Executes deterministic 1-to-1 bipartite line-item alignment.
   - Evaluates 13 success gates and outputs `blind_scores.json`, `failures.csv`, and `BLIND_EVALUATION_REPORT.md`.

---

## Line-Item Alignment Algorithm

To prevent position bias, hallucination distortion, or double-counting, `score_predictions.py` uses a deterministic 1-to-1 optimal bipartite alignment algorithm:

1. **Cost Matrix Construction**:
   Given expected items $E = [e_1, \dots, e_M]$ and predicted items $P = [p_1, \dots, p_N]$, a similarity score $S_{i,j} \in [0.0, 1.0]$ is computed for every pair $(e_i, p_j)$ combining:
   - Product code match / candidate overlap (0.50 weight)
   - Text similarity via RapidFuzz token set ratio (0.20 weight)
   - Exact Decimal quantity match (0.15 weight)
   - Normalized unit match (0.15 weight)
   Cost $C_{i,j} = 1.0 - S_{i,j}$.

2. **Optimal Matching**:
   Solved via SciPy `linear_sum_assignment` (Hungarian algorithm) to find the globally optimal assignment minimizing total dissimilarity.

3. **Unassigned Item Accounting**:
   - Unassigned expected items count as **missing items** (recall penalty).
   - Unassigned predicted items count as **extra items** (precision penalty).

---

## Execution Commands

```bash
# 1. Generate Dataset & Run Audit
.venv\Scripts\python.exe evaluation/generate_dataset.py

# 2. Freeze Manifest Hashes
.venv\Scripts\python.exe evaluation/generate_manifest.py

# 3. Run Predictions on Isolated Database
.venv\Scripts\python.exe evaluation/run_predictions.py \
  --catalog evaluation/data/synthetic_catalog_400.csv \
  --orders evaluation/data/blind_orders.jsonl \
  --output evaluation/results/blind_predictions.jsonl \
  --db evaluation/eval_orders.db \
  --run-id run_blind_eval_v1

# 4. Score Predictions & Generate Report
.venv\Scripts\python.exe evaluation/score_predictions.py
```
