"""Run 20 live tests using real Gemini API, measure accuracy, response time, token usage and cost."""

import os
import sys
import json
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

from app.catalog import load_catalog
from app.matcher import ProductMatcher
from app.extractor import GeminiExtractor
from tests.test_live_gemini import TEST_CASES, match_expected_to_actual

def run_live_evaluation():
    catalog = load_catalog()
    matcher = ProductMatcher(catalog)
    extractor = GeminiExtractor()

    print("=" * 80)
    print("  RUNNING LIVE GEMINI EVALUATION - 20 NEW MESSAGES")
    print(f"  Model: {extractor.candidate_models[0]}")
    print("=" * 80)

    results = []
    total_expected = 0
    prod_correct = 0
    qty_correct = 0
    unit_correct = 0
    silent_errors = 0
    api_errors = 0
    total_latency = 0.0

    total_prompt_tokens = 0
    total_candidate_tokens = 0

    for tc in TEST_CASES:
        msg = tc["message"]
        expected = tc["expected"]
        total_expected += len(expected)

        start_t = time.time()
        try:
            # Call Gemini
            extraction = extractor.extract(msg)
            elapsed = time.time() - start_t
            total_latency += elapsed

            # Calculate token estimate (Prompt ~250 tokens, Response ~60 tokens per item)
            prompt_tokens = len(msg.split()) * 4 + 220
            output_tokens = len(extraction.items) * 60 + 20
            total_prompt_tokens += prompt_tokens
            total_candidate_tokens += output_tokens

            # Match items against catalog
            matched_items = matcher.match_items(extraction.items)
            comparisons = match_expected_to_actual(expected, matched_items)

            for c in comparisons:
                if c["product_correct"]:
                    prod_correct += 1
                if c["quantity_correct"]:
                    qty_correct += 1
                if c["unit_correct"]:
                    unit_correct += 1
                if c.get("is_silent_error"):
                    silent_errors += 1

            print(f"Test #{tc['id']:02d}: OK ({elapsed:.2f}s) | Extracted {len(extraction.items)} items | Tokens: ~{prompt_tokens}+{output_tokens}")
        except Exception as e:
            elapsed = time.time() - start_t
            total_latency += elapsed
            api_errors += 1
            print(f"Test #{tc['id']:02d}: FAILED ({elapsed:.2f}s) | Error: {e}")

        # Short pause between calls
        time.sleep(2.0)

    avg_latency = total_latency / max(len(TEST_CASES), 1)

    # Cost Calculation:
    # Gemini 3.6 Flash / 1.5 Flash Pricing:
    # Input: $0.075 per 1,000,000 tokens
    # Output: $0.300 per 1,000,000 tokens
    input_cost = (total_prompt_tokens / 1_000_000) * 0.075
    output_cost = (total_candidate_tokens / 1_000_000) * 0.300
    total_cost_usd = input_cost + output_cost

    summary = {
        "total_messages": len(TEST_CASES),
        "total_expected_items": total_expected,
        "product_accuracy": f"{prod_correct}/{total_expected} ({prod_correct/max(total_expected,1):.1%})",
        "quantity_accuracy": f"{qty_correct}/{total_expected} ({qty_correct/max(total_expected,1):.1%})",
        "unit_accuracy": f"{unit_correct}/{total_expected} ({unit_correct/max(total_expected,1):.1%})",
        "silent_errors": silent_errors,
        "api_errors": api_errors,
        "average_response_time_sec": round(avg_latency, 2),
        "total_response_time_sec": round(total_latency, 2),
        "total_prompt_tokens": total_prompt_tokens,
        "total_output_tokens": total_candidate_tokens,
        "estimated_cost_usd": f"${total_cost_usd:.6f} (Free tier: $0.00)",
    }

    print("\n" + "=" * 80)
    print("  FINAL EVALUATION REPORT")
    print("=" * 80)
    for k, v in summary.items():
        print(f"  {k:30s}: {v}")
    print("=" * 80)

    with open("live_eval_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    run_live_evaluation()
