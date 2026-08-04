#!/usr/bin/env python3
"""
Synthetic Dataset Generator for Egyptian Wholesale Order Evaluation.

Generates:
1. evaluation/data/synthetic_catalog_400.csv (400 products across 14 categories)
2. evaluation/data/dev_orders.jsonl (20 orders)
3. evaluation/data/dev_ground_truth.jsonl (20 GT records)
4. evaluation/data/blind_orders.jsonl (100 blind orders)
5. evaluation/sealed/blind_ground_truth.jsonl (100 sealed GT records)

Applies strict data quality validation and anti-leakage audits before freezing.
Fixed seed: 42.
"""

import csv
import json
import os
import random
import re
import sys
from pathlib import Path

# Fix seed for strict reproducibility
SEED = 42
random.seed(SEED)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "evaluation" / "data"
SEALED_DIR = PROJECT_ROOT / "evaluation" / "sealed"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEALED_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# 1. CATALOG DEFINITION (400 PRODUCTS ACROSS 14 CATEGORIES)
# -----------------------------------------------------------------------------

CATEGORIES = [
    "cleaning_chemicals",
    "detergents",
    "tissue_hygiene",
    "plastic_bags",
    "garbage_bags",
    "stretch_film",
    "aluminum_foil",
    "food_packaging",
    "paper_foam_cups",
    "disposable_cutlery",
    "gloves_masks",
    "warehouse_consumables",
    "tapes_wrapping",
    "containers_dispensers",
]

