"""Unit tests for product matching engine."""

from app.matcher import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM
from app.models import ExtractedItem


class TestFindCandidates:
    """Test candidate retrieval from catalog."""

    def test_exact_product_name(self, matcher):
        """Test matching with exact product name."""
        candidates = matcher.find_candidates('أكياس سودا شفافة 50×70')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK001'
        assert candidates[0].score >= CONFIDENCE_HIGH

    def test_alias_match(self, matcher):
        """Test matching via product alias."""
        candidates = matcher.find_candidates('شنط سودا كبيره')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK002'

    def test_colloquial_name_bril(self, matcher):
        """Test matching colloquial name: بريل كبير."""
        candidates = matcher.find_candidates('بريل كبير')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL011'
        assert candidates[0].score >= CONFIDENCE_MEDIUM

    def test_brand_alias_clorox(self, matcher):
        """Test brand name alias: كلوركس مركز → كلور مركز."""
        candidates = matcher.find_candidates('كلوركس مركز')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL004'
        assert candidates[0].score >= CONFIDENCE_HIGH

    def test_soda_bags_with_dimensions(self, matcher):
        """Test matching soda bags with dimensions."""
        candidates = matcher.find_candidates('اكياس سودا 50 في 70')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK001'

    def test_stretch_roll(self, matcher):
        """Test matching: استرتش كبير."""
        candidates = matcher.find_candidates('استرتش كبير')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK004'

    def test_scotch_tape(self, matcher):
        """Test Egyptian slang: سلوتيب عريض شفاف."""
        candidates = matcher.find_candidates('سلوتيب عريض شفاف')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK006'

    def test_foil_big(self, matcher):
        """Test: فويل كبير → PK009."""
        candidates = matcher.find_candidates('فويل كبير')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK009'

    def test_foil_small(self, matcher):
        """Test: فويل صغير → PK010."""
        candidates = matcher.find_candidates('فويل صغير')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK010'

    def test_discriminate_similar_products(self, matcher):
        """Test that similar products with size differences are distinguished."""
        big = matcher.find_candidates('فويل كبير')
        small = matcher.find_candidates('فويل صغير')
        assert big[0].product_id != small[0].product_id
        assert big[0].product_id == 'PK009'
        assert small[0].product_id == 'PK010'

    def test_flash_green(self, matcher):
        """Test: فلاش اخضر → CL009 (صنوبر)."""
        candidates = matcher.find_candidates('فلاش اخضر')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL009'

    def test_flash_purple(self, matcher):
        """Test: فلاش موف → CL008 (لافندر)."""
        candidates = matcher.find_candidates('فلاش موف')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL008'

    def test_garbage_bags_big(self, matcher):
        """Test: اكياس زباله كبيره → CL019."""
        candidates = matcher.find_candidates('اكياس زباله كبيره')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL019'

    def test_sponge(self, matcher):
        """Test: اسفنج مواعين or سفنجه → CL016."""
        candidates = matcher.find_candidates('سفنجة مواعين')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'CL016'

    def test_foam_cups_small(self, matcher):
        """Test: كوبايات فوم صغيره → PK016."""
        candidates = matcher.find_candidates('كوبايات فوم صغيره')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK016'

    def test_foam_cups_big(self, matcher):
        """Test: كوبايات فوم كبيره → PK017."""
        candidates = matcher.find_candidates('كوبايات فوم كبيره')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK017'

    def test_no_match_nonexistent(self, matcher):
        """Test that a non-existent product returns low scores."""
        candidates = matcher.find_candidates('شفاط كهربائي')
        # Either no candidates or all below medium threshold
        if candidates:
            assert candidates[0].score < CONFIDENCE_MEDIUM

    def test_spelling_error_dettol(self, matcher):
        """Test common spelling error: ديتو instead of ديتول."""
        candidates = matcher.find_candidates('ديتو كبير')
        assert len(candidates) > 0
        # Should match one of the Dettol products
        assert candidates[0].product_id in ('CL006', 'CL007')

    def test_sandwich_bags(self, matcher):
        """Test: اكياس سندوتش → PK022."""
        candidates = matcher.find_candidates('اكياس سندوتش')
        assert len(candidates) > 0
        assert candidates[0].product_id == 'PK022'


class TestMatchItem:
    """Test the full match_item pipeline."""

    def test_confirmed_match(self, matcher):
        """High confidence match remains an advisory selection."""
        item = ExtractedItem(
            raw_text='3 كراتين اكياس سودا 50 في 70',
            product_description='اكياس سودا 50×70',
            quantity=3,
            unit='كرتونة',
        )
        result = matcher.match_item(item)
        assert result.status == 'advisory_select'
        assert result.matched_product is not None
        assert result.matched_product.product_id == 'PK001'
        assert result.confidence >= CONFIDENCE_HIGH

    def test_not_found(self, matcher):
        """Non-existent product should return 'not_found' status."""
        item = ExtractedItem(
            raw_text='5 كيلو سكر',
            product_description='سكر ابيض',
            quantity=5,
            unit='كيلو',
        )
        result = matcher.match_item(item)
        assert result.status in ('not_found', 'low_confidence')
        assert result.confidence < CONFIDENCE_HIGH

    def test_low_confidence_includes_reason(self, matcher):
        """Low confidence results should include a reason."""
        item = ExtractedItem(
            raw_text='بلاستيك',
            product_description='بلاستيك',
            quantity=1,
            unit='قطعة',
        )
        result = matcher.match_item(item)
        if result.status in ('low_confidence', 'ambiguous', 'not_found'):
            assert result.reason is not None
            assert len(result.reason) > 0

    def test_quantity_preserved(self, matcher):
        """Extracted quantity should be preserved in result."""
        item = ExtractedItem(
            raw_text='7 بريل كبير',
            product_description='بريل كبير',
            quantity=7,
            unit='قطعة',
        )
        result = matcher.match_item(item)
        assert result.extracted_quantity == 7
        assert result.extracted_unit == 'قطعة'

    def test_unit_preserved(self, matcher):
        """Extracted unit should be preserved in result."""
        item = ExtractedItem(
            raw_text='3 جركن كلور مركز',
            product_description='كلور مركز',
            quantity=3,
            unit='جركن',
        )
        result = matcher.match_item(item)
        assert result.extracted_unit == 'جركن'
        assert result.status == 'advisory_select'
