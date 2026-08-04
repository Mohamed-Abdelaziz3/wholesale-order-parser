#!/usr/bin/env python3
"""
Live Gemini API Integration Test — 20 NEW messages.

This script:
1. Sends 20 brand-new Egyptian dialect order messages to the real Gemini API
2. Runs the full pipeline: Extraction → Normalization → Matching
3. Compares results against pre-defined expected outputs
4. Records: input, raw output, expected, actual, timing, errors
5. Calculates: product accuracy, quantity accuracy, unit accuracy, silent errors
6. Generates a detailed JSON results file (NO API key saved)
"""

import json
import os
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

from app.catalog import load_catalog
from app.extractor import GeminiExtractor
from app.matcher import ProductMatcher

# ============================================================
# Unit normalization for comparison (accept equivalent forms)
# ============================================================
UNIT_GROUPS = {
    'كرتونة': 'كرتونة', 'كرتون': 'كرتونة', 'كراتين': 'كرتونة',
    'كرتونه': 'كرتونة', 'كرتونتين': 'كرتونة',
    'رول': 'رول', 'لفة': 'رول', 'لفه': 'رول', 'لف': 'رول', 'لفف': 'رول',
    'باكو': 'باكو', 'باكيت': 'باكو', 'علبة': 'باكو', 'علبه': 'باكو',
    'باكت': 'باكو', 'باكه': 'باكو', 'عبوة': 'باكو', 'عبوه': 'باكو',
    'قطعة': 'قطعة', 'حتة': 'قطعة', 'حته': 'قطعة', 'حبة': 'قطعة',
    'حبه': 'قطعة', 'قطعه': 'قطعة', 'واحدة': 'قطعة', 'واحده': 'قطعة',
    'جركن': 'جركن', 'جالون': 'جركن', 'جراكن': 'جركن', 'جركنه': 'جركن',
    'كيس': 'كيس', 'اكياس': 'كيس', 'شنطة': 'كيس', 'شنطه': 'كيس',
    'دستة': 'دستة', 'دسته': 'دستة',
}


def norm_unit(u: str) -> str:
    return UNIT_GROUPS.get(u.strip(), u.strip())