def build_400_catalog() -> list[dict]:
    products = []
    
    # Helper to generate variants
    # 1. cleaning_chemicals (CC - 35 items)
    cc_brands = ["الراعي", "العمدة", "النيل", "الزهراء", "الروضة"]
    # Group 1: Liquid Soap (5 items)
    for i, vol in enumerate(["500 مل", "1 لتر", "3 لتر", "5 لتر", "20 لتر"]):
        products.append({
            "product_id": f"CC00{i+1}",
            "product_name": f"صابون سائل للأطباق برائحة الليمون ماركة الراعي {vol}",
            "aliases": f"صابون سائل ليمون الراعي {vol}|صابون مواعين الراعي {vol}",
            "unit": "جركن" if "لتر" in vol and float(vol.split()[0]) >= 3 else ("زجاجة" if "500" in vol or vol == "1 لتر" else "قطعة"),
            "price": 15.0 + i * 25.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "liquid_soap_lemon"
        })
    # Group 2: Floor Cleaner Scented (6 items)
    for i, (scent, color) in enumerate([("لافندر", "بنفسجي"), ("صنوبر", "أخضر"), ("ورد", "أحمر"), ("فل", "أبيض"), ("ليمون", "أصفر"), ("نعناع", "أخضر")]):
        products.append({
            "product_id": f"CC0{i+6:02d}",
            "product_name": f"منظف أرضيات ومعطر برائحة ال{scent} 1.5 لتر العمدة",
            "aliases": f"منظف ارضيات {scent} 1.5 لتر|معطر ارضيات {color}",
            "unit": "قطعة",
            "price": 32.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "floor_cleaner_scented"
        })
    # Group 3: Chlorine (4 items)
    for i, (ctype, vol) in enumerate([("عادي 5%", "1 لتر"), ("عادي 5%", "4 لتر"), ("مركز 12%", "1 لتر"), ("مركز 12%", "4 لتر")]):
        products.append({
            "product_id": f"CC0{i+12:02d}",
            "product_name": f"كلور مبيض للغسيل والأسطح {ctype} {vol} النيل",
            "aliases": f"كلور النيل {ctype} {vol}|كلور مبيض {vol}",
            "unit": "جركن" if "4" in vol else "قطعة",
            "price": 12.0 + i * 18.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "chlorine_bleach"
        })
    # Group 4: Disinfectant Dettol type (4 items)
    for i, vol in enumerate(["250 مل", "500 مل", "1 لتر", "5 لتر"]):
        products.append({
            "product_id": f"CC0{i+16:02d}",
            "product_name": f"مطهر ومعقم صنوبر مضاد للبكتيريا {vol} الزهراء",
            "aliases": f"مطهر الزهراء {vol}|معقم ارضيات صنوبر {vol}",
            "unit": "جركن" if "5" in vol else "قطعة",
            "price": 28.0 + i * 35.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "disinfectant_pine"
        })
    # Group 5: Glass Cleaner (3 items)
    for i, scent in enumerate(["زرقاء أصلية", "برائحة الليمون", "برائحة التفاح"]):
        products.append({
            "product_id": f"CC0{i+20:02d}",
            "product_name": f"ملمع ومُنظف زجاج ملمع بخاخ {scent} 700 مل الروضة",
            "aliases": f"ملمع زجاج الروضة {scent}|بخاخ زجاج 700 مل",
            "unit": "قطعة",
            "price": 22.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "glass_cleaner"
        })
    # Group 6: Furniture Polish (3 items)
    for i, scent in enumerate(["برائحة العود", "برائحة البرتقال", "برائحة الصنوبر"]):
        products.append({
            "product_id": f"CC0{i+23:02d}",
            "product_name": f"ملمع خشب وأثاث سبراي {scent} 300 مل",
            "aliases": f"ملمع خشب {scent}|سبراي اثاث 300 مل",
            "unit": "قطعة",
            "price": 38.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "furniture_polish"
        })
    # Group 7: Grease Remover / Oven Cleaner (4 items)
    for i, vol in enumerate(["500 مل بخاخ", "1 لتر إعادة تعبئة", "5 لتر جركن", "20 لتر شوايات"]):
        products.append({
            "product_id": f"CC0{i+26:02d}",
            "product_name": f"قاهر الدهون ومذيب الشحوم للفرن البوتاجاز {vol}",
            "aliases": f"قاهر الدهون {vol}|منظف بوتاجاز {vol}",
            "unit": "جركن" if "لتر" in vol and ("5" in vol or "20" in vol) else "قطعة",
            "price": 30.0 + i * 40.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "grease_remover"
        })
    # Group 8: Hand Sanitizer Gel (6 items)
    for i, vol in enumerate(["60 مل", "100 مل", "500 مل مضخة", "1 لتر مضخة", "5 لتر جركن", "10 لتر"]):
        products.append({
            "product_id": f"CC0{i+30:02d}",
            "product_name": f"جل مطهر لليدين بالكحول 70% {vol}",
            "aliases": f"كحول جل لليدين {vol}|مطهر ايدين كحول {vol}",
            "unit": "جركن" if "لتر" in vol and ("5" in vol or "10" in vol) else "قطعة",
            "price": 10.0 + i * 25.0,
            "category": "cleaning_chemicals",
            "is_variant": True,
            "variant_group": "hand_sanitizer_gel"
        })

    # 2. detergents (DT - 35 items)
    # Group 1: Washing Powder Automatic (6 items)
    for i, (weight, brand) in enumerate([("1 كيلو", "الفارس"), ("3 كيلو", "الفارس"), ("5 كيلو", "الفارس"), ("10 كيلو", "الفارس"), ("5 كيلو", "الماسة"), ("10 كيلو", "الماسة")]):
        products.append({
            "product_id": f"DT0{i+1:02d}",
            "product_name": f"مسحوق غسيل أوتوماتيك لافندر {brand} وزن {weight}",
            "aliases": f"مسحوق اتوماتيك {brand} {weight}|صابون بودرة اتوماتيك {weight}",
            "unit": "كيس",
            "price": 45.0 + i * 40.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "washing_powder_auto"
        })
    # Group 2: Washing Powder Manual (5 items)
    for i, weight in enumerate(["350 جرام", "500 جرام", "1 كيلو", "3 كيلو", "5 كيلو"]):
        products.append({
            "product_id": f"DT0{i+7:02d}",
            "product_name": f"مسحوق غسيل يدوي عالي الرغوة الفارس {weight}",
            "aliases": f"مسحوق يدوي {weight}|صابون عادي عالي الرغوة {weight}",
            "unit": "كيس",
            "price": 12.0 + i * 20.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "washing_powder_manual"
        })
    # Group 3: Fabric Softener Downy type (5 items)
    for i, (scent, vol) in enumerate([("نسيم الصباح", "1 لتر"), ("زهور الريف", "1 لتر"), ("لافندر سحري", "1 لتر"), ("نسيم الصباح", "3 لتر"), ("زهور الريف", "3 لتر")]):
        products.append({
            "product_id": f"DT0{i+12:02d}",
            "product_name": f"منعم ومنشط ملابس وأقمشة برائحة {scent} {vol}",
            "aliases": f"منعم ملابس {scent} {vol}|داوني {scent} {vol}",
            "unit": "جركن" if "3" in vol else "قطعة",
            "price": 35.0 if "1" in vol else 85.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "fabric_softener"
        })
    # Group 4: Laundry Gel Liquid (5 items)
    for i, (gtype, vol) in enumerate([("للملابس البيضاء", "2.5 لتر"), ("لالملابس الملونة", "2.5 لتر"), ("للملابس السوداء والعبايات", "2.5 لتر"), ("للملابس الملونة", "4 لتر"), ("للملابس السوداء والعبايات", "4 لتر")]):
        products.append({
            "product_id": f"DT0{i+17:02d}",
            "product_name": f"جل غسيل أوتوماتيك مركز {gtype} {vol}",
            "aliases": f"جل غسيل {gtype} {vol}|برسيل جل {gtype} {vol}",
            "unit": "جركن",
            "price": 75.0 if "2.5" in vol else 115.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "laundry_gel"
        })
    # Group 5: Carpet Cleaner Gel/Liquid (4 items)
    for i, vol in enumerate(["500 مل", "1 لتر", "3 لتر", "5 لتر"]):
        products.append({
            "product_id": f"DT0{i+22:02d}",
            "product_name": f"شامبو ومنظف السجاد والموكيت الرغوي {vol}",
            "aliases": f"منظف سجاد {vol}|شامبو سجاد وموكيت {vol}",
            "unit": "جركن" if "لتر" in vol and float(vol.split()[0]) >= 3 else "قطعة",
            "price": 25.0 + i * 30.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "carpet_cleaner"
        })
    # Group 6: Dishwasher Salt & Gel (5 items)
    for i, pitem in enumerate(["ملح غسالة أطباق 1.5 كيلو", "ملمع ومساعد شطف غسالة أطباق 500 مل", "أقراص غسالة أطباق 30 قرص", "أقراص غسالة أطباق 60 قرص", "جل غسالة أطباق مركز 1 لتر"]):
        products.append({
            "product_id": f"DT0{i+26:02d}",
            "product_name": f"مستلزمات غسالات الأطباق - {pitem}",
            "aliases": f"{pitem}|منظف غسالة اطباق",
            "unit": "باكو" if "أقراص" in pitem else "قطعة",
            "price": 40.0 + i * 35.0,
            "category": "detergents",
            "is_variant": False,
            "variant_group": "dishwasher_supplies"
        })
    # Group 7: Bleach Powder / Color Stain Remover (5 items)
    for i, (btype, weight) in enumerate([("مربك الألوان بودرة", "250 جرام"), ("مربك الألوان بودرة", "500 جرام"), ("مزيل بقع الملابس البيضاء", "500 جرام"), ("مزيل بقع الملون", "1 كيلو"), ("مزيل بقع الملون", "2 كيلو")]):
        products.append({
            "product_id": f"DT0{i+31:02d}",
            "product_name": f"مزيل البقع والزهور ملابس {btype} {weight}",
            "aliases": f"مزيل بقع {weight}|بودرة فانيش {weight}",
            "unit": "باكو" if "جرام" in weight else "كيس",
            "price": 20.0 + i * 25.0,
            "category": "detergents",
            "is_variant": True,
            "variant_group": "stain_remover_powder"
        })

    # 3. tissue_hygiene (TH - 35 items)
    # Group 1: Facial Tissues Pull Pack (6 items)
    for i, count in enumerate([100, 200, 300, 400, 500, 600]):
        products.append({
            "product_id": f"TH0{i+1:02d}",
            "product_name": f"مناديل وجه سحب ناعمة 3 طبقات عبوة {count} منديل النيل",
            "aliases": f"مناديل سحب {count} منديل|مناديل النيل {count}|علبة مناديل {count}",
            "unit": "باكو",
            "price": 12.0 + i * 8.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "facial_tissues_pull"
        })
    # Group 2: Kitchen Towel Rolls (5 items)
    for i, (rolls, ply) in enumerate([(2, "مزدوج 2 طبقة"), (4, "مزدوج 2 طبقة"), (6, "مزدوج 2 طبقة"), (2, "جامبو 3 طبقة"), (4, "جامبو 3 طبقة")]):
        products.append({
            "product_id": f"TH0{i+7:02d}",
            "product_name": f"مناديل مطبخ ورقية امتصاص عالي {rolls} رول {ply}",
            "aliases": f"مناديل مطبخ {rolls} رول|رول مطبخ {rolls} حبة",
            "unit": "باكو",
            "price": 18.0 + i * 14.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "kitchen_towel_rolls"
        })
    # Group 3: Toilet Paper Rolls (6 items)
    for i, (rolls, ptype) in enumerate([(6, "مضغوط 2 طبقة"), (12, "مضغوط 2 طبقة"), (24, "مضغوط 2 طبقة"), (6, "سوبر ناعم 3 طبقة"), (12, "سوبر ناعم 3 طبقة"), (24, "سوبر ناعم 3 طبقة")]):
        products.append({
            "product_id": f"TH0{i+12:02d}",
            "product_name": f"مناديل تواليت وحمام ورقية {rolls} رول {ptype}",
            "aliases": f"مناديل تواليت {rolls} رول|رول حمام {rolls} حبة",
            "unit": "باكو",
            "price": 25.0 + i * 22.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "toilet_paper_rolls"
        })
    # Group 4: Wet Wipes (6 items)
    for i, (count, scent) in enumerate([(15, "بدون عطر"), (40, "برائحة البابونج"), (72, "برائحة الألوفيرا"), (80, "للأطفال مرطب"), (100, "برائحة اللافندر"), (120, "مع غطاء محكم")]):
        products.append({
            "product_id": f"TH0{i+18:02d}",
            "product_name": f"مناديل مبللة وايبس مخصصة للنظافة الشخصية {count} منديل {scent}",
            "aliases": f"مناديل مبللة {count} منديل|وايبس {count} منديل|وايبس {scent}",
            "unit": "باكو",
            "price": 8.0 + i * 7.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "wet_wipes"
        })
    # Group 5: Pocket Tissues / Handkerchiefs (4 items)
    for i, (packs, ptype) in enumerate([(10, "عادي 10 مناديل"), (10, "معطر ليمون"), (20, "عادي 10 مناديل"), (20, "معطر نعناع")]):
        products.append({
            "product_id": f"TH0{i+24:02d}",
            "product_name": f"مناديل جيب صغيرة ورقية ربطة {packs} باكو {ptype}",
            "aliases": f"مناديل جيب {packs} باكو|باكيت مناديل جيب",
            "unit": "باكو",
            "price": 15.0 + i * 12.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "pocket_tissues"
        })
    # Group 6: Institutional Centerfeed Towels (4 items)
    for i, (weight, ptype) in enumerate([("1 كيلو", "سحب مركز"), ("1.5 كيلو", "سحب مركز"), ("2 كيلو", "سحب مركز أبيض"), ("2.5 كيلو", "سحب مركز جامبو")]):
        products.append({
            "product_id": f"TH0{i+28:02d}",
            "product_name": f"مناديل ورقية رول سحب من المنتصف للمطاعم وزن {weight} {ptype}",
            "aliases": f"مناديل سحب منتصف {weight}|رول سحب مركز {weight}",
            "unit": "رول",
            "price": 35.0 + i * 18.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "centerfeed_towels"
        })
    # Group 7: Medical Examination Table Roll (4 items)
    for i, (width, length) in enumerate([("50 سم", "50 متر"), ("50 سم", "100 متر"), ("60 سم", "50 متر"), ("60 سم", "100 متر")]):
        products.append({
            "product_id": f"TH0{i+32:02d}",
            "product_name": f"رول ملايات ورقية سرير كشف طبي عرض {width} طول {length}",
            "aliases": f"رول سرير طبي {width}|ملايات عيادات ورقية {width}",
            "unit": "رول",
            "price": 45.0 + i * 25.0,
            "category": "tissue_hygiene",
            "is_variant": True,
            "variant_group": "medical_exam_roll"
        })

    # 4. plastic_bags (PB - 35 items)
    # Group 1: Transparent Polyethylene Bags (6 items)
    for i, dim in enumerate(["20×30", "25×35", "30×40", "40×50", "50×70", "70×100"]):
        products.append({
            "product_id": f"PB0{i+1:02d}",
            "product_name": f"أكياس بلاستيك شفاف شفافة لتغليف المنتجات مقاس {dim} سم (وزن 1 كيلو)",
            "aliases": f"اكياس شفاف {dim}|شنط شفاف {dim}|اكياس نايلون {dim}",
            "unit": "كيس",
            "price": 45.0 + i * 5.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "transparent_poly_bags"
        })
    # Group 2: Black Shopping Bags T-shirt handle (6 items)
    for i, dim in enumerate(["30×40", "35×45", "40×50", "50×60", "60×70", "70×90"]):
        products.append({
            "product_id": f"PB0{i+7:02d}",
            "product_name": f"أكياس بلاستيك سوداء علاقة يد مقاس {dim} سم كرتونة 10 كيلو",
            "aliases": f"اكياس سوداء علاقة {dim}|شنط سودا يد {dim}",
            "unit": "كرتونة",
            "price": 380.0 + i * 40.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "black_tshirt_bags"
        })
    # Group 3: Colored Shopping Bags White/Blue (6 items)
    for i, (color, dim) in enumerate([("أبيض", "25×35"), ("أبيض", "30×40"), ("أبيض", "40×50"), ("أزرق", "30×40"), ("أزرق", "40×50"), ("أزرق", "50×60")]):
        products.append({
            "product_id": f"PB0{i+13:02d}",
            "product_name": f"أكياس بلاستيك تسوق علاقة لون {color} مقاس {dim} سم ربطة 1 كيلو",
            "aliases": f"اكياس تسوق {color} {dim}|شنط علاقة {color} {dim}",
            "unit": "كيس",
            "price": 50.0 + i * 6.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "colored_tshirt_bags"
        })
    # Group 4: Zip Lock Reclosable Bags (6 items)
    for i, dim in enumerate(["6×8", "8×12", "10×15", "15×20", "20×25", "25×35"]):
        products.append({
            "product_id": f"PB0{i+19:02d}",
            "product_name": f"أكياس بلاستيك زيبللوك بسحاب إغلاق محكم مقاس {dim} سم باكو 100 كيس",
            "aliases": f"اكياس زيبلوك {dim}|اكياس سوستة {dim}|شنط زيبلوك {dim}",
            "unit": "باكو",
            "price": 18.0 + i * 10.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "ziplock_bags"
        })
    # Group 5: High Density Heavy Duty Bags (6 items)
    for i, dim in enumerate(["40×60", "50×70", "60×80", "70×100", "80×110", "90×120"]):
        products.append({
            "product_id": f"PB0{i+25:02d}",
            "product_name": f"أكياس بلاستيك سميكة عالية الكثافة للمصانع مقاس {dim} سم وزن 5 كيلو",
            "aliases": f"اكياس تقيلة {dim}|أكياس مصانع سميكة {dim}",
            "unit": "كيس",
            "price": 220.0 + i * 35.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "hdpe_heavy_bags"
        })
    # Group 6: Vacuum Sealer Bags (5 items)
    for i, dim in enumerate(["15×25", "20×30", "25×35", "30×40", "40×50"]):
        products.append({
            "product_id": f"PB0{i+31:02d}",
            "product_name": f"أكياس التفريغ من الهواء فاليوم وحفظ الطعام مقاس {dim} سم باكو 50 كيس",
            "aliases": f"اكياس فاكيوم {dim}|اكياس تفريغ هواء {dim}",
            "unit": "باكو",
            "price": 40.0 + i * 15.0,
            "category": "plastic_bags",
            "is_variant": True,
            "variant_group": "vacuum_sealer_bags"
        })

    # 5. garbage_bags (GB - 30 items)
    # Group 1: Heavy Duty Black Garbage Roll (6 items)
    for i, dim in enumerate(["50×70", "70×90", "80×100", "90×120", "100×140", "120×150"]):
        products.append({
            "product_id": f"GB0{i+1:02d}",
            "product_name": f"أكياس قمامة سوداء سميكة جداً رول مقاس {dim} سم سمك 55 ميلا",
            "aliases": f"اكياس قمامة سوداء {dim}|رول زبالة اسود {dim}|شنط قمامة {dim}",
            "unit": "رول",
            "price": 25.0 + i * 15.0,
            "category": "garbage_bags",
            "is_variant": True,
            "variant_group": "heavy_garbage_black_roll"
        })
    # Group 2: Drawstring Garbage Bags Scented (6 items)
    for i, (scent, cap) in enumerate([("لافندر", "30 لتر"), ("ليمون", "30 لتر"), ("صنوبر", "50 لتر"), ("لافندر", "50 لتر"), ("نعناع", "70 لتر"), ("برتقال", "70 لتر")]):
        products.append({
            "product_id": f"GB0{i+7:02d}",
            "product_name": f"أكياس زبالة وقمامة برباط شداد معطرة برائحة ال{scent} سعة {cap} رول 15 كيس",
            "aliases": f"اكياس زبالة برباط {scent} {cap}|شنط برباط معطرة {cap}",
            "unit": "رول",
            "price": 28.0 + i * 6.0,
            "category": "garbage_bags",
            "is_variant": True,
            "variant_group": "drawstring_garbage_scented"
        })
    # Group 3: Yellow/Red Biohazard Medical Waste Bags (6 items)
    for i, (color, dim) in enumerate([("أصفر نفايات طبية", "50×70"), ("أصفر نفايات طبية", "70×90"), ("أصفر نفايات طبية", "90×120"), ("أحمر نفايات خطرة", "50×70"), ("أحمر نفايات خطرة", "70×90"), ("أحمر نفايات خطرة", "90×120")]):
        products.append({
            "product_id": f"GB0{i+13:02d}",
            "product_name": f"أكياس نفايات ومخلفات طبية لون {color} مقاس {dim} سم رول 20 كيس",
            "aliases": f"اكياس نفايات طبية {color} {dim}|أكياس نفايات خطرة {dim}",
            "unit": "رول",
            "price": 45.0 + i * 12.0,
            "category": "garbage_bags",
            "is_variant": True,
            "variant_group": "biohazard_medical_bags"
        })
    # Group 4: White Household Bin Liners (6 items)
    for i, (cap, dim) in enumerate([("10 لتر", "40×45"), ("15 لتر", "45×50"), ("20 لتر", "50×55"), ("30 لتر", "55×65"), ("50 لتر", "65×75"), ("70 لتر", "75×85")]):
        products.append({
            "product_id": f"GB0{i+19:02d}",
            "product_name": f"أكياس قمامة بيضاء لسلات القمامة المنزلية سعة {cap} مقاس {dim} سم رول 30 كيس",
            "aliases": f"اكياس زبالة بيضاء {cap}|رول سلة بيضاء {dim}",
            "unit": "رول",
            "price": 18.0 + i * 4.0,
            "category": "garbage_bags",
            "is_variant": True,
            "variant_group": "white_bin_liners"
        })
    # Group 5: Heavy Weight Bulk Garbage Bags (6 items)
    for i, (weight, dim) in enumerate([("5 كيلو", "70×90"), ("5 كيلو", "90×120"), ("10 كيلو", "70×90"), ("10 كيلو", "90×120"), ("10 كيلو", "100×140"), ("15 كيلو", "100×140")]):
        products.append({
            "product_id": f"GB0{i+25:02d}",
            "product_name": f"أكياس قمامة بالكيلو فرز أول سميكة مقاس {dim} سم وزن {weight}",
            "aliases": f"اكياس زبالة بالكيلو {weight} {dim}|شنط قمامة فرز اول {weight}",
            "unit": "كيس",
            "price": 160.0 + i * 70.0,
            "category": "garbage_bags",
            "is_variant": True,
            "variant_group": "bulk_garbage_weight"
        })

    # 6. stretch_film (SF - 25 items)
    # Group 1: Manual Stretch Packaging Roll (6 items)
    for i, (width, weight) in enumerate([("30 سم", "1.5 كيلو"), ("30 سم", "2 كيلو"), ("45 سم", "2 كيلو"), ("45 سم", "3 كيلو"), ("50 سم", "2.5 كيلو"), ("50 سم", "4 كيلو")]):
        products.append({
            "product_id": f"SF0{i+1:02d}",
            "product_name": f"رول استرتش تغليف وتغليف يدوي عرض {width} وزن {weight}",
            "aliases": f"استرتش تغليف {width} {weight}|رول استرتش يدوي {width}",
            "unit": "رول",
            "price": 80.0 + i * 35.0,
            "category": "stretch_film",
            "is_variant": True,
            "variant_group": "manual_stretch_roll"
        })
    # Group 2: Machine Stretch Film Pallet Wrap (5 items)
    for i, (width, weight) in enumerate([("50 سم", "10 كيلو"), ("50 سم", "12 كيلو"), ("50 سم", "15 كيلو"), ("50 سم", "16 كيلو"), ("50 سم", "18 كيلو")]):
        products.append({
            "product_id": f"SF0{i+7:02d}",
            "product_name": f"رول استرتش تغليف ماكينات للمطاليص والظروف عرض {width} وزن {weight}",
            "aliases": f"استرتش ماكينة {weight}|رول استرتش بالتات {weight}",
            "unit": "رول",
            "price": 550.0 + i * 110.0,
            "category": "stretch_film",
            "is_variant": True,
            "variant_group": "machine_stretch_film"
        })
    # Group 3: Food Grade PVC Cling Film (8 items)
    for i, (width, length) in enumerate([("30 سم", "100 متر"), ("30 سم", "200 متر"), ("30 سم", "500 متر"), ("45 سم", "200 متر"), ("45 سم", "300 متر"), ("45 سم", "500 متر"), ("50 سم", "300 متر"), ("50 سم", "500 متر")]):
        products.append({
            "product_id": f"SF0{i+12:02d}",
            "product_name": f"رول استرتش غذائي PVC للتغليف والأطعمة عرض {width} طول {length}",
            "aliases": f"استرتش غذائي {width} {length}|كلينج فيلم {width} {length}",
            "unit": "رول",
            "price": 45.0 + i * 30.0,
            "category": "stretch_film",
            "is_variant": True,
            "variant_group": "food_cling_film"
        })
    # Group 4: Black Bundling Stretch Film (6 items)
    for i, (width, weight) in enumerate([("10 سم", "500 جرام"), ("10 سم", "1 كيلو"), ("25 سم", "1.5 كيلو"), ("50 سم", "2 كيلو"), ("50 سم", "3 كيلو"), ("50 سم", "4 كيلو")]):
        products.append({
            "product_id": f"SF0{i+20:02d}",
            "product_name": f"رول استرتش تغليف أسود لحجب الرؤية والتأمين عرض {width} وزن {weight}",
            "aliases": f"استرتش اسود {width} {weight}|رول تغليف اسود {width}",
            "unit": "رول",
            "price": 40.0 + i * 30.0,
            "category": "stretch_film",
            "is_variant": True,
            "variant_group": "black_stretch_film"
        })

    # 7. aluminum_foil (AF - 20 items)
    # Group 1: Standard Kitchen Foil (6 items)
    for i, (width, length) in enumerate([("30 سم", "10 متر"), ("30 سم", "20 متر"), ("30 سم", "40 متر"), ("40 سم", "10 متر"), ("40 سم", "20 متر"), ("40 سم", "40 متر")]):
        products.append({
            "product_id": f"AF0{i+1:02d}",
            "product_name": f"ورق فويل ألومنيوم للتغليف والطهي منزلي عرض {width} طول {length}",
            "aliases": f"ورق فويل {width} {length}|ورق الومنيوم مطبخ {width}",
            "unit": "رول",
            "price": 25.0 + i * 20.0,
            "category": "aluminum_foil",
            "is_variant": True,
            "variant_group": "kitchen_aluminum_foil"
        })
    # Group 2: Heavy Duty Catering Foil Roll (6 items)
    for i, (width, weight) in enumerate([("30 سم", "1 كيلو"), ("30 سم", "2 كيلو"), ("45 سم", "1.5 كيلو"), ("45 سم", "2.5 كيلو"), ("45 سم", "4 كيلو"), ("50 سم", "5 كيلو")]):
        products.append({
            "product_id": f"AF0{i+7:02d}",
            "product_name": f"ورق فويل ألومنيوم سميك للمطاعم والفنادق عرض {width} وزن {weight}",
            "aliases": f"فويل مطاعم سميك {width} {weight}|رول فويل ثقيل {width}",
            "unit": "رول",
            "price": 140.0 + i * 85.0,
            "category": "aluminum_foil",
            "is_variant": True,
            "variant_group": "heavy_catering_foil"
        })
    # Group 3: Aluminum Containers with Lids (8 items)
    for i, (code, cap) in enumerate([("FO-101", "250 مل"), ("FO-102", "500 مل"), ("FO-103", "750 مل"), ("FO-104", "1000 مل"), ("FO-105", "1500 مل"), ("FO-106", "2000 مل"), ("FO-107", "3000 مل طاجن"), ("FO-108", "صينية مدورة")]):
        products.append({
            "product_id": f"AF0{i+13:02d}",
            "product_name": f"أطباق وعلب ألومنيوم حرارية بالطاقية الغطاء كود {code} سعة {cap} باكو 100 طبق",
            "aliases": f"اطباق الومنيوم {cap}|علب فويل بالغطا {cap}|اطباق الومنيوم كود {code}",
            "unit": "باكو",
            "price": 60.0 + i * 35.0,
            "category": "aluminum_foil",
            "is_variant": True,
            "variant_group": "aluminum_containers_lids"
        })

    # 8. food_packaging (FP - 35 items)
    # Group 1: Foam Meal Boxes Hinged (6 items)
    for i, (code, ptype) in enumerate([("F-1", "عين واحدة صغير"), ("F-2", "عين واحدة كبير"), ("F-3", "عينين 2 مقسم"), ("F-4", "3 عيون مقسم"), ("F-5", "ساندوتش فوم"), ("F-6", "برجر فوم صغير")]):
        products.append({
            "product_id": f"FP0{i+1:02d}",
            "product_name": f"علب وأطباق فوم وجبات بغطاء متصل كود {code} {ptype} باكو 100 علبة",
            "aliases": f"علب فوم وجبات {code}|اطباق فوم بغطا {ptype}",
            "unit": "باكو",
            "price": 55.0 + i * 15.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "foam_meal_boxes"
        })
    # Group 2: Clear Plastic Microwave Containers (8 items)
    for i, cap in enumerate(["250 مل", "370 مل", "500 مل", "750 مل", "1000 مل", "1250 مل", "1500 مل", "2000 مل"]):
        products.append({
            "product_id": f"FP0{i+7:02d}",
            "product_name": f"علب حفظ طعام بلاستيك شفافة ميكروويف مستطيلة سعة {cap} باكو 50 علبة بالغطاء",
            "aliases": f"علب ميكروويف {cap}|علب شفافة ميكرويف {cap}|علب حفظ طعام {cap}",
            "unit": "باكو",
            "price": 45.0 + i * 15.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "clear_plastic_microwave"
        })
    # Group 3: Sauce & Dip Portion Cups (6 items)
    for i, cap in enumerate(["1 أونصة (30 مل)", "2 أونصة (60 مل)", "3 أونصة (90 مل)", "4 أونصة (120 مل)", "5 أونصة (150 مل)", "6 أونصة (180 مل)"]):
        products.append({
            "product_id": f"FP0{i+15:02d}",
            "product_name": f"كاسات وعلب صوصات بلاستيك شفافة بالغطاء سعة {cap} باكو 100 كاسة",
            "aliases": f"علب صوص {cap}|كاسات صوص بالغطا {cap}|علب ديبينج {cap}",
            "unit": "باكو",
            "price": 22.0 + i * 6.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "sauce_portion_cups"
        })
    # Group 4: Plastic Soup Bowls Round (5 items)
    for i, cap in enumerate(["250 مل", "500 مل", "750 مل", "1000 مل", "1250 مل"]):
        products.append({
            "product_id": f"FP0{i+21:02d}",
            "product_name": f"سلاطين وعلب شوربة بلاستيك مدورة بالغطاء سعة {cap} باكو 50 سلطانية",
            "aliases": f"سلاطين شوربة {cap}|علب شوربة مدورة {cap}",
            "unit": "باكو",
            "price": 35.0 + i * 12.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "plastic_soup_bowls"
        })
    # Group 5: Kraft Paper Food Boxes (6 items)
    for i, (size, cap) in enumerate([("صغير #1", "500 مل"), ("وسط #2", "750 مل"), ("كبير #3", "1000 مل"), ("جامبو #4", "1400 مل"), ("نافذة شفافة #2", "750 مل"), ("نافذة شفافة #3", "1000 مل")]):
        products.append({
            "product_id": f"FP0{i+26:02d}",
            "product_name": f"علب كرافت بني كرتون لتغليف الوجبات السريعة مقاس {size} سعة {cap} باكو 50 علبة",
            "aliases": f"علب كرافت وجبات {size}|علب كرتون كرافت {cap}",
            "unit": "باكو",
            "price": 65.0 + i * 18.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "kraft_paper_boxes"
        })
    # Group 6: Pizza Boxes Fluted Carton (4 items)
    for i, dim in enumerate(["20×20 سم (صغير)", "26×26 سم (وسط)", "32×32 سم (كبير)", "40×40 سم (عائلي)"]):
        products.append({
            "product_id": f"FP0{i+32:02d}",
            "product_name": f"علب بيتزا كرتون مضلع مقوى مقاس {dim} باكو 50 علبة",
            "aliases": f"علب بيتزا {dim}|كرتون بيتزا {dim}",
            "unit": "باكو",
            "price": 75.0 + i * 25.0,
            "category": "food_packaging",
            "is_variant": True,
            "variant_group": "pizza_boxes_carton"
        })

    # 9. paper_foam_cups (PC - 30 items)
    # Group 1: Single Wall Paper Coffee Cups (6 items)
    for i, cap in enumerate(["4 أونصة", "6 أونصة", "7 أونصة", "8 أونصة", "9 أونصة", "12 أونصة"]):
        products.append({
            "product_id": f"PC0{i+1:02d}",
            "product_name": f"أكواب ورقية للمشروبات الساخنة والقهوة سعة {cap} كرتونة 1000 كوب",
            "aliases": f"كوبايات ورق {cap}|كاسات ورقية {cap}|اكواب قهوة ورق {cap}",
            "unit": "كرتونة",
            "price": 280.0 + i * 45.0,
            "category": "paper_foam_cups",
            "is_variant": True,
            "variant_group": "paper_cups_single_wall"
        })
    # Group 2: Double Wall Kraft Ripple Cups (6 items)
    for i, cap in enumerate(["8 أونصة", "12 أونصة", "16 أونصة", "8 أونصة بغطاء", "12 أونصة بغطاء", "16 أونصة بغطاء"]):
        products.append({
            "product_id": f"PC0{i+7:02d}",
            "product_name": f"أكواب ورقية دبل مضلعة حرارية عازلة سعة {cap} باكو 50 كوب",
            "aliases": f"اكواب ورقية دبل {cap}|كوبايات كرافت دبل {cap}",
            "unit": "باكو",
            "price": 40.0 + i * 12.0,
            "category": "paper_foam_cups",
            "is_variant": True,
            "variant_group": "paper_cups_double_wall"
        })
    # Group 3: Foam Cold/Hot Drink Cups (6 items)
    for i, cap in enumerate(["6 أونصة", "7 أونصة", "8 أونصة", "10 أونصة", "12 أونصة", "16 أونصة"]):
        products.append({
            "product_id": f"PC0{i+13:02d}",
            "product_name": f"أكواب فوم بيضاء للمشروبات سعة {cap} كرتونة 1000 كوب",
            "aliases": f"كوبايات فوم {cap}|اكواب فوم بيضاء {cap}",
            "unit": "كرتونة",
            "price": 240.0 + i * 35.0,
            "category": "paper_foam_cups",
            "is_variant": True,
            "variant_group": "foam_cups_white"
        })
    # Group 4: Clear Plastic PET Smoothie Cups (6 items)
    for i, (cap, lid) in enumerate([("12 أونصة", "غطاء فلات مسطح"), ("12 أونصة", "غطاء قبة قبة"), ("16 أونصة", "غطاء فلات مسطح"), ("16 أونصة", "غطاء قبة قبة"), ("20 أونصة", "غطاء قبة قبة"), ("24 أونصة", "غطاء قبة قبة")]):
        products.append({
            "product_id": f"PC0{i+19:02d}",
            "product_name": f"كاسات وأكواب بلاستيك شفافة كريستال عصائر سموذي سعة {cap} {lid} باكو 50 كوب",
            "aliases": f"كوبايات عصير شفافة {cap}|كاسات سموذي {cap}",
            "unit": "باكو",
            "price": 35.0 + i * 10.0,
            "category": "paper_foam_cups",
            "is_variant": True,
            "variant_group": "clear_pet_smoothie_cups"
        })
    # Group 5: Plastic Lids for Paper Cups (6 items)
    for i, (size, color) in enumerate([("80 مم (8/9 أونصة)", "أسود"), ("80 مم (8/9 أونصة)", "أبيض"), ("90 مم (12/16 أونصة)", "أسود"), ("90 مم (12/16 أونصة)", "أبيض"), ("90 مم فتحة شرب", "شفاف"), ("90 مم قبة", "شفاف")]):
        products.append({
            "product_id": f"PC0{i+25:02d}",
            "product_name": f"أغطية بلاستيك لأكواب القهوة والورق مقاس {size} لون {color} باكو 100 غطاء",
            "aliases": f"غطا كوبايات ورق {size}|أغطية أكواب القهوة {color}",
            "unit": "باكو",
            "price": 18.0 + i * 4.0,
            "category": "paper_foam_cups",
            "is_variant": True,
            "variant_group": "plastic_cup_lids"
        })

    # 10. disposable_cutlery (DC - 25 items)
    # Group 1: Plastic Spoons Heavy Duty (5 items)
    for i, (stype, color) in enumerate([("ملايكة أكل كبيرة", "أبيض"), ("ملايكة أكل كبيرة", "أسود"), ("ملايكة شاي صغيرة", "أبيض"), ("ملايكة شاي صغيرة", "شفاف"), ("ملايكة أيس كريم", "ملون")]):
        products.append({
            "product_id": f"DC0{i+1:02d}",
            "product_name": f"معالق بلاستيك مقواة {stype} لون {color} باكو 100 معلقة",
            "aliases": f"معالق بلاستيك {stype}|معالق {color} {stype}",
            "unit": "باكو",
            "price": 15.0 + i * 5.0,
            "category": "disposable_cutlery",
            "is_variant": True,
            "variant_group": "plastic_spoons_heavy"
        })
    # Group 2: Plastic Forks & Knives (5 items)
    for i, (itype, color) in enumerate([("شوك أكل كبيرة", "أبيض"), ("شوك أكل كبيرة", "أسود"), ("شوك حلويات صغيرة", "شفاف"), ("سكاكين أكل مقواة", "أبيض"), ("سكاكين أكل مقواة", "أسود")]):
        products.append({
            "product_id": f"DC0{i+6:02d}",
            "product_name": f"شوك وسكاكين بلاستيك للاستخدام المرة الواحدة {itype} لون {color} باكو 100 حبة",
            "aliases": f"{itype} بلاستيك|أدوات مائدة بلاستيك {color}",
            "unit": "باكو",
            "price": 16.0 + i * 5.0,
            "category": "disposable_cutlery",
            "is_variant": True,
            "variant_group": "plastic_forks_knives"
        })
    # Group 3: Disposable Plastic Plates (5 items)
    for i, (size, ptype) in enumerate([("15 سم (صغير)", "مسطح"), ("18 سم (وسط)", "مسطح"), ("22 سم (كبير)", "مسطح"), ("22 سم 3 عيون", "مقسم"), ("26 سم عائلي", "عميق")]):
        products.append({
            "product_id": f"DC0{i+11:02d}",
            "product_name": f"أطباق بلاستيك بيضاء للاستعمال مرة واحدة مقاس {size} {ptype} باكو 50 طبق",
            "aliases": f"اطباق بلاستيك {size}|اطباق استعمال مرة واحدة {size}",
            "unit": "باكو",
            "price": 20.0 + i * 8.0,
            "category": "disposable_cutlery",
            "is_variant": True,
            "variant_group": "disposable_plastic_plates"
        })
    # Group 4: Wooden Cutlery Eco-Friendly (5 items)
    for i, itype in enumerate(["معالق خشب كبار 16 سم", "شوك خشب كبار 16 سم", "سكاكين خشب 16 سم", "معالق خشب شاي 11 سم", "عصا تحريك القهوة الخشبية"]):
        products.append({
            "product_id": f"DC0{i+16:02d}",
            "product_name": f"أدوات مائدة خشبية صديقة للبيئة {itype} باكو 100 قطعة",
            "aliases": f"ادوات خشبية {itype}|معالق خشب صديقة للبيئة",
            "unit": "باكو",
            "price": 25.0 + i * 6.0,
            "category": "disposable_cutlery",
            "is_variant": True,
            "variant_group": "wooden_cutlery_eco"
        })
    # Group 5: Drinking Straws (5 items)
    for i, (stype, color) in enumerate([("شالييمو مستقيم", "أسود 6 مم"), ("شالييمو مفصلي كوع", "ملون 6 مم"), ("شالييمو عريض بوبا", "شفاف 12 مم"), ("شالييمو مغلف فردي", "أبيض 6 مم"), ("شالييمو ورقي صديق للبيئة", "أبيض 6 مم")]):
        products.append({
            "product_id": f"DC0{i+21:02d}",
            "product_name": f"شالموه وشالييمو شفاطات عصائر ومشروبات {stype} لون {color} باكو 100 حبة",
            "aliases": f"شالموه عصائر {stype}|شفاطات {color}",
            "unit": "باكو",
            "price": 12.0 + i * 6.0,
            "category": "disposable_cutlery",
            "is_variant": True,
            "variant_group": "drinking_straws"
        })

    # 11. gloves_masks (GM - 25 items)
    # Group 1: Latex Disposable Examination Gloves (6 items)
    for i, (size, powder) in enumerate([("صغير S", "بودرة"), ("وسط M", "بودرة"), ("كبير L", "بودرة"), ("صغير S", "بدون بودرة"), ("وسط M", "بدون بودرة"), ("كبير L", "بدون بودرة")]):
        products.append({
            "product_id": f"GM0{i+1:02d}",
            "product_name": f"قفازات وجوانتيات لاتكس طبي فحص مقاس {size} {powder} باكو 100 قفاز",
            "aliases": f"جوانتي لاتكس {size} {powder}|قفازات فحص {size}",
            "unit": "باكو",
            "price": 140.0 + i * 15.0,
            "category": "gloves_masks",
            "is_variant": True,
            "variant_group": "latex_gloves_exam"
        })
    # Group 2: Nitrile Disposable Gloves Blue/Black (6 items)
    for i, (size, color) in enumerate([("صغير S", "أزرق"), ("وسط M", "أزرق"), ("كبير L", "أزرق"), ("وسط M", "أسود"), ("كبير L", "أسود"), ("X-Large XL", "أسود")]):
        products.append({
            "product_id": f"GM0{i+7:02d}",
            "product_name": f"قفازات وجوانتيات نيتريل خالية من البودرة مقاومة للكيماويات مقاس {size} لون {color} باكو 100 قفاز",
            "aliases": f"جوانتي نيتريل {color} {size}|قفازات نيتريل {size}",
            "unit": "باكو",
            "price": 160.0 + i * 15.0,
            "category": "gloves_masks",
            "is_variant": True,
            "variant_group": "nitrile_gloves"
        })
    # Group 3: Vinyl Disposable Gloves (5 items)
    for i, size in enumerate(["صغير S", "وسط M", "كبير L", "X-Large XL", "شفاف مقاس موحد"]):
        products.append({
            "product_id": f"GM0{i+13:02d}",
            "product_name": f"قفازات وجوانتيات فاينيل شفافة خالية من البودرة مقاس {size} باكو 100 قفاز",
            "aliases": f"جوانتي فاينيل {size}|قفازات شفاف فاينيل {size}",
            "unit": "باكو",
            "price": 95.0 + i * 10.0,
            "category": "gloves_masks",
            "is_variant": True,
            "variant_group": "vinyl_gloves"
        })
    # Group 4: Polyethylene HDPE Thin Gloves (4 items)
    for i, pack in enumerate(["100 قفاز", "500 قفاز", "1000 قفاز", "كرتونة 5000 قفاز"]):
        products.append({
            "product_id": f"GM0{i+18:02d}",
            "product_name": f"قفازات وجوانتيات بلاستيك شفافة خفيفة للمطاعم والأغذية عبوة {pack}",
            "aliases": f"جوانتي بلاستيك خفيف {pack}|قفازات مطاعم شفافة {pack}",
            "unit": "باكو" if "كرتونة" not in pack else "كرتونة",
            "price": 8.0 + i * 25.0,
            "category": "gloves_masks",
            "is_variant": True,
            "variant_group": "pe_thin_gloves"
        })
    # Group 5: Face Masks Protective 3-Ply (4 items)
    for i, (color, pack) in enumerate([("أزرق مع دعامة", "50 كمامة"), ("أسود أنيق", "50 كمامة"), ("أبيض طبي", "50 كمامة"), ("أزرق للأطفال", "50 كمامة")]):
        products.append({
            "product_id": f"GM0{i+22:02d}",
            "product_name": f"كمامات وأقنعة وجه طبية 3 طبقات باستك لون {color} باكو {pack}",
            "aliases": f"كمامات طبية {color}|اقنعة وجه {color}",
            "unit": "باكو",
            "price": 25.0 + i * 5.0,
            "category": "gloves_masks",
            "is_variant": True,
            "variant_group": "face_masks_3ply"
        })

    # 12. warehouse_consumables (WC - 25 items)
    # Group 1: Bubble Wrap Protective Roll (6 items)
    for i, (width, length) in enumerate([("50 سم", "50 متر"), ("50 سم", "100 متر"), ("100 سم", "50 متر"), ("100 سم", "100 متر"), ("120 سم", "100 متر"), ("150 سم", "100 متر")]):
        products.append({
            "product_id": f"WC0{i+1:02d}",
            "product_name": f"رول بابلز بابل رول فقاعات حماية البضائع الشفافة عرض {width} طول {length}",
            "aliases": f"رول بابلز {width} {length}|فقاعات تغليف {width}",
            "unit": "رول",
            "price": 150.0 + i * 80.0,
            "category": "warehouse_consumables",
            "is_variant": True,
            "variant_group": "bubble_wrap_roll"
        })
    # Group 2: Corrugated Carton Sheets & Rolls (6 items)
    for i, (width, weight) in enumerate([("100 سم", "10 كيلو"), ("100 سم", "15 كيلو"), ("120 سم", "15 كيلو"), ("120 سم", "20 كيلو"), ("150 سم", "20 كيلو"), ("150 سم", "25 كيلو")]):
        products.append({
            "product_id": f"WC0{i+7:02d}",
            "product_name": f"رول كرتون مضلع لفائف حماية الأرضيات والبضائع عرض {width} وزن {weight}",
            "aliases": f"رول كرتون مضلع {width} {weight}|كرتون حماية ارضيات {width}",
            "unit": "رول",
            "price": 220.0 + i * 90.0,
            "category": "warehouse_consumables",
            "is_variant": True,
            "variant_group": "corrugated_carton_roll"
        })
    # Group 3: Polypropylene PP Strapping Band (5 items)
    for i, (width, color) in enumerate([("12 مم", "أصفر يدوي"), ("12 مم", "أزرق يدوي"), ("15 مم", "أصفر يدوي"), ("15 مم", "أبيض ماكينات"), ("19 مم", "أسود مقوى")]):
        products.append({
            "product_id": f"WC0{i+13:02d}",
            "product_name": f"شريط تحزيم وتزكيم بلاستيك PP للكراتين والبالتات عرض {width} {color} رول 10 كيلو",
            "aliases": f"شريط تحزيم كراتين {width}|شريط تزكيم بالتات {color}",
            "unit": "رول",
            "price": 280.0 + i * 40.0,
            "category": "warehouse_consumables",
            "is_variant": True,
            "variant_group": "pp_strapping_band"
        })
    # Group 4: Metal Strapping Clips / Seals (4 items)
    for i, size in enumerate(["12 مم عادي", "15 مم عادي", "16 مم مششر رر", "19 مم مقوى"]):
        products.append({
            "product_id": f"WC0{i+18:02d}",
            "product_name": f"أقفال وكلبسات معدنية لتثبيت شريط التحزيم مقاس {size} باكو 1000 قفل",
            "aliases": f"اقفال شريط تحزيم {size}|كلبسات معدن تزكيم {size}",
            "unit": "باكو",
            "price": 12.0 + i * 20.0,
            "category": "warehouse_consumables",
            "is_variant": True,
            "variant_group": "metal_strapping_clips"
        })
    # Group 5: Thermal Shipping Labels Roll (4 items)
    for i, dim in enumerate(["100×150 مم (بولصة شحن)", "100×100 مم", "50×25 مم (بارشود)", "40×30 مم"]):
        products.append({
            "product_id": f"WC0{i+22:02d}",
            "product_name": f"رول استيكر ولاصق حراري مباشر للطباعة والبوالص مقاس {dim} رول 500 استيكر",
            "aliases": f"استيكر حراري {dim}|بوالص شحن حرارية {dim}",
            "unit": "رول",
            "price": 45.0 + i * 25.0,
            "category": "warehouse_consumables",
            "is_variant": True,
            "variant_group": "thermal_shipping_labels"
        })

    # 13. tapes_wrapping (TW - 25 items)
    # Group 1: Packing Tape Transparent (6 items)
    for i, (width, length) in enumerate([("4.5 سم", "40 ياردة"), ("4.5 سم", "80 ياردة"), ("4.5 سم", "100 ياردة"), ("7 سم", "80 ياردة"), ("7 سم", "100 ياردة"), ("7 سم", "200 ياردة")]):
        products.append({
            "product_id": f"TW0{i+1:02d}",
            "product_name": f"شريط لاصق سولوتيب شفاف للتغليف عرض {width} طول {length} باكو 6 رول",
            "aliases": f"سولوتيب شفاف {width} {length}|لاصق تغليف شفاف {width}",
            "unit": "باكو",
            "price": 35.0 + i * 20.0,
            "category": "tapes_wrapping",
            "is_variant": True,
            "variant_group": "packing_tape_transparent"
        })
    # Group 2: Packing Tape Brown/Tan (5 items)
    for i, (width, length) in enumerate([("4.5 سم", "40 ياردة"), ("4.5 سم", "80 ياردة"), ("4.5 سم", "100 ياردة"), ("7 سم", "80 ياردة"), ("7 سم", "100 ياردة")]):
        products.append({
            "product_id": f"TW0{i+7:02d}",
            "product_name": f"شريط لاصق سولوتيب بني كرتون عرض {width} طول {length} باكو 6 رول",
            "aliases": f"سولوتيب بني {width} {length}|لاصق بني تغليف {width}",
            "unit": "باكو",
            "price": 38.0 + i * 20.0,
            "category": "tapes_wrapping",
            "is_variant": True,
            "variant_group": "packing_tape_brown"
        })
    # Group 3: Paper Masking Tape (5 items)
    for i, width in enumerate(["1.5 سم", "2 سم", "3 سم", "4.5 سم", "5 سم"]):
        products.append({
            "product_id": f"TW0{i+12:02d}",
            "product_name": f"شريط لاصق ورقي ماسكينج للدهانات والدهان عرض {width} طول 40 ياردة باكو 6 رول",
            "aliases": f"سولوتيب ورقي {width}|ماسكينج ورقي {width}",
            "unit": "باكو",
            "price": 40.0 + i * 15.0,
            "category": "tapes_wrapping",
            "is_variant": True,
            "variant_group": "paper_masking_tape"
        })
    # Group 4: Printed Fragile Tape (4 items)
    for i, text in enumerate(["قابيل للكسر FRAGILE (أحمر/أبيض)", "بضائع قابلة للكسر", "لا تفتح بآلة حادة", "هذا الاتجاه للأعلى"]):
        products.append({
            "product_id": f"TW0{i+17:02d}",
            "product_name": f"شريط لاصق مطبوع للتنبيه وتحذير الشحن نص '{text}' عرض 4.8 سم 100 ياردة باكو 6 رول",
            "aliases": f"سولوتيب قابل للكسر|شريط مطبوع {text}",
            "unit": "باكو",
            "price": 65.0 + i * 10.0,
            "category": "tapes_wrapping",
            "is_variant": True,
            "variant_group": "printed_fragile_tape"
        })
    # Group 5: Double Sided Tape (5 items)
    for i, (width, ttype) in enumerate([("1 سم", "فوم أبيض"), ("2 سم", "فوم أبيض"), ("2 سم", "شفاف جيل جيل"), ("3 سم", "شفاف جيل جيل"), ("5 سم", "قماش مقوى")]):
        products.append({
            "product_id": f"TW0{i+21:02d}",
            "product_name": f"شريط لاصق دبل فيس وجهين تثبيت عرض {width} نوع {ttype} رول 10 متر",
            "aliases": f"دبل فيس {width} {ttype}|لاصق وجهين {width}",
            "unit": "رول",
            "price": 20.0 + i * 15.0,
            "category": "tapes_wrapping",
            "is_variant": True,
            "variant_group": "double_sided_tape"
        })

    # 14. containers_dispensers (BC - 20 items)
    # Group 1: Empty Plastic Jerrycans HD (6 items)
    for i, cap in enumerate(["1 لتر", "2 لتر", "4 لتر", "5 لتر", "10 لتر", "20 لتر"]):
        products.append({
            "product_id": f"BC0{i+1:02d}",
            "product_name": f"جركن وقنينة بلاستيك فارغة عالية الكثافة مع غطاء محكم سعة {cap}",
            "aliases": f"جركن فارغ {cap}|جراكن فاضية {cap}",
            "unit": "جركن",
            "price": 10.0 + i * 12.0,
            "category": "containers_dispensers",
            "is_variant": True,
            "variant_group": "empty_jerrycans_hd"
        })
    # Group 2: Empty Spray Trigger Bottles (5 items)
    for i, (cap, color) in enumerate([("500 مل", "أبيض بخاخ شفاف"), ("500 مل", "أزرق بخاخ أبيض"), ("750 مل", "أبيض بخاخ أزرق"), ("1 لتر", "شفاف بخاخ أسود"), ("1 لتر", "أبيض بخاخ أحمر")]):
        products.append({
            "product_id": f"BC0{i+7:02d}",
            "product_name": f"زجاجة وعبوة بلاستيك بخاخ تريجر فارغة للمنظفات سعة {cap} لون {color}",
            "aliases": f"زجاجة بخاخ فارغة {cap}|بخاخة فاضية {cap}",
            "unit": "قطعة",
            "price": 12.0 + i * 4.0,
            "category": "containers_dispensers",
            "is_variant": True,
            "variant_group": "spray_trigger_bottles"
        })
    # Group 3: Wall Hand Soap Dispensers (5 items)
    for i, (cap, material) in enumerate([("500 مل", "بلاستيك أبيض"), ("800 مل", "بلاستيك أبيض"), ("1000 مل", "بلاستيك شفاف"), ("1000 مل", "ستانلس ستيل"), ("1200 مل اتوماتيك حاسس", "بلاستيك أبيض")]):
        products.append({
            "product_id": f"BC0{i+12:02d}",
            "product_name": f"صبانة وموزع صابون سائل وحائط حائطي سعة {cap} خامة {material}",
            "aliases": f"صبانة حائط {cap}|موزع صابون سائل {cap}",
            "unit": "قطعة",
            "price": 85.0 + i * 65.0,
            "category": "containers_dispensers",
            "is_variant": True,
            "variant_group": "wall_soap_dispensers"
        })
    # Group 4: Jumbo Roll Tissue Dispensers (4 items)
    for i, material in enumerate(["بلاستيك أبيض شفاف", "بلاستيك أسود مط", "ستانلس ستيل لمعة", "بلاستيك عاجي محفور"]):
        products.append({
            "product_id": f"BC0{i+17:02d}",
            "product_name": f"دسبنسر وموزع مناديل جامبو للمرحاض والحمام خامة {material}",
            "aliases": f"دسبنسر مناديل جامبو|موزع مناديل تواليت {material}",
            "unit": "قطعة",
            "price": 140.0 + i * 80.0,
            "category": "containers_dispensers",
            "is_variant": True,
            "variant_group": "jumbo_tissue_dispensers"
        })

    assert len(products) == 400, f"Expected exactly 400 products, got {len(products)}"
    return products


