# Offline + Bounded-API Reranker Benchmark Suite

This directory contains the rigorous OFFLINE + bounded-API reranker evaluation benchmark for `wholesale-order-parser`.

## Architecture & Methodology
The benchmark operates over frozen Top-5 candidate lists produced by Method J (BM25 + Character TF-IDF + Multilingual E5-small). It is designed exclusively for second-stage reranker selection and architecture evaluation.

### Mandatory Rules & Guarantees
1. **Zero Retrieval Re-Execution**: Candidate lists are extracted strictly from existing retrieval benchmark artifacts. If frozen Top-5 records are missing, execution fails immediately without recomputing embeddings or scores.
2. **Anti-Leakage Barrier**: Prediction generation scripts enforce strict filesystem isolation, explicitly rejecting any file path or read operation referencing `sealed/`.
3. **Dev-First Execution Order**: All calibration, model selection, query representation testing, and threshold setting occur strictly on the DEV split (`data/dev_cases.jsonl` and `sealed/dev_labels.jsonl`) and are frozen into `frozen_config.json` before any held-out EVAL execution.
4. **Bounded Choice**: Gemini (RR-G) operates as a bounded-choice selector over the 5 provided candidates using a strict JSON schema and temperature `0.0`.

## Unambiguous Method Identifiers
- **RR-A**: Frozen retrieval order
- **RR-B**: RapidFuzz diagnostic
- **RR-C**: mMARCO (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`)
- **RR-D**: BGE (`BAAI/bge-reranker-v2-m3`)
- **RR-E**: Jina (`jinaai/jina-reranker-v2-base-multilingual`)
- **RR-F**: Local + retrieval fusion
- **RR-G**: Gemini bounded-choice reranker
- **RR-H**: Local + contradiction penalties
- **RR-I**: Cost-aware cascade
- **RR-J**: Local-only safe cascade

## Execution Instructions
Execute the workflow in strict sequential order from the repository root using `.venv`:
```powershell
# Step 1: Verify frozen evidence
.venv\Scripts\python evaluation\reranker_benchmark\verify_env.py

# Step 2: Build and freeze case snapshot
.venv\Scripts\python evaluation\reranker_benchmark\build_cases.py

# Step 3: Run local rerankers on DEV only
.venv\Scripts\python evaluation\reranker_benchmark\run_local_rerankers.py --split dev

# Step 4: Run Gemini reranker on DEV only
.venv\Scripts\python evaluation\reranker_benchmark\run_gemini_reranker.py --split dev

# Step 5 & 6: Calibrate on DEV and freeze configuration
.venv\Scripts\python evaluation\reranker_benchmark\calibrate.py

# Step 7: Run automated test suite
.venv\Scripts\python -m pytest tests\test_reranker_benchmark.py -v

# Step 8 & 9: Run held-out EVAL predictions exactly once and freeze hashes
.venv\Scripts\python evaluation\reranker_benchmark\run_local_rerankers.py --split eval
.venv\Scripts\python evaluation\reranker_benchmark\run_gemini_reranker.py --split eval

# Step 10 & 11: Score held-out predictions and generate final report
.venv\Scripts\python evaluation\reranker_benchmark\score_results.py
```