# ============================================================
# 20 NEW test messages — none reused from previous tests
# ============================================================
TEST_CASES = [
    {
        "id": 1,
        "message": "عايز 4 صابون سايل كبير و3 صابون سايل صغير",
        "expected": [
            {"product_id": "CL001", "quantity": 4, "unit": "قطعة"},
            {"product_id": "CL002", "quantity": 3, "unit": "قطعة"},
        ],
    },
    {
        "id": 2,
        "message": "ابعتلي 6 فلاش ليمون",
        "expected": [
            {"product_id": "CL010", "quantity": 6, "unit": "قطعة"},
        ],
    },
    {
        "id": 3,
        "message": "محتاج كرتونتين اكياس سودا 40 في 50",
        "expected": [
            {"product_id": "PK003", "quantity": 2, "unit": "كرتونة"},
        ],
    },
    {
        "id": 4,
        "message": "عايز تمن باكو اكواب فوم كبيره",
        "expected": [
            {"product_id": "PK017", "quantity": 8, "unit": "باكو"},
        ],
    },
    {
        "id": 5,
        "message": "خمستاشر حتة سلوتيب عريض بني",
        "expected": [
            {"product_id": "PK007", "quantity": 15, "unit": "قطعة"},
        ],
    },
    {
        "id": 6,
        "message": "ابعتلي 3 جالون كلور مركز و5 بريل صغير",
        "expected": [
            {"product_id": "CL004", "quantity": 3, "unit": "جركن"},
            {"product_id": "CL012", "quantity": 5, "unit": "قطعة"},
        ],
    },
    {
        "id": 7,
        "message": "عايز رول ورق كرافت واحد وباكو اكياس بسوسته",
        "expected": [
            {"product_id": "PK015", "quantity": 1, "unit": "رول"},
            {"product_id": "PK011", "quantity": 1, "unit": "باكو"},
        ],
    },
    {
        "id": 8,
        "message": "محتاج عشر باكو صحون فوم كبيره",
        "expected": [
            {"product_id": "PK018", "quantity": 10, "unit": "باكو"},
        ],
    },
    {
        "id": 9,
        "message": "ابعتلي اتناشر سلك ستيل",
        "expected": [
            {"product_id": "CL014", "quantity": 12, "unit": "قطعة"},
        ],
    },
    {
        "id": 10,
        "message": "عايز 7 رول فوط مطبخ ورقيه",
        "expected": [
            {"product_id": "CL018", "quantity": 7, "unit": "رول"},
        ],
    },
    {
        "id": 11,
        "message": "محتاج باكو جوانتي وسط واتنين بخاخ حشرات",
        "expected": [
            {"product_id": "CL025", "quantity": 1, "unit": "باكو"},
            {"product_id": "CL024", "quantity": 2, "unit": "قطعة"},
        ],
    },
    {
        "id": 12,
        "message": "عايز 3 معطر بخاخ و2 ريفيل معطر اوتوماتيك",
        "expected": [
            {"product_id": "CL022", "quantity": 3, "unit": "قطعة"},
            {"product_id": "CL023", "quantity": 2, "unit": "قطعة"},
        ],
    },
    {
        "id": 13,
        "message": "ابعتلي 5 لفة استرتش صغير 30 سنتي",
        "expected": [
            {"product_id": "PK005", "quantity": 5, "unit": "رول"},
        ],
    },
    {
        "id": 14,
        "message": "محتاج 20 اسفنجه مواعين",
        "expected": [
            {"product_id": "CL016", "quantity": 20, "unit": "قطعة"},
        ],
    },
    {
        "id": 15,
        "message": "عايز باكو شوك بلاستيك وباكو معالق",
        "expected": [
            {"product_id": "PK021", "quantity": 1, "unit": "باكو"},
            {"product_id": "PK020", "quantity": 1, "unit": "باكو"},
        ],
    },
    {
        "id": 16,
        "message": "ابعتلي كرتونه سودا 70 في 100 لا خليها اتنين",
        "expected": [
            {"product_id": "PK002", "quantity": 2, "unit": "كرتونة"},
        ],
    },
    {
        "id": 17,
        "message": "محتاج ست باكو مناديل مبلله وتلات رول اكياس زباله وسط",
        "expected": [
            {"product_id": "CL017", "quantity": 6, "unit": "باكو"},
            {"product_id": "CL021", "quantity": 3, "unit": "رول"},
        ],
    },
    {
        "id": 18,
        "message": "عايز فويل المنيوم الكبير 4 رول وفويل صغير رولين",
        "expected": [
            {"product_id": "PK009", "quantity": 4, "unit": "رول"},
            {"product_id": "PK010", "quantity": 2, "unit": "رول"},
        ],
    },
    {
        "id": 19,
        "message": "ابعتلي صابون بودره غسيل عدد 3 اكياس",
        "expected": [
            {"product_id": "CL003", "quantity": 3, "unit": "كيس"},
        ],
    },
    {
        "id": 20,
        "message": "محتاج كرتون تغليف كبير 5 حبات وكرتون صغير 10",
        "expected": [
            {"product_id": "PK013", "quantity": 5, "unit": "قطعة"},
            {"product_id": "PK014", "quantity": 10, "unit": "قطعة"},
        ],
    },
]