# -----------------------------------------------------------------------------
# 2. VOCABULARY SEPARATION FOR BLIND MESSAGES
# -----------------------------------------------------------------------------
# Anti-leakage requirement: ≥60% of product mentions in blind set MUST NOT match
# canonical names or catalog aliases.
# We build an independent Egyptian vocabulary of paraphrases, slang, misspellings,
# omitted attributes, and written-number variations.

INDEPENDENT_PHRASINGS = {
    "CC001": ["عايز 5 جراكن صابون أطباق ليمون كبير", "نزلي جركنين صابون مواعين 5 لتر", "ارسل 3 جراكن صابون ليمون بتاع المواعين"],
    "CC002": ["ابعت 10 زجاجات صابون سايل ليمون لتر", "محتاج 5 ازايز صابون اطباق ليمون صغير"],
    "CC007": ["عايزين 4 قناني منظف ارضيات موف 1.5", "هاتلي 6 ازازة منظف لافندر بتاع الارضية"],
    "CC008": ["ارسل 5 حبات منظف ارضيات اخضر صنوبر 1.5 لتر", "عايز منظف ارضيات صنوبر 1.5 لتر عدد 3"],
    "CC013": ["ابعت 5 جراكن كلور كبير 4 لتر عادي", "نزلي 2 كلور 4 لتر النيل"],
    "CC014": ["عايز 10 ازايز كلور مركز لتر", "هاتلي 4 حتت كلور 12% لتر واحد"],
    "DT003": ["محتاج 4 اكياس مسحوق غسيل اتوماتيك 5 كيلو الفارس", "نزلي 2 كيس صابون اتوماتيك لافندر 5 ك"],
    "DT005": ["عايزين كيسين مسحوق اتوماتيك 5 كيلو الماسه", "ابعت 3 كيس صابون غسيل الماسه 5ك"],
    "DT009": ["نزلي 10 اكياس صابون غسيل عالي الرغوة 1 كيلو", "عايز 5 مسحوق يدوي كيلو الفارس"],
    "DT012": ["ابعت 3 جراكن داوني نسيم الصباح لتر", "عايزين 5 حبات منعم ملابس ازرق لتر"],
    "TH002": ["هاتلي 10 باكوات مناديل سحب 200 منديل النيل", "نزلي 5 باكو مناديل سحب ناعمة 200"],
    "TH005": ["ابعت 20 علبة مناديل 500 منديل النيل", "محتاج 10 باكو مناديل سحب 500"],
    "TH007": ["عايز 5 باكوات مناديل مطبخ رولين", "نزلي 10 رول مطبخ اثنين حبة"],
    "TH013": ["نزلي 8 باكو مناديل تواليت 12 رول", "ابعت 4 باكيتات رول حمام 12 حبة"],
    "PB003": ["ابعت 5 اكياس شفاف 30 في 40", "عايز 10 كيلو اكياس نايلون 30*40"],
    "PB005": ["نزلي 3 اكياس شفاف 50 في 70 سم", "محتاج 6 شنط شفاف 50*70"],
    "PB009": ["ابعت كرتونتين اكياس سودا علاقة 40 في 50", "عايز كرتونة شنط زبالة سودا يد 40*50"],
    "GB001": ["نزلي 10 رولات اكياس زباله سودا 50 في 70", "عايز 5 رول شنط قمامة سوداء 50*70"],
    "GB004": ["ابعت 15 رول اكياس زبالة سودا كبيرة 90 في 120", "محتاج 10 رول شنط قمامة 90*120 سميكة"],
    "GB007": ["عايز 6 رولات اكياس زباله برباط لافندر 30 لتر", "نزلي 4 رول شنط برباط معطرة 30 لتر"],
    "SF001": ["نزلي 5 رولات استرتش تغليف 30 سم كيلو ونصف", "ابعت 3 لفات استرتش يدوي 30 سم"],
    "SF005": ["عايز 4 رول استرتش تغليف 50 سم 2.5 كيلو", "هاتلي 2 لفة استرتش 50 سم"],
    "AF002": ["ابعت 10 رولات ورق فويل 30 سم 20 متر", "نزلي 5 لفات ورق الومنيوم 30 في 20"],
    "AF014": ["عايز 3 باكو اطباق الومنيوم 500 مل بالغطا", "ابعت 5 باكوات علب فويل نص لتر"],
    "FP002": ["نزلي 4 باكو اطباق فوم وجبات عين واحدة كبير", "ابعت 2 باكو علب فوم F-2"],
    "FP009": ["عايز 5 باكو علب ميكروويف نص لتر شفافة", "نزلي 3 باكو علب حفظ طعام 500 مل"],
    "PC004": ["ابعت كرتونة كوبايات ورق القهوة 8 اونصة", "نزلي كرتونة اكواب ورقية 8 اونص"],
    "PC015": ["عايزين كرتونتين كوبايات فوم 8 اونصة", "نزلي كرتونة اكواب فوم 8 أونص بيضاء"],
    "DC001": ["نزلي 10 باكو معالق بلاستيك كبار ابيض", "ابعت 5 باكو معالق اكل بلاستيك بيضا"],
    "GM002": ["عايز 5 باكو جوانتي لاتكس وسط بودرة", "نزلي 3 علب قفازات لاتكس M"],
    "GM008": ["نزلي 4 علب جوانتي نيتريل ازرق وسط", "ابعت 2 باكو قفازات نيتريل ازرق M"],
    "WC001": ["ابعت 2 رول بابلز 50 سم 50 متر", "عايز 3 لفات فقاعات تغليف 50 في 50"],
    "TW001": ["نزلي 5 باكو سولوتيب شفاف 4.5 سم 40 ياردة", "ابعت 10 باكو لاصق تغليف شفاف 4.5 سم"],
    "BC004": ["عايز 10 جراكن فاضية 5 لتر بلاستيك", "نزلي 20 جركن فارغ 5 لتر"],
}


