import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.normalizer import normalize_arabic, normalize_unit

class AttributeContradictionDetector:
    """
    Implements deterministic penalties for explicitly stated, high-confidence contradictions.
    Per instructions:
    - Missing attributes must never be treated as contradictions.
    - Uncertain attributes must never eliminate a candidate.
    - Hard rejection / huge penalty is permitted only for exact, explicit, high-confidence contradictions:
      * Volume/capacity (e.g., 5 L vs 20 L)
      * Concentration / percentage (e.g., 5% vs 12%)
      * Dimensions (e.g., 50x70 vs 70x90)
      * Pack / package count (e.g., pack of 50 vs pack of 100)
      * Explicitly named brand A vs brand B (e.g., الفارس vs الراعي vs النيل)
      * Explicit color (e.g., red vs white vs black vs blue vs green vs yellow)
    """
    def __init__(self):
        self.known_brands = [
            "الفارس", "الراعي", "النيل", "الماسة", "الاهرام", "الشروق", "البدر",
            "سانتيا", "فاين", "زينة", "بليدج", "ديتول", "بريل", "فيري"
        ]
        self.known_colors = {
            "ابيض": "white",
            "بيضا": "white",
            "أبيض": "white",
            "اسود": "black",
            "سودا": "black",
            "أزرق": "blue",
            "ازرق": "blue",
            "احمر": "red",
            "أحمر": "red",
            "أخضر": "green",
            "اخضر": "green",
            "اصفر": "yellow",
            "أصفر": "yellow",
            "شفاف": "transparent",
            "شفافة": "transparent",
            "ملون": "colored",
            "ملونة": "colored"
        }

    def parse_attributes(self, text):
        if not text:
            return {}
        norm_text = normalize_arabic(str(text)).lower()
        attrs = {
            "brands": set(),
            "colors": set(),
            "volumes": set(),
            "weights": set(),
            "dimensions": set(),
            "pack_counts": set(),
            "percentages": set()
        }
        
        # Brands
        for b in self.known_brands:
            if normalize_arabic(b) in norm_text:
                attrs["brands"].add(b)
                
        # Colors
        for w in norm_text.split():
            if w in self.known_colors:
                attrs["colors"].add(self.known_colors[w])
                
        # Dimensions (e.g., 50x70, 50*70, 50 في 70)
        dim_matches = re.findall(r"(\d+)\s*(?:[x*×]|في|\*)\s*(\d+)", norm_text)
        for m in dim_matches:
            d1, d2 = sorted([int(m[0]), int(m[1])])
            attrs["dimensions"].add((d1, d2))
            
        # Percentages (e.g. 5%, 12%)
        perc_matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", norm_text)
        for p in perc_matches:
            attrs["percentages"].add(float(p))
            
        # Volumes (e.g., 5 لتر, 500 مل, 4 لتر)
        vol_matches = re.findall(r"(\d+(?:\.\d+)?)\s*(لتر|مل|ل|ml|l)", norm_text)
        for val, unit in vol_matches:
            v = float(val)
            if unit in ["مل", "ml"]:
                v = v / 1000.0  # convert to liters
            attrs["volumes"].add(v)
            
        # Weights (e.g., 5 كيلو, 5 ك, 500 جرام, 500 جم)
        wt_matches = re.findall(r"(\d+(?:\.\d+)?)\s*(كيلو|ك|جرام|جم|كجم|kg|g)", norm_text)
        for val, unit in wt_matches:
            w = float(val)
            if unit in ["جرام", "جم", "g"]:
                w = w / 1000.0  # convert to kg
            attrs["weights"].add(w)
            
        # Pack counts (e.g., باكو 100 معلقة, 200 منديل, 50 كوب, 1000 كوب)
        count_matches = re.findall(r"(?:باكو|كرتونة|رول|باكيت|كيس|قطعة|علبة)?\s*(\d+)\s*(?:منديل|معلقة|كوب|علبة|قطعة|حبة|كيس|رول)", norm_text)
        for c in count_matches:
            attrs["pack_counts"].add(int(c))
            
        return attrs

    def check_contradictions(self, query_attrs, cand_attrs):
        """
        Returns (is_contradiction, reason_codes, applied_penalties)
        """
        reasons = []
        penalty = 0.0
        
        # 1. Brand contradiction
        if query_attrs["brands"] and cand_attrs["brands"]:
            if not query_attrs["brands"].intersection(cand_attrs["brands"]):
                reasons.append(f"explicit_brand_match_conflict: query={query_attrs['brands']} vs cand={cand_attrs['brands']}")
                penalty += 10.0
                
        # 2. Color contradiction
        if query_attrs["colors"] and cand_attrs["colors"]:
            if not query_attrs["colors"].intersection(cand_attrs["colors"]):
                reasons.append(f"explicit_color_conflict: query={query_attrs['colors']} vs cand={cand_attrs['colors']}")
                penalty += 10.0
                
        # 3. Dimensions contradiction
        if query_attrs["dimensions"] and cand_attrs["dimensions"]:
            if not query_attrs["dimensions"].intersection(cand_attrs["dimensions"]):
                reasons.append(f"explicit_dimension_match_conflict: query={query_attrs['dimensions']} vs cand={cand_attrs['dimensions']}")
                penalty += 10.0
                
        # 4. Volume contradiction
        if query_attrs["volumes"] and cand_attrs["volumes"]:
            if not query_attrs["volumes"].intersection(cand_attrs["volumes"]):
                reasons.append(f"explicit_volume_conflict: query={query_attrs['volumes']} vs cand={cand_attrs['volumes']}")
                penalty += 10.0
                
        # 5. Weight contradiction
        if query_attrs["weights"] and cand_attrs["weights"]:
            if not query_attrs["weights"].intersection(cand_attrs["weights"]):
                reasons.append(f"explicit_weight_conflict: query={query_attrs['weights']} vs cand={cand_attrs['weights']}")
                penalty += 10.0
                
        # 6. Percentage contradiction
        if query_attrs["percentages"] and cand_attrs["percentages"]:
            if not query_attrs["percentages"].intersection(cand_attrs["percentages"]):
                reasons.append(f"explicit_percentage_conflict: query={query_attrs['percentages']} vs cand={cand_attrs['percentages']}")
                penalty += 10.0
                
        # 7. Pack count contradiction
        if query_attrs["pack_counts"] and cand_attrs["pack_counts"]:
            if not query_attrs["pack_counts"].intersection(cand_attrs["pack_counts"]):
                reasons.append(f"explicit_pack_count_conflict: query={query_attrs['pack_counts']} vs cand={cand_attrs['pack_counts']}")
                penalty += 10.0
                
        is_contradiction = len(reasons) > 0
        return is_contradiction, reasons, penalty

    def score_candidate(self, query_text, candidate_rich_text, base_score):
        q_attrs = self.parse_attributes(query_text)
        c_attrs = self.parse_attributes(candidate_rich_text)
        is_contra, reasons, penalty = self.check_contradictions(q_attrs, c_attrs)
        
        final_score = base_score - penalty
        return {
            "final_score": final_score,
            "is_contradiction": is_contra,
            "reasons": reasons,
            "penalty_applied": penalty
        }

if __name__ == "__main__":
    detector = AttributeContradictionDetector()
    q = detector.parse_attributes("5 لتر كلور الفارس ابيض")
    c = detector.parse_attributes("كلور 20 لتر النيل احمر")
    print("Contradiction check:", detector.check_contradictions(q, c))