def match_expected_to_actual(expected_items, actual_results):
    """Match expected items to actual results. Returns list of comparison dicts."""
    comparisons = []
    used_indices = set()

    for exp in expected_items:
        best_match = None
        best_idx = -1

        # Pass 1: find by exact product_id
        for i, res in enumerate(actual_results):
            if i in used_indices:
                continue
            if res.matched_product and res.matched_product.product_id == exp["product_id"]:
                best_match = res
                best_idx = i
                break

        # Pass 2: if not found by product_id, take first unused
        if best_match is None:
            for i, res in enumerate(actual_results):
                if i not in used_indices:
                    best_match = res
                    best_idx = i
                    break

        if best_match is not None and best_idx >= 0:
            used_indices.add(best_idx)

        comp = {
            "expected_product_id": exp["product_id"],
            "expected_quantity": exp["quantity"],
            "expected_unit": exp["unit"],
        }

        if best_match and best_match.matched_product:
            actual_pid = best_match.matched_product.product_id
            actual_qty = best_match.extracted_quantity
            actual_unit = best_match.extracted_unit

            comp["actual_product_id"] = actual_pid
            comp["actual_product_name"] = best_match.matched_product.product_name
            comp["actual_quantity"] = actual_qty
            comp["actual_unit"] = actual_unit
            comp["actual_status"] = best_match.status
            comp["actual_confidence"] = round(best_match.confidence, 4)
            comp["actual_raw_text"] = best_match.raw_text
            comp["actual_extracted_product"] = best_match.extracted_product

            comp["product_correct"] = actual_pid == exp["product_id"]
            comp["quantity_correct"] = abs(actual_qty - exp["quantity"]) < 0.01
            comp["unit_correct"] = norm_unit(actual_unit) == norm_unit(exp["unit"])
            comp["is_silent_error"] = (
                best_match.status == 'confirmed'
                and not comp["product_correct"]
            )
        elif best_match:
            # Matched but no catalog product found
            comp["actual_product_id"] = None
            comp["actual_quantity"] = best_match.extracted_quantity
            comp["actual_unit"] = best_match.extracted_unit
            comp["actual_status"] = best_match.status
            comp["actual_confidence"] = round(best_match.confidence, 4)
            comp["actual_raw_text"] = best_match.raw_text
            comp["actual_extracted_product"] = best_match.extracted_product
            comp["product_correct"] = False
            comp["quantity_correct"] = abs(best_match.extracted_quantity - exp["quantity"]) < 0.01
            comp["unit_correct"] = norm_unit(best_match.extracted_unit) == norm_unit(exp["unit"])
            comp["is_silent_error"] = False
        else:
            comp["actual_product_id"] = None
            comp["actual_quantity"] = None
            comp["actual_unit"] = None
            comp["actual_status"] = "MISSING"
            comp["product_correct"] = False
            comp["quantity_correct"] = False
            comp["unit_correct"] = False
            comp["is_silent_error"] = False

        comparisons.append(comp)

    return comparisons