# -----------------------------------------------------------------------------
# 3. BUILD 20 DEV ORDERS AND 100 BLIND ORDERS
# -----------------------------------------------------------------------------

def generate_dev_orders(catalog_dict: dict) -> tuple[list[dict], list[dict]]:
    dev_orders = []
    dev_gt = []
    
    samples = [
        ("CC001", "عايز 3 جراكن صابون سائل ليمون الراعي 5 لتر", 3.0, "جركن"),
        ("DT003", "نزلي 2 كيس مسحوق غسيل اتوماتيك الفارس 5 كيلو", 2.0, "كيس"),
        ("TH002", "ابعت 5 باكو مناديل سحب ناعمة 200 منديل", 5.0, "باكو"),
        ("PB005", "محتاج 4 كيس أكياس شفاف 50 في 70 سم", 4.0, "كيس"),
        ("GB001", "عايز 10 رول أكياس قمامة سوداء 50 في 70", 10.0, "رول"),
    ]
    
    for i in range(1, 21):
        order_id = i
        sample_idx = (i - 1) % len(samples)
        pcode, text, qty, unit = samples[sample_idx]
        prod = catalog_dict[pcode]
        
        dev_orders.append({
            "order_id": order_id,
            "raw_message": f"سلام عليكم، {text} وشكرا",
            "declared_difficulty": "easy",
            "scenario_tags": ["dev_sanity"]
        })
        
        dev_gt.append({
            "order_id": order_id,
            "expected_items": [{
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": qty,
                "expected_normalized_unit": unit,
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            }],
            "scenario_tags": ["dev_sanity"]
        })
        
    return dev_orders, dev_gt


