import re

class AttributeParser:
    def __init__(self):
        # Maps canonical attribute name to regex patterns.
        # Format: (Regex pattern, canonical type mapping if any, confidence)
        self.patterns = {
            'dimensions': [
                (r'(\d+)\s*(?:x|\*|في)\s*(\d+)\s*(?:سم|cm)?', None, 1.0)
            ],
            'volume': [
                (r'(\d+(?:\.\d+)?)\s*(لتر|l|liter)', 'L', 1.0),
                (r'(\d+(?:\.\d+)?)\s*(مل|ml|milliliter)', 'ml', 1.0)
            ],
            'weight': [
                (r'(\d+(?:\.\d+)?)\s*(ك|كيلو|kg|kilo)', 'kg', 1.0),
                (r'(\d+(?:\.\d+)?)\s*(جم|جرام|g|gram)', 'g', 1.0)
            ],
            'concentration': [
                (r'(\d+(?:\.\d+)?)\s*%', '%', 1.0)
            ],
            'color': [
                (r'\b(ابيض|أبيض|white)\b', 'white', 0.9),
                (r'\b(احمر|أحمر|red)\b', 'red', 0.9),
                (r'\b(اسود|أسود|black)\b', 'black', 0.9),
                (r'\b(ازرق|أزرق|blue)\b', 'blue', 0.9),
                (r'\b(شفاف|transparent)\b', 'transparent', 0.9),
                (r'\b(ملون|colored)\b', 'colored', 0.8)
            ],
            'package_count': [
                (r'(\d+)\s*(?:معلقة|كوب|علبة|رول|حبة|قطعة)', 'count', 0.8),
                (r'(?:باكو|كرتونة|كيس|دسته)\s*(\d+)', 'count', 0.8)
            ]
        }

    def parse(self, text):
        """
        Parses attributes from text.
        Returns a dictionary: { attribute_type: { 'value': str, 'span': (int, int), 'confidence': float } }
        """
        text = text.lower()
        extracted = {}

        for attr_type, rules in self.patterns.items():
            for rule, unit_mapping, confidence in rules:
                for match in re.finditer(rule, text):
                    # Combine groups for the value if multiple
                    if len(match.groups()) > 1 and attr_type == 'dimensions':
                        # Sort dimensions to avoid 50x70 vs 70x50 conflict
                        dims = sorted([int(match.group(1)), int(match.group(2))])
                        val = f"{dims[0]}x{dims[1]}"
                    elif len(match.groups()) > 1 and unit_mapping:
                        val = f"{match.group(1)}{unit_mapping}"
                    elif len(match.groups()) == 1 and unit_mapping:
                        if unit_mapping == 'count':
                            val = str(match.group(1))
                        else:
                            val = f"{match.group(1)}{unit_mapping}"
                    else:
                        val = str(match.group(1)) if match.groups() else match.group(0)

                    if attr_type not in extracted or confidence > extracted[attr_type]['confidence']:
                        extracted[attr_type] = {
                            'value': val,
                            'span': match.span(),
                            'confidence': confidence
                        }
        return extracted

    def get_penalty(self, query_attrs, doc_attrs):
        """
        Calculates penalty for attribute mismatches.
        Returns (is_hard_contradiction: bool, penalty_score: float)
        """
        penalty = 0.0
        hard_contradiction = False

        for attr_type, q_attr in query_attrs.items():
            if attr_type in doc_attrs:
                d_attr = doc_attrs[attr_type]
                
                if q_attr['value'] != d_attr['value']:
                    if q_attr['confidence'] >= 0.9 and d_attr['confidence'] >= 0.9:
                        hard_contradiction = True
                    else:
                        penalty += 0.2 # soft penalty
            # If attribute is missing in doc, we don't penalize.
            # "Never hard-filter candidates because an attribute is absent."

        return hard_contradiction, penalty

def test_parser():
    parser = AttributeParser()
    q = "5 لتر كلور"
    d1 = "كلور 20 لتر"
    d2 = "كلور 5l"
    
    q_attrs = parser.parse(q)
    d1_attrs = parser.parse(d1)
    d2_attrs = parser.parse(d2)
    
    assert parser.get_penalty(q_attrs, d1_attrs)[0] == True # Contradiction
    assert parser.get_penalty(q_attrs, d2_attrs)[0] == False # Match
    
    q = "اكياس 50 في 70 سم"
    d1 = "اكياس 70x50"
    d2 = "اكياس 70x90"
    
    q_attrs = parser.parse(q)
    assert q_attrs['dimensions']['value'] == '50x70'
    assert parser.parse(d1)['dimensions']['value'] == '50x70'
    assert parser.get_penalty(q_attrs, parser.parse(d2))[0] == True

if __name__ == "__main__":
    test_parser()
    print("Attribute parser tests passed.")
