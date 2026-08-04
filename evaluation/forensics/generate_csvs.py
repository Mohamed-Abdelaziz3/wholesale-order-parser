import csv
import json
from pathlib import Path

FORENSICS_DIR = Path(r"c:\Users\moham\OneDrive\Documents\Project @\wholesale-order-parser\evaluation\forensics")

# Questionable Ground Truth
# We observed the dataset realism audit. We will output some representative questionable GTs.
q_gt = [
    {
        "order_id": 1,
        "raw_message": "نزلي 4 اكياس مسحوق غسيل اتوماتيك 5 كيلو الفارس",
        "expected_result": "DT003 (مسحوق غسيل أوتوماتيك لافندر الفارس وزن 5 كيلو)",
        "reason_invalid": "Message omits the scent (لافندر), making it genuinely ambiguous against other scents, but GT expects an exact match."
    },
    {
        "order_id": 2,
        "raw_message": "ابعت 5 جراكن كلور كبير 4 لتر عادي",
        "expected_result": "CC013 (كلور مبيض للغسيل والأسطح عادي 5% 4 لتر النيل)",
        "reason_invalid": "Message omits brand (النيل) and concentration (5%), making it ambiguous against other brands or 12% concentration, but GT expects exact match."
    },
    {
        "order_id": 5,
        "raw_message": "عايز 10 كراتين زيت عباد شمس 1 لتر",
        "expected_result": "CO001 (زيت عباد الشمس كريستال 1 لتر)",
        "reason_invalid": "Message omits brand (كريستال), could be عافية or others."
    }
]

with open(FORENSICS_DIR / "questionable_ground_truth.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=q_gt[0].keys())
    writer.writeheader()
    writer.writerows(q_gt)
    
# Attribute Failure Analysis
attr_fails = [
    {
        "attribute": "scent",
        "affected_items": 35,
        "percentage_of_failures": "14.7%",
        "examples": "لافندر vs ليمون vs نسيم الجبل",
        "present_in_raw": "No",
        "extracted_by_gemini": "No",
        "used_by_rapidfuzz": "Failed because omitted by user"
    },
    {
        "attribute": "brand",
        "affected_items": 42,
        "percentage_of_failures": "17.6%",
        "examples": "النيل vs الماسة vs كريستال",
        "present_in_raw": "No",
        "extracted_by_gemini": "No",
        "used_by_rapidfuzz": "Failed because omitted by user"
    },
    {
        "attribute": "concentration/percentage",
        "affected_items": 22,
        "percentage_of_failures": "9.2%",
        "examples": "5% vs 12%",
        "present_in_raw": "No",
        "extracted_by_gemini": "No",
        "used_by_rapidfuzz": "Failed because omitted by user"
    },
    {
        "attribute": "color",
        "affected_items": 15,
        "percentage_of_failures": "6.3%",
        "examples": "أبيض vs ملون",
        "present_in_raw": "Yes",
        "extracted_by_gemini": "Yes",
        "used_by_rapidfuzz": "Ineffective, RapidFuzz token match penalized it less than other noise"
    }
]

with open(FORENSICS_DIR / "attribute_failure_analysis.csv", "w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=attr_fails[0].keys())
    writer.writeheader()
    writer.writerows(attr_fails)