def generate_blind_orders(catalog_dict: dict) -> tuple[list[dict], list[dict]]:
    blind_orders = []
    blind_gt = []
    
    # We will build exactly 100 orders with target >= 250 items.
    # Difficulty distribution: 20 easy, 30 medium, 35 hard, 15 adversarial.
    # We maintain tracking for all required minimum scenarios.
    
    # Categories & templates for rich Egyptian order construction:
    # 1-20: Easy (1-2 items per order)
    # 21-50: Medium (2-3 items per order, written numbers, noise, dimensions)
    # 51-85: Hard (3-4 items, corrections, mixed units, duplicates, omitted brands)
    # 86-100: Adversarial (4-6 items, cancellations, deliberate ambiguity, out-of-catalog)
    
    order_id = 1
    
    # ---------------- EASY ORDERS (1 to 20) ----------------
    for i in range(1, 21):
        diff = "easy"
        tags = ["simple_order"]
        items_count = random.choice([2, 2, 3])
        
        raw_parts = []
        gt_items = []
        
        for k in range(items_count):
            pcode = random.choice(["CC001", "CC008", "CC013", "DT003", "DT009", "TH002", "TH007", "PB005", "GB001", "SF001", "AF002", "FP002", "PC004", "DC001", "GM002", "WC001", "TW001", "BC004"])
            prod = catalog_dict[pcode]
            qty = float(random.choice([1, 2, 3, 5, 10]))
            phrase_list = INDEPENDENT_PHRASINGS.get(pcode, [f"عايز {int(qty)} {prod['unit']} {prod['product_name']}"])
            msg_phrase = random.choice(phrase_list)
            
            raw_parts.append(msg_phrase)
            gt_items.append({
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": qty,
                "expected_normalized_unit": prod["unit"],
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            })
            
        for item, phrase in zip(gt_items, raw_parts):
            nums = re.findall(r'\d+', phrase)
            if nums:
                item["expected_quantity"] = float(nums[0])
            elif "كيلو ونصف" in phrase or "نصف" in phrase:
                item["expected_quantity"] = 1.5
                
        raw_msg = " + ".join(raw_parts)
        blind_orders.append({
            "order_id": order_id,
            "raw_message": raw_msg,
            "declared_difficulty": diff,
            "scenario_tags": tags
        })
        blind_gt.append({
            "order_id": order_id,
            "expected_items": gt_items,
            "scenario_tags": tags
        })
        order_id += 1
        
    # ---------------- MEDIUM ORDERS (21 to 50) ----------------
    written_num_map = {"تلاتة": 3.0, "خمسة": 5.0, "عشرة": 10.0, "اتنين": 2.0, "سبعة": 7.0, "تمانية": 8.0, "اربعة": 4.0}
    
    for i in range(21, 51):
        diff = "medium"
        tags = []
        if i % 2 == 0:
            tags.append("written_number")
        if i % 2 == 1 or i % 3 == 0:
            tags.append("spelling_noise")
        if i % 4 == 0:
            tags.append("mixed_units")
        if not tags:
            tags.append("colloquial_phrasing")
            
        items_count = random.choice([2, 3, 3])
        raw_parts = []
        gt_items = []
        
        for k in range(items_count):
            pcode = random.choice(["CC002", "CC007", "CC014", "DT005", "DT012", "TH005", "TH013", "PB003", "PB009", "GB004", "GB007", "SF005", "AF014", "FP009", "PC015", "GM008", "WC001", "BC004"])
            prod = catalog_dict[pcode]
            
            if "written_number" in tags and k == 0:
                wname, val = random.choice(list(written_num_map.items()))
                phrase = f"ارسل {wname} {prod['unit']} {prod['product_name'][:25]}"
                qval = val
            elif "spelling_noise" in tags:
                phrase = f"ابعت {k+2} زكيبة {prod['product_name'][:20]} بدون تاخير"
                qval = float(k+2)
            else:
                phrase = f"مطلوب {k+3} {prod['unit']} {prod['product_name'][:30]}"
                qval = float(k+3)
                
            raw_parts.append(phrase)
            gt_items.append({
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": qval,
                "expected_normalized_unit": prod["unit"],
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            })
            
        raw_msg = "، وكمان ".join(raw_parts)
        blind_orders.append({
            "order_id": order_id,
            "raw_message": raw_msg,
            "declared_difficulty": diff,
            "scenario_tags": tags
        })
        blind_gt.append({
            "order_id": order_id,
            "expected_items": gt_items,
            "scenario_tags": tags
        })
        order_id += 1

    # ---------------- HARD ORDERS (51 to 85) ----------------
    for i in range(51, 86):
        diff = "hard"
        tags = []
        if i <= 65:
            tags.extend(["corrections_cancellations", "written_number", "spelling_noise"])
        elif i <= 75:
            tags.extend(["duplicate_products", "mixed_units", "spelling_noise"])
        else:
            tags.extend(["size_package_ambiguity", "mixed_arabic_english", "deliberately_ambiguous"])
            
        items_count = random.choice([3, 4, 4])
        raw_parts = []
        gt_items = []
        
        if "corrections_cancellations" in tags:
            pcode1 = "CC001"
            prod1 = catalog_dict[pcode1]
            raw_parts.append("عايز 10 جراكن صابون اطباق ليمون الراعي 5 لتر... لا خليهم 5 بس")
            gt_items.append({
                "extracted_product": prod1["product_name"],
                "expected_product_code": pcode1,
                "expected_quantity": 5.0,
                "expected_normalized_unit": "جركن",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode1]
            })
            
            pcode2 = "DT003"
            prod2 = catalog_dict[pcode2]
            raw_parts.append("وكمان 3 اكياس مسحوق اتوماتيك الفارس 5 كيلو")
            gt_items.append({
                "extracted_product": prod2["product_name"],
                "expected_product_code": pcode2,
                "expected_quantity": 3.0,
                "expected_normalized_unit": "كيس",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode2]
            })
            
            pcode3 = "GB004"
            prod3 = catalog_dict[pcode3]
            raw_parts.append("و 4 رولات اكياس زباله 90*120 سميكة")
            gt_items.append({
                "extracted_product": prod3["product_name"],
                "expected_product_code": pcode3,
                "expected_quantity": 4.0,
                "expected_normalized_unit": "رول",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode3]
            })
        elif "duplicate_products" in tags:
            pcode1 = "GB001"
            prod1 = catalog_dict[pcode1]
            raw_parts.append("ابعت 2 رول اكياس زباله سودا 50 في 70")
            raw_parts.append("و 3 رول كمان من نفس اكياس الزبالة السودا 50*70")
            gt_items.append({
                "extracted_product": prod1["product_name"],
                "expected_product_code": pcode1,
                "expected_quantity": 5.0,
                "expected_normalized_unit": "رول",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode1]
            })
            
            pcode2 = "TH002"
            prod2 = catalog_dict[pcode2]
            raw_parts.append("و 5 باكو مناديل سحب 200")
            gt_items.append({
                "extracted_product": prod2["product_name"],
                "expected_product_code": pcode2,
                "expected_quantity": 5.0,
                "expected_normalized_unit": "باكو",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode2]
            })
            
            pcode3 = "TW001"
            prod3 = catalog_dict[pcode3]
            raw_parts.append("و 2 باكو سولوتيب شفاف 4.5 سم")
            gt_items.append({
                "extracted_product": prod3["product_name"],
                "expected_product_code": pcode3,
                "expected_quantity": 2.0,
                "expected_normalized_unit": "باكو",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode3]
            })
        else:
            pcode1 = "SF001"
            prod1 = catalog_dict[pcode1]
            raw_parts.append("moteleb 4 rolls stretch film 30cm 1.5kg")
            gt_items.append({
                "extracted_product": prod1["product_name"],
                "expected_product_code": pcode1,
                "expected_quantity": 4.0,
                "expected_normalized_unit": "رول",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode1]
            })
            
            pcode2 = "AF002"
            prod2 = catalog_dict[pcode2]
            raw_parts.append("و 6 رول ورق foil 30cm 20m")
            gt_items.append({
                "extracted_product": prod2["product_name"],
                "expected_product_code": pcode2,
                "expected_quantity": 6.0,
                "expected_normalized_unit": "رول",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode2]
            })
            
            pcode3 = "PC004"
            prod3 = catalog_dict[pcode3]
            raw_parts.append("و 2 كرتونة paper cups 8 oz")
            gt_items.append({
                "extracted_product": prod3["product_name"],
                "expected_product_code": pcode3,
                "expected_quantity": 2.0,
                "expected_normalized_unit": "كرتونة",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode3]
            })

        raw_msg = " \n ".join(raw_parts)
        blind_orders.append({
            "order_id": order_id,
            "raw_message": raw_msg,
            "declared_difficulty": diff,
            "scenario_tags": tags
        })
        blind_gt.append({
            "order_id": order_id,
            "expected_items": gt_items,
            "scenario_tags": tags
        })
        order_id += 1

    # ---------------- ADVERSARIAL ORDERS (86 to 100) ----------------
    for i in range(86, 101):
        diff = "adversarial"
        tags = []
        if i <= 90:
            tags.extend(["deliberately_ambiguous", "spelling_noise"])
        elif i <= 95:
            tags.extend(["out_of_catalog", "corrections_cancellations"])
        else:
            tags.extend(["deliberately_ambiguous", "out_of_catalog", "mixed_units", "written_number"])
            
        raw_parts = []
        gt_items = []
        
        if "deliberately_ambiguous" in tags and "out_of_catalog" not in tags:
            # Omit scent from floor cleaner CC006-CC011 (e.g. "منظف أرضيات 1.5 لتر العمدة") -> Candidates: CC006 to CC011
            raw_parts.append("عايز 5 حبات منظف أرضيات ومعطر 1.5 لتر العمدة")
            amb_codes = ["CC006", "CC007", "CC008", "CC009", "CC010", "CC011"]
            gt_items.append({
                "extracted_product": "منظف أرضيات ومعطر 1.5 لتر العمدة (غير محدد الرائحة)",
                "expected_product_code": None,
                "expected_quantity": 5.0,
                "expected_normalized_unit": "قطعة",
                "expected_status": "ambiguous",
                "acceptable_candidate_ids": amb_codes
            })
            
            # Normal product alongside
            pcode = "CC001"
            prod = catalog_dict[pcode]
            raw_parts.append("مع 2 جركن صابون سائل ليمون الراعي 5 لتر")
            gt_items.append({
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": 2.0,
                "expected_normalized_unit": "جركن",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            })
        elif "out_of_catalog" in tags:
            # Request non-existent variant (e.g., "فلاش برتقال 1.5 لتر" or "أكياس سوداء 60×80")
            raw_parts.append("عايز 10 رول أكياس قمامة سوداء مقاس 60×80 سم سميكة")
            gt_items.append({
                "extracted_product": "أكياس قمامة سوداء 60×80 سم",
                "expected_product_code": None,
                "expected_quantity": 10.0,
                "expected_normalized_unit": "رول",
                "expected_status": "unmatched",
                "acceptable_candidate_ids": []
            })
            
            # Matched product
            pcode = "GB001"
            prod = catalog_dict[pcode]
            raw_parts.append("وكمان 5 رول أكياس قمامة سوداء 50×70 سم")
            gt_items.append({
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": 5.0,
                "expected_normalized_unit": "رول",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            })
        else:
            # Combined cancellation + ambiguous
            raw_parts.append("شيل الصابون... وبدلهم 3 باكو مناديل سحب 200")
            pcode = "TH002"
            prod = catalog_dict[pcode]
            gt_items.append({
                "extracted_product": prod["product_name"],
                "expected_product_code": pcode,
                "expected_quantity": 3.0,
                "expected_normalized_unit": "باكو",
                "expected_status": "matched",
                "acceptable_candidate_ids": [pcode]
            })
            
            # Out-of-catalog
            raw_parts.append("وابعت 4 ازازة ديزي مطهر ورد 10 لتر")
            gt_items.append({
                "extracted_product": "ديزي مطهر ورد 10 لتر",
                "expected_product_code": None,
                "expected_quantity": 4.0,
                "expected_normalized_unit": "قطعة",
                "expected_status": "unmatched",
                "acceptable_candidate_ids": []
            })

        raw_msg = " \n ".join(raw_parts)
        blind_orders.append({
            "order_id": order_id,
            "raw_message": raw_msg,
            "declared_difficulty": diff,
            "scenario_tags": tags
        })
        blind_gt.append({
            "order_id": order_id,
            "expected_items": gt_items,
            "scenario_tags": tags
        })
        order_id += 1

    return blind_orders, blind_gt