def run_all_tests():
    """Run all 20 test cases against real Gemini API."""

    # Load catalog and matcher
    catalog = load_catalog()
    matcher = ProductMatcher(catalog)

    # Initialize REAL Gemini extractor (no mocks)
    try:
        extractor = GeminiExtractor()
    except ValueError as e:
        print(f"FATAL: Cannot initialize Gemini extractor: {e}")
        sys.exit(1)

    print("=" * 90)
    print("   LIVE GEMINI API INTEGRATION TEST - 20 NEW MESSAGES (NO MOCKS)")
    print(f"   Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Catalog: {len(catalog)} products loaded")
    print("=" * 90)

    all_results = []
    total_expected_items = 0
    total_product_correct = 0
    total_quantity_correct = 0
    total_unit_correct = 0
    total_silent_errors = 0
    total_api_errors = 0
    total_elapsed = 0.0

    for tc in TEST_CASES:
        tc_id = tc["id"]
        message = tc["message"]
        expected = tc["expected"]
        num_expected = len(expected)

        print(f"\n{'-' * 90}")
        print(f"  TEST {tc_id:02d}/20")
        print(f"  INPUT: {message}")
        print(f"  EXPECTED: {num_expected} item(s)")
        for ex in expected:
            print(f"    -> {ex['product_id']} x {ex['quantity']} {ex['unit']}")

        tc_record = {
            "id": tc_id,
            "message": message,
            "expected": expected,
            "expected_count": num_expected,
        }

        start_time = time.time()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # REAL Gemini extraction
                extraction = extractor.extract(message)
                elapsed = round(time.time() - start_time, 3)

                # Raw extraction JSON
                raw_items = [item.model_dump() for item in extraction.items]
                tc_record["elapsed_seconds"] = elapsed
                tc_record["raw_extraction"] = raw_items
                tc_record["extracted_count"] = len(extraction.items)
                tc_record["unresolved"] = extraction.unresolved_text
                tc_record["error"] = None

                print(f"\n  RAW EXTRACTION ({elapsed:.2f}s) - {len(extraction.items)} item(s):")
                for ri in raw_items:
                    print(f"    * \"{ri['product_description']}\" x {ri['quantity']} {ri['unit']}")
                    print(f"      raw: \"{ri['raw_text']}\"")

                # Match against catalog
                matched_results = matcher.match_items(extraction.items)

                # Compare with expected
                comparisons = match_expected_to_actual(expected, matched_results)
                tc_record["comparisons"] = comparisons
                break  # Success, exit retry loop
            except Exception as e:
                error_msg = str(e)
                if '429' in error_msg and attempt < max_retries - 1:
                    print("\n  WARNING: RATE LIMIT HIT. Sleeping for 45 seconds before retry...")
                    time.sleep(45.0)
                    continue
                else:
                    elapsed = round(time.time() - start_time, 3)
                    tc_record["elapsed_seconds"] = elapsed
                    tc_record["error"] = error_msg
                    tc_record["raw_extraction"] = None
                    tc_record["comparisons"] = []
                    total_api_errors += 1

                    print(f"\n  API ERROR ({elapsed:.2f}s): {error_msg}")

                    # Count all expected items as failures
                    total_expected_items += num_expected
                    comparisons = []
                    break

        print("\n  MATCHING RESULTS:")
        for c in comparisons:
            p_ok = "OK" if c["product_correct"] else "FAIL"
            q_ok = "OK" if c["quantity_correct"] else "FAIL"
            u_ok = "OK" if c["unit_correct"] else "FAIL"
            silent = " SILENT ERROR" if c.get("is_silent_error") else ""

            if c["actual_product_id"]:
                print(
                    f"    [{p_ok}] Product: expected={c['expected_product_id']} "
                    f"actual={c['actual_product_id']} "
                    f"(conf={c['actual_confidence']:.0%} {c['actual_status']}){silent}"
                )
                print(
                    f"    [{q_ok}] Qty: expected={c['expected_quantity']} "
                    f"actual={c['actual_quantity']}"
                )
                print(
                    f"    [{u_ok}] Unit: expected={c['expected_unit']} "
                    f"actual={c['actual_unit']}"
                )
            else:
                print(
                    f"    [FAIL] Product: expected={c['expected_product_id']} "
                    f"actual=NOT MATCHED (status={c.get('actual_status', 'N/A')})"
                )

            total_expected_items += 1
            if c["product_correct"]:
                total_product_correct += 1
            if c["quantity_correct"]:
                total_quantity_correct += 1
            if c["unit_correct"]:
                total_unit_correct += 1
            if c.get("is_silent_error"):
                total_silent_errors += 1

        total_elapsed += elapsed
        all_results.append(tc_record)

        # Rate limit pause between requests
        time.sleep(15.0)

    # ============================================================
    # SUMMARY
    # ============================================================
    print(f"\n{'=' * 90}")
    print("   SUMMARY")
    print(f"{'=' * 90}")
    print("  Total test messages:       20")
    print(f"  Total expected items:      {total_expected_items}")
    print(f"  API errors:                {total_api_errors}")
    print(f"  Silent errors (confirmed   {total_silent_errors}")
    print("   but wrong product):")
    print()
    print(f"  Product accuracy:          {total_product_correct}/{total_expected_items} "
          f"({total_product_correct/max(total_expected_items,1)*100:.1f}%)")
    print(f"  Quantity accuracy:         {total_quantity_correct}/{total_expected_items} "
          f"({total_quantity_correct/max(total_expected_items,1)*100:.1f}%)")
    print(f"  Unit accuracy:             {total_unit_correct}/{total_expected_items} "
          f"({total_unit_correct/max(total_expected_items,1)*100:.1f}%)")
    print()
    print(f"  Average response time:     {total_elapsed/max(len(all_results),1):.2f}s")
    print(f"  Total elapsed time:        {total_elapsed:.2f}s")
    print(f"{'=' * 90}")

    # Save results JSON (NO API KEY)
    output_path = os.path.join(PROJECT_ROOT, 'live_test_results.json')
    save_data = {
        "timestamp": datetime.now().isoformat(),
        "total_messages": 20,
        "total_expected_items": total_expected_items,
        "product_accuracy": round(total_product_correct / max(total_expected_items, 1), 4),
        "quantity_accuracy": round(total_quantity_correct / max(total_expected_items, 1), 4),
        "unit_accuracy": round(total_unit_correct / max(total_expected_items, 1), 4),
        "silent_errors": total_silent_errors,
        "api_errors": total_api_errors,
        "avg_response_time": round(total_elapsed / max(len(all_results), 1), 3),
        "total_elapsed": round(total_elapsed, 3),
        "results": all_results,
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"\n  Results saved to: {output_path}")
    print("  (API key NOT included in output)")

    return save_data


if __name__ == "__main__":
    run_all_tests()
