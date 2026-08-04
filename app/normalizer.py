"""Arabic text normalization for Egyptian dialect order processing."""

import re

# Arabic diacritics (tashkeel) pattern
_DIACRITICS_RE = re.compile(
    r'[\u0610-\u061A\u064B-\u065F\u0670'
    r'\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED]'
)

# Alef variants → bare alef
_ALEF_MAP = {
    '\u0623': '\u0627',  # أ → ا
    '\u0625': '\u0627',  # إ → ا
    '\u0622': '\u0627',  # آ → ا
    '\u0671': '\u0627',  # ٱ → ا
}

# Arabic-Indic numerals → Western
_INDIC_DIGITS = {
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4',
    '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
}

# Written numbers in Egyptian Arabic (singular forms)
WRITTEN_NUMBERS: dict[str, float] = {
    # 1
    'واحد': 1, 'واحده': 1, 'واحدة': 1, 'واحدا': 1,
    # 2
    'اتنين': 2, 'اثنين': 2, 'اتنان': 2, 'اثنان': 2,
    # 3
    'تلاته': 3, 'تلاتة': 3, 'ثلاثة': 3, 'ثلاثه': 3, 'تلات': 3,
    # 4
    'اربعه': 4, 'اربعة': 4, 'اربع': 4, 'أربعة': 4, 'أربعه': 4,
    # 5
    'خمسه': 5, 'خمسة': 5, 'خمس': 5,
    # 6
    'سته': 6, 'ستة': 6, 'ست': 6,
    # 7
    'سبعه': 7, 'سبعة': 7, 'سبع': 7,
    # 8
    'تمنيه': 8, 'تمانية': 8, 'ثمانية': 8, 'تمانيه': 8, 'تمنية': 8, 'تمن': 8,
    # 9
    'تسعه': 9, 'تسعة': 9, 'تسع': 9,
    # 10
    'عشره': 10, 'عشرة': 10, 'عشر': 10,
    # 11-19 (Egyptian style)
    'حداشر': 11, 'احداشر': 11,
    'اتناشر': 12, 'اثناعشر': 12, 'اطناشر': 12,
    'تلتاشر': 13, 'ثلاثة عشر': 13, 'تلاتاشر': 13,
    'اربعتاشر': 14, 'أربعة عشر': 14,
    'خمستاشر': 15, 'خمسة عشر': 15,
    'ستاشر': 16, 'ستة عشر': 16,
    'سبعتاشر': 17, 'سبعة عشر': 17,
    'تمنتاشر': 18, 'ثمانية عشر': 18,
    'تسعتاشر': 19, 'تسعة عشر': 19,
    # 20+
    'عشرين': 20, 'ثلاثين': 30, 'تلاتين': 30,
    'اربعين': 40, 'خمسين': 50,
    # Fractions
    'نص': 0.5, 'نصف': 0.5, 'ربع': 0.25,
}

# Unit normalization map (various forms → canonical form)
UNIT_ALIASES: dict[str, str] = {
    # كرتونة
    'كرتونه': 'كرتونة', 'كرتونة': 'كرتونة', 'كرتون': 'كرتونة',
    'كراتين': 'كرتونة', 'كرتونتين': 'كرتونة',
    # رول
    'رول': 'رول', 'لفه': 'رول', 'لفة': 'رول', 'لف': 'رول', 'لفف': 'رول',
    # باكو
    'باكو': 'باكو', 'باكيت': 'باكو', 'علبه': 'باكو', 'علبة': 'باكو',
    # قطعة
    'قطعه': 'قطعة', 'قطعة': 'قطعة', 'حته': 'قطعة', 'حتة': 'قطعة', 'حبه': 'قطعة', 'حبة': 'قطعة',
    # دستة (12 units)
    'دسته': 'دستة', 'دستة': 'دستة',
    # جركن
    'جركن': 'جركن', 'جركنه': 'جركن', 'جركنة': 'جركن',
    'جراكن': 'جركن', 'جالون': 'جركن',
    # كيس
    'كيس': 'كيس', 'اكياس': 'كيس', 'شنطه': 'كيس', 'شنطة': 'كيس',
}


def remove_diacritics(text: str) -> str:
    """Remove Arabic diacritical marks (tashkeel)."""
    return _DIACRITICS_RE.sub('', text)


def normalize_alef(text: str) -> str:
    """Normalize alef variants (أ إ آ ٱ) to bare alef (ا)."""
    for src, dst in _ALEF_MAP.items():
        text = text.replace(src, dst)
    return text


def convert_indic_numerals(text: str) -> str:
    """Convert Arabic-Indic numerals (٠-٩) to Western (0-9)."""
    for src, dst in _INDIC_DIGITS.items():
        text = text.replace(src, dst)
    return text


def normalize_dimensions(text: str) -> str:
    """Normalize dimension separators: '50 في 70' → '50×70', '50x70' → '50×70'."""
    # "50 في 70" or "50في70"
    text = re.sub(r'(\d+)\s*في\s*(\d+)', r'\1×\2', text)
    # "50x70" or "50X70" (Latin x)
    text = re.sub(r'(\d+)\s*[xX]\s*(\d+)', r'\1×\2', text)
    # "50*70"
    text = re.sub(r'(\d+)\s*\*\s*(\d+)', r'\1×\2', text)
    return text


def written_number_to_digit(word: str) -> float | None:
    """Convert a written Arabic number word to its numeric value, or None."""
    normalized = normalize_alef(remove_diacritics(word.strip()))
    return WRITTEN_NUMBERS.get(normalized)


def normalize_unit(word: str) -> str | None:
    """Normalize a unit word to its canonical form, or None if not a unit."""
    normalized = normalize_alef(remove_diacritics(word.strip()))
    return UNIT_ALIASES.get(normalized)


def normalize_arabic(text: str) -> str:
    """Full Arabic text normalization pipeline.

    Steps:
    1. Remove diacritics
    2. Normalize alef variants
    3. Convert Arabic-Indic numerals
    4. Normalize dimensions (50 في 70 → 50×70)
    5. Normalize whitespace
    """
    text = remove_diacritics(text)
    text = normalize_alef(text)
    text = convert_indic_numerals(text)
    text = normalize_dimensions(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_numbers_from_text(text: str) -> list[tuple[str, float]]:
    """Extract all numbers (digit or written) from text with their original form.

    Returns list of (original_text, numeric_value) tuples.
    """
    results = []

    # First pass: find digit-based numbers
    for m in re.finditer(r'\d+(?:\.\d+)?', text):
        results.append((m.group(), float(m.group())))

    # Second pass: find written numbers
    words = text.split()
    for i, word in enumerate(words):
        # Handle "نص دستة" = 6
        if word in ('نص', 'نصف') and i + 1 < len(words):
            next_word = normalize_alef(remove_diacritics(words[i + 1]))
            if next_word in ('دسته', 'دستة', 'دستتين'):
                results.append((f'{word} {words[i + 1]}', 6.0))
                continue

        val = written_number_to_digit(word)
        if val is not None:
            results.append((word, val))

    return results