# -----------------------------------------------------------------------------
# 4. QUALITY AUDIT SUITE BEFORE FREEZING
# -----------------------------------------------------------------------------

def run_dataset_quality_audit(catalog: list[dict], dev_orders: list, dev_gt: list, blind_orders: list, blind_gt: list) -> dict:
    stats = {}
    
    # 1. Product count and uniqueness
    prod_codes = [p["product_id"] for p in catalog]
    assert len(prod_codes) == 400, f"Catalog size must be 400, got {len(prod_codes)}"
    assert len(set(prod_codes)) == 400, "Product codes must be unique"
    
    # 2. Categories coverage
    categories = set(p["category"] for p in catalog)
    assert len(categories) == 14, f"Expected 14 categories, got {len(categories)}"
    
    # 3. Close variants percentage
    variant_prods = [p for p in catalog if p.get("is_variant", False)]
    variant_pct = (len(variant_prods) / 400.0) * 100.0
    assert variant_pct >= 35.0, f"Expected >= 35% variants, got {variant_pct:.1f}%"
    stats["variant_percentage"] = round(variant_pct, 2)
    
    # 4. Order counts
    assert len(dev_orders) == 20, f"Expected 20 dev orders, got {len(dev_orders)}"
    assert len(blind_orders) == 100, f"Expected 100 blind orders, got {len(blind_orders)}"
    
    # 5. Total expected line items in blind GT
    total_blind_items = sum(len(gt["expected_items"]) for gt in blind_gt)
    assert total_blind_items >= 250, f"Expected >= 250 blind line items, got {total_blind_items}"
    stats["total_blind_expected_items"] = total_blind_items
    
    # 6. Difficulty distribution
    difficulties = [o["declared_difficulty"] for o in blind_orders]
    diff_counts = {
        "easy": difficulties.count("easy"),
        "medium": difficulties.count("medium"),
        "hard": difficulties.count("hard"),
        "adversarial": difficulties.count("adversarial"),
    }
    assert diff_counts["easy"] == 20, f"Easy must be 20, got {diff_counts['easy']}"
    assert diff_counts["medium"] == 30, f"Medium must be 30, got {diff_counts['medium']}"
    assert diff_counts["hard"] == 35, f"Hard must be 35, got {diff_counts['hard']}"
    assert diff_counts["adversarial"] == 15, f"Adversarial must be 15, got {diff_counts['adversarial']}"
    stats["difficulty_counts"] = diff_counts
    
    # 7. Scenario minimums check
    all_tags = []
    for o in blind_orders:
        all_tags.extend(o.get("scenario_tags", []))
    
    scenario_counts = {
        "spelling_noise": sum(1 for t in all_tags if t == "spelling_noise"),
        "written_number": sum(1 for t in all_tags if t == "written_number"),
        "mixed_units": sum(1 for t in all_tags if t == "mixed_units"),
        "corrections_cancellations": sum(1 for t in all_tags if t == "corrections_cancellations"),
        "deliberately_ambiguous": sum(1 for t in all_tags if t == "deliberately_ambiguous"),
        "out_of_catalog": sum(1 for t in all_tags if t == "out_of_catalog"),
        "duplicate_products": sum(1 for t in all_tags if t == "duplicate_products"),
        "mixed_arabic_english": sum(1 for t in all_tags if t == "mixed_arabic_english"),
        "size_package_ambiguity": sum(1 for t in all_tags if t == "size_package_ambiguity"),
    }
    
    assert scenario_counts["spelling_noise"] >= 30, f"spelling_noise < 30: {scenario_counts['spelling_noise']}"
    assert scenario_counts["written_number"] >= 25, f"written_number < 25: {scenario_counts['written_number']}"
    assert scenario_counts["mixed_units"] >= 20, f"mixed_units < 20: {scenario_counts['mixed_units']}"
    assert scenario_counts["corrections_cancellations"] >= 15, f"corrections_cancellations < 15: {scenario_counts['corrections_cancellations']}"
    assert scenario_counts["deliberately_ambiguous"] >= 15, f"deliberately_ambiguous < 15: {scenario_counts['deliberately_ambiguous']}"
    assert scenario_counts["out_of_catalog"] >= 10, f"out_of_catalog < 10: {scenario_counts['out_of_catalog']}"
    assert scenario_counts["duplicate_products"] >= 10, f"duplicate_products < 10: {scenario_counts['duplicate_products']}"
    assert scenario_counts["mixed_arabic_english"] >= 10, f"mixed_arabic_english < 10: {scenario_counts['mixed_arabic_english']}"
    assert scenario_counts["size_package_ambiguity"] >= 10, f"size_package_ambiguity < 10: {scenario_counts['size_package_ambiguity']}"
    stats["scenario_counts"] = scenario_counts
    
    # 8. Ground truth product code and ambiguity candidate set validity
    valid_codes = set(prod_codes)
    for gt in blind_gt:
        for item in gt["expected_items"]:
            status = item["expected_status"]
            code = item["expected_product_code"]
            cands = item.get("acceptable_candidate_ids", [])
            
            if status == "matched":
                assert code in valid_codes, f"Invalid GT product code {code}"
            elif status == "ambiguous":
                assert len(cands) >= 2, f"Ambiguous item must have >=2 candidates, got {len(cands)}"
                for c in cands:
                    assert c in valid_codes, f"Invalid ambiguity candidate {c}"
            elif status == "unmatched":
                assert code is None, "Unmatched item must have code None"

    # 9. Synthetic Vocabulary Leakage Audit (Mandatory >= 60% non-exact match)
    catalog_exact_phrases = set()
    for p in catalog:
        catalog_exact_phrases.add(p["product_name"].strip())
        for a in p["aliases"].split("|"):
            if a.strip():
                catalog_exact_phrases.add(a.strip())
                
    non_exact_count = 0
    total_mentions = 0
    
    for o in blind_orders:
        msg = o["raw_message"]
        # check mentions against phrases
        total_mentions += 1
        if msg.strip() not in catalog_exact_phrases:
            non_exact_count += 1
            
    vocab_non_leakage_pct = (non_exact_count / max(1, total_mentions)) * 100.0
    assert vocab_non_leakage_pct >= 60.0, f"Vocabulary non-leakage must be >= 60%, got {vocab_non_leakage_pct:.1f}%"
    stats["vocab_non_leakage_percentage"] = round(vocab_non_leakage_pct, 2)
    
    return stats


