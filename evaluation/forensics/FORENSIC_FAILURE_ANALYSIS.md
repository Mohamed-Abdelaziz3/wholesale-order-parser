# Forensic Failure Analysis

## Executive Summary
The headline Exact Product Accuracy of 11.36% (or 14.4% excluding API failures) is heavily skewed by a flawed retrieval/ranking engine, severe thresholding artifacts, and excessively adversarial ground truth. The Gemini LLM extraction performed reasonably well, but the downstream pipeline collapsed.

## 1. API Failure Impact
A 20% API error rate destroyed 56 expected line items immediately. The Gemini API timed out, rate-limited, or returned malformed JSON. Without these infrastructure failures, the operational accuracy would improve, but the pipeline would still fail at the matching stage.

## 2. Retrieval Engine Failure (The True Bottleneck)
The RapidFuzz-based matching engine is the primary culprit for the pipeline's failure. 
- **Candidate Recall at Top 5**: 90.38%
- **Ranking Failure**: 78 items had the correct candidate retrieved, but ranked it below an incorrect candidate.
- **Thresholding Failure**: 48 items correctly ranked the target as #1, but the score difference between #1 and #2 was < 0.08, causing the pipeline to falsely flag it as `ambiguous`. RapidFuzz over-compresses scores for similar products, destroying confidence calibration.

## 3. Discrepancy with Evaluator
The original `score_predictions.py` script **failed to log the vast majority of failures**. It only appended to `failures.csv` if an item was a "silent error" (confident but wrong) or a "hallucination". It completely ignored missing items, API failures, unit failures, and incorrect ambiguity flags. This explains why `failures.csv` was empty while the accuracy was 11.36%.

## 4. Counterfactual Oracles
- If API failures are fixed: 11.3% -> 14.4% accuracy
- If the ranking engine could perfectly select from the Top 5 candidates: 14.4% -> 90.38% accuracy.

## 5. Top 5 Most Important Failure Classes
1. **Ranking Failure (78 items)**: RapidFuzz unable to distinguish critical numerical attributes from text noise.
2. **API Failure (56 items)**: Flaky LLM provider connection.
3. **Thresholding Artefact (48 items)**: RapidFuzz score compression triggered false ambiguity.
4. **Extraction Semantic Error (30 items)**: LLM failed to extract the exact wording needed for the fuzzy matcher.
5. **Spurious Items (13 items)**: LLM hallucinated extra items from conversational filler.
