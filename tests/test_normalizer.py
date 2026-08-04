"""Unit tests for Arabic text normalization."""

import pytest

from app.normalizer import (
    convert_indic_numerals,
    normalize_alef,
    normalize_arabic,
    normalize_dimensions,
    normalize_unit,
    remove_diacritics,
    written_number_to_digit,
)


class TestRemoveDiacritics:
    def test_fatha_kasra_damma(self):
        assert remove_diacritics('مُنْتَج') == 'منتج'

    def test_shadda_tanween(self):
        assert remove_diacritics('مُنَظَّف') == 'منظف'

    def test_no_diacritics(self):
        text = 'اكياس سودا'
        assert remove_diacritics(text) == text


class TestNormalizeAlef:
    def test_hamza_above(self):
        assert normalize_alef('أكياس') == 'اكياس'

    def test_hamza_below(self):
        assert normalize_alef('إستلام') == 'استلام'

    def test_madda(self):
        assert normalize_alef('آخر') == 'اخر'

    def test_mixed(self):
        assert normalize_alef('أكياس إستلام آخر') == 'اكياس استلام اخر'


class TestConvertIndicNumerals:
    def test_single_digit(self):
        assert convert_indic_numerals('٣') == '3'

    def test_multi_digit(self):
        assert convert_indic_numerals('١٢٣') == '123'

    def test_mixed_text(self):
        assert convert_indic_numerals('عايز ٣ كراتين') == 'عايز 3 كراتين'

    def test_no_indic(self):
        assert convert_indic_numerals('عايز 3 كراتين') == 'عايز 3 كراتين'


class TestNormalizeDimensions:
    def test_arabic_in(self):
        assert normalize_dimensions('50 في 70') == '50×70'

    def test_arabic_in_no_space(self):
        assert normalize_dimensions('50في70') == '50×70'

    def test_latin_x(self):
        assert normalize_dimensions('50x70') == '50×70'

    def test_asterisk(self):
        assert normalize_dimensions('50*70') == '50×70'


class TestWrittenNumberToDigit:
    @pytest.mark.parametrize("word,expected", [
        ('واحد', 1), ('اتنين', 2), ('تلاته', 3), ('تلاتة', 3),
        ('اربعه', 4), ('خمسه', 5), ('سته', 6), ('سبعه', 7),
        ('تمانية', 8), ('تسعه', 9), ('عشره', 10),
        ('حداشر', 11), ('اتناشر', 12), ('عشرين', 20),
        ('نص', 0.5), ('ربع', 0.25),
    ])
    def test_valid_numbers(self, word, expected):
        assert written_number_to_digit(word) == expected

    def test_unknown_word(self):
        assert written_number_to_digit('بريل') is None

    def test_egyptian_variants(self):
        # ثلاثة (formal) should also work
        assert written_number_to_digit('ثلاثة') == 3
        assert written_number_to_digit('ثمانية') == 8


class TestNormalizeUnit:
    @pytest.mark.parametrize("word,expected", [
        ('كرتونه', 'كرتونة'), ('كراتين', 'كرتونة'), ('كرتون', 'كرتونة'),
        ('رول', 'رول'), ('لفه', 'رول'), ('لفة', 'رول'),
        ('باكو', 'باكو'), ('باكيت', 'باكو'),
        ('قطعه', 'قطعة'), ('حته', 'قطعة'), ('حبه', 'قطعة'),
        ('دسته', 'دستة'), ('دستة', 'دستة'),
        ('جركن', 'جركن'), ('جراكن', 'جركن'), ('جالون', 'جركن'),
    ])
    def test_unit_normalization(self, word, expected):
        assert normalize_unit(word) == expected

    def test_unknown_unit(self):
        assert normalize_unit('منتج') is None


class TestNormalizeArabic:
    def test_full_pipeline(self):
        text = 'عايز ٣ كراتين أكياس سودا 50 في 70'
        result = normalize_arabic(text)
        assert '3' in result
        assert '50×70' in result
        assert 'اكياس' in result  # أ → ا

    def test_whitespace_normalization(self):
        text = '  عايز    بريل   كبير  '
        result = normalize_arabic(text)
        assert result == 'عايز بريل كبير'

    def test_preserves_dimensions(self):
        text = 'اكياس سودا 70 في 100'
        result = normalize_arabic(text)
        assert '70×100' in result