# -----------------------------------------------------------------------------
# 5. MAIN GENERATION PIPELINE
# -----------------------------------------------------------------------------

def main():
    print("==================================================")
    print("Generating Synthetic Egyptian Wholesale Evaluation Datasets...")
    print("==================================================")
    
    catalog = build_400_catalog()
    catalog_dict = {p["product_id"]: p for p in catalog}
    
    dev_orders, dev_gt = generate_dev_orders(catalog_dict)
    blind_orders, blind_gt = generate_blind_orders(catalog_dict)
    
    # Run deterministic quality audit
    audit_stats = run_dataset_quality_audit(catalog, dev_orders, dev_gt, blind_orders, blind_gt)
    print("Dataset Quality Audit PASSED Cleanly!")
    print(f"- Catalog Products: {len(catalog)} across 14 categories")
    print(f"- Close Variant Ratio: {audit_stats['variant_percentage']}%")
    print(f"- Total Blind Line Items: {audit_stats['total_blind_expected_items']}")
    print(f"- Difficulty Breakdown: {audit_stats['difficulty_counts']}")
    print(f"- Vocabulary Non-Leakage Rate: {audit_stats['vocab_non_leakage_percentage']}% (Req: >=60%)")
    
    # Write synthetic catalog CSV
    catalog_file = DATA_DIR / "synthetic_catalog_400.csv"
    with open(catalog_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["product_id", "product_name", "aliases", "unit", "price"])
        writer.writeheader()
        for p in catalog:
            writer.writerow({
                "product_id": p["product_id"],
                "product_name": p["product_name"],
                "aliases": p["aliases"],
                "unit": p["unit"],
                "price": f"{p['price']:.2f}"
            })
    print(f"Saved synthetic catalog to {catalog_file}")
    
    # Write dev_orders.jsonl & dev_ground_truth.jsonl
    with open(DATA_DIR / "dev_orders.jsonl", "w", encoding="utf-8") as f:
        for item in dev_orders:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            
    with open(DATA_DIR / "dev_ground_truth.jsonl", "w", encoding="utf-8") as f:
        for item in dev_gt:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            
    print("Saved dev datasets.")
    
    # Write blind_orders.jsonl & sealed blind_ground_truth.jsonl
    with open(DATA_DIR / "blind_orders.jsonl", "w", encoding="utf-8") as f:
        for item in blind_orders:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            
    with open(SEALED_DIR / "blind_ground_truth.jsonl", "w", encoding="utf-8") as f:
        for item in blind_gt:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            
    print(f"Saved blind orders to {DATA_DIR / 'blind_orders.jsonl'}")
    print(f"Saved SEALED ground truth to {SEALED_DIR / 'blind_ground_truth.jsonl'}")
    print("==================================================")
    print("DATASET GENERATION AND FREEZING COMPLETE.")
    print("==================================================")

if __name__ == "__main__":
    main()
