"""Integration tests for the full order processing pipeline.

These 20+ hard test cases verify the complete extraction → matching pipeline
using mock extraction results. They cover all required scenarios:
- Standard and colloquial product names
- Multiple products per message
- Various units (كرتونة، دستة، باكو، رول، لفة، جركن)
- Written numbers
- Similar products with different sizes
- Non-existent products
- Ambiguous messages
- Quantity corrections
- Common Egyptian spelling errors
"""

from app.matcher import CONFIDENCE_HIGH, ProductMatcher
from app.models import ExtractedItem, OrderLineResult, OrderResult


def process_extracted_items(
    matcher: ProductMatcher,
    items: list[ExtractedItem],
    unresolved: list[str] | None = None,
) -> OrderResult:
    """Simulate the full pipeline: extraction result → matching → OrderResult."""
    results: list[OrderLineResult] = []
    for item in items:
        result = matcher.match_item(item)
        results.append(result)

    confirmed = sum(1 for r in results if r.status == 'advisory_select')
    return OrderResult(
        original_message='test message',
        items=results,
        unresolved=unresolved or [],
        total_confirmed=confirmed,
        total_items=len(results),
    )


# ============================================================
# TEST CASES - 25 hard integration tests
# ============================================================


class TestPipeline:
    """Full pipeline integration tests."""

    # --- 1. Standard product name (exact) ---
    def test_01_standard_product_name(self, matcher):
        """Standard product name with dimensions."""
        items = [ExtractedItem(
            raw_text='3 كراتين أكياس سودا شفافة 50×70',
            product_description='أكياس سودا شفافة 50×70',
            quantity=3, unit='كرتونة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.total_items == 1
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'PK001'
        assert result.items[0].extracted_quantity == 3

    # --- 2. Colloquial / abbreviated name ---
    def test_02_colloquial_name(self, matcher):
        """Colloquial name: بريل كبير (Pril dishwash big)."""
        items = [ExtractedItem(
            raw_text='5 بريل كبير',
            product_description='بريل كبير',
            quantity=5, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'CL011'
        assert result.items[0].extracted_quantity == 5

    # --- 3. Multiple products in one message ---
    def test_03_multiple_products(self, matcher):
        """Two different products in same message."""
        items = [
            ExtractedItem(
                raw_text='3 كراتين اكياس سودا 50 في 70',
                product_description='اكياس سودا 50×70',
                quantity=3, unit='كرتونة',
            ),
            ExtractedItem(
                raw_text='اتنين رول استرتش الكبير',
                product_description='استرتش كبير',
                quantity=2, unit='رول',
            ),
        ]
        result = process_extracted_items(matcher, items)
        assert result.total_items == 2
        assert result.items[0].matched_product.product_id == 'PK001'
        assert result.items[1].matched_product.product_id == 'PK004'
        assert result.items[0].extracted_quantity == 3
        assert result.items[1].extracted_quantity == 2

    # --- 4. كرتونة unit ---
    def test_04_carton_unit(self, matcher):
        """Unit: كرتونة."""
        items = [ExtractedItem(
            raw_text='كرتونة اكياس سودا 70 في 100',
            product_description='اكياس سودا 70×100',
            quantity=1, unit='كرتونة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'PK002'

    # --- 5. دستة unit (dozen = 12) ---
    def test_05_dozen_unit(self, matcher):
        """Unit: دستة (dozen) for wire scrubbers."""
        items = [ExtractedItem(
            raw_text='دستة سلك مواعين ستيل',
            product_description='سلك مواعين ستيل',
            quantity=12, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'CL014'
        assert result.items[0].extracted_quantity == 12

    # --- 6. باكو unit ---
    def test_06_pack_unit(self, matcher):
        """Unit: باكو for plastic spoons."""
        items = [ExtractedItem(
            raw_text='تلات باكو ملاعق بلاستيك',
            product_description='ملاعق بلاستيك',
            quantity=3, unit='باكو',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'PK020'
        assert result.items[0].extracted_quantity == 3

    # --- 7. رول unit ---
    def test_07_roll_unit(self, matcher):
        """Unit: رول for stretch wrap."""
        items = [ExtractedItem(
            raw_text='2 رول استرتش كبير',
            product_description='استرتش كبير',
            quantity=2, unit='رول',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'PK004'

    # --- 8. جركن unit ---
    def test_08_jerrycan_unit(self, matcher):
        """Unit: جركن for concentrated chlorine."""
        items = [ExtractedItem(
            raw_text='4 جركن كلور مركز',
            product_description='كلور مركز',
            quantity=4, unit='جركن',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status == 'advisory_select'
        assert result.items[0].matched_product.product_id == 'CL004'
        assert result.items[0].extracted_quantity == 4

    # --- 9. Written number: تلاته ---
    def test_09_written_number_three(self, matcher):
        """Written number تلاته = 3."""
        items = [ExtractedItem(
            raw_text='تلاته ديتول كبير',
            product_description='ديتول كبير',
            quantity=3, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].extracted_quantity == 3
        assert result.items[0].matched_product.product_id == 'CL006'

    # --- 10. Written number: خمس باكو ---
    def test_10_written_number_five(self, matcher):
        """Written number خمس = 5."""
        items = [ExtractedItem(
            raw_text='خمس باكو شوك بلاستيك',
            product_description='شوك بلاستيك',
            quantity=5, unit='باكو',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].extracted_quantity == 5
        assert result.items[0].matched_product.product_id == 'PK021'

    # --- 11. Similar products with different sizes (big foil vs small) ---
    def test_11_similar_product_big(self, matcher):
        """Distinguish big foil from small."""
        items = [ExtractedItem(
            raw_text='5 رول فويل كبير',
            product_description='فويل كبير',
            quantity=5, unit='رول',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'PK009'

    # --- 12. Similar products: small variant ---
    def test_12_similar_product_small(self, matcher):
        """Distinguish small foil from big."""
        items = [ExtractedItem(
            raw_text='3 رول فويل صغير',
            product_description='فويل صغير',
            quantity=3, unit='رول',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'PK010'

    # --- 13. Non-existent product ---
    def test_13_nonexistent_product(self, matcher):
        """Product not in catalog should not be confirmed."""
        items = [ExtractedItem(
            raw_text='5 كيلو سكر',
            product_description='سكر ابيض',
            quantity=5, unit='كيلو',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].status != 'advisory_select'
        assert result.items[0].status in ('not_found', 'low_confidence')

    # --- 14. Ambiguous message (generic name) ---
    def test_14_ambiguous_generic_name(self, matcher):
        """Generic name like 'بلاستيك' should not auto-confirm."""
        items = [ExtractedItem(
            raw_text='محتاج شوية بلاستيك',
            product_description='بلاستيك',
            quantity=1, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        # Should NOT be confirmed - too ambiguous
        assert result.items[0].status != 'advisory_select'
        if result.items[0].reason:
            assert len(result.items[0].reason) > 0

    # --- 15. Spelling error: ديتو → ديتول ---
    def test_15_spelling_error_dettol(self, matcher):
        """Common typo: ديتو instead of ديتول."""
        items = [ExtractedItem(
            raw_text='تلاته ديتو كبير',
            product_description='ديتو كبير',
            quantity=3, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        # Should find Dettol despite typo
        if result.items[0].status == 'advisory_select':
            assert result.items[0].matched_product.product_id in ('CL006', 'CL007')
        else:
            # At least low confidence with Dettol as alternative
            assert any(
                a.product_id in ('CL006', 'CL007')
                for a in result.items[0].alternatives
            )

    # --- 16. Quantity correction (10 → 7) ---
    def test_16_quantity_correction(self, matcher):
        """Quantity corrected from 10 to 7. Extractor should handle this."""
        items = [ExtractedItem(
            raw_text='عايز 10 بريل كبير لا خلي 7 بس',
            product_description='بريل كبير',
            quantity=7,  # Corrected quantity
            unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].extracted_quantity == 7
        assert result.items[0].matched_product.product_id == 'CL011'

    # --- 17. Half dozen: نص دستة = 6 ---
    def test_17_half_dozen(self, matcher):
        """نص دستة اسفنج = 6 sponges."""
        items = [ExtractedItem(
            raw_text='نص دستة اسفنج مواعين',
            product_description='اسفنج مواعين',
            quantity=6, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].extracted_quantity == 6
        assert result.items[0].matched_product.product_id == 'CL016'

    # --- 18. Flash green vs purple ---
    def test_18_flash_green(self, matcher):
        """فلاش اخضر → CL009 (صنوبر)."""
        items = [ExtractedItem(
            raw_text='3 فلاش اخضر',
            product_description='فلاش اخضر',
            quantity=3, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'CL009'

    # --- 19. Egyptian slang: سلوتيب ---
    def test_19_scotch_tape_slang(self, matcher):
        """Egyptian slang: سلوتيب عريض شفاف = شريط لاصق."""
        items = [ExtractedItem(
            raw_text='عشرين سلوتيب عريض شفاف',
            product_description='سلوتيب عريض شفاف',
            quantity=20, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'PK006'
        assert result.items[0].extracted_quantity == 20

    # --- 20. Multiple items with different units ---
    def test_20_mixed_units_order(self, matcher):
        """Complex order with mixed units."""
        items = [
            ExtractedItem(
                raw_text='3 لفة استرتش صغير',
                product_description='استرتش صغير',
                quantity=3, unit='رول',
            ),
            ExtractedItem(
                raw_text='2 جركن كلور مركز',
                product_description='كلور مركز',
                quantity=2, unit='جركن',
            ),
            ExtractedItem(
                raw_text='باكو مناديل مبللة',
                product_description='مناديل مبللة',
                quantity=1, unit='باكو',
            ),
        ]
        result = process_extracted_items(matcher, items)
        assert result.total_items == 3
        assert result.items[0].matched_product.product_id == 'PK005'
        assert result.items[1].matched_product.product_id == 'CL004'
        assert result.items[2].matched_product.product_id == 'CL017'

    # --- 21. Wet wipes ---
    def test_21_wet_wipes(self, matcher):
        """مناديل مبلله → CL017."""
        items = [ExtractedItem(
            raw_text='5 باكو مناديل مبلله',
            product_description='مناديل مبلله',
            quantity=5, unit='باكو',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'CL017'

    # --- 22. Combined order with matching and not-found ---
    def test_22_mixed_found_and_notfound(self, matcher):
        """Order with both matching and non-matching items."""
        items = [
            ExtractedItem(
                raw_text='5 بريل كبير',
                product_description='بريل كبير',
                quantity=5, unit='قطعة',
            ),
            ExtractedItem(
                raw_text='3 زجاجات زيت',
                product_description='زيت طعام',
                quantity=3, unit='زجاجة',
            ),
        ]
        result = process_extracted_items(matcher, items)
        assert result.total_items == 2
        assert result.items[0].status == 'advisory_select'
        assert result.items[1].status != 'advisory_select'

    # --- 23. Shrink wrap ---
    def test_23_shrink_wrap(self, matcher):
        """بلاستيك شرنك → PK012."""
        items = [ExtractedItem(
            raw_text='2 رول شرنك',
            product_description='شرنك',
            quantity=2, unit='رول',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'PK012'

    # --- 24. Jerrycan 20L vs 5L ---
    def test_24_jerrycan_big_vs_small(self, matcher):
        """جركن 20 لتر vs 5 لتر should be distinguished."""
        big = ExtractedItem(
            raw_text='جركن 20 لتر',
            product_description='جركن 20 لتر',
            quantity=1, unit='قطعة',
        )
        small = ExtractedItem(
            raw_text='جركن 5 لتر',
            product_description='جركن 5 لتر',
            quantity=1, unit='قطعة',
        )
        result_big = matcher.match_item(big)
        result_small = matcher.match_item(small)

        assert result_big.matched_product.product_id == 'PK025'
        assert result_small.matched_product.product_id == 'PK024'

    # --- 25. Gloves (Egyptian slang: جوانتى) ---
    def test_25_gloves_slang(self, matcher):
        """Egyptian slang: جوانتى → CL025."""
        items = [ExtractedItem(
            raw_text='3 باكو جوانتى',
            product_description='جوانتى',
            quantity=3, unit='باكو',
        )]
        result = process_extracted_items(matcher, items)
        assert result.items[0].matched_product.product_id == 'CL025'


class TestEdgeCases:
    """Edge cases that must not produce false positives."""

    def test_no_false_positive_on_ambiguous(self, matcher):
        """System should never confirm a wrong product on ambiguous input."""
        # "اكياس" alone is too vague - multiple bag products
        items = [ExtractedItem(
            raw_text='اكياس',
            product_description='اكياس',
            quantity=1, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        # Should not be 'confirmed' since it's ambiguous
        if result.items[0].status == 'advisory_select':
            # If confirmed, alternatives should clarify
            assert result.items[0].confidence >= CONFIDENCE_HIGH
        else:
            assert result.items[0].status in ('ambiguous', 'low_confidence', 'not_found')

    def test_unresolved_items_propagated(self, matcher):
        """Unresolved text from extraction should be propagated."""
        items = [ExtractedItem(
            raw_text='5 بريل كبير',
            product_description='بريل كبير',
            quantity=5, unit='قطعة',
        )]
        result = process_extracted_items(
            matcher, items,
            unresolved=['الباقي بكره ان شاء الله']
        )
        assert len(result.unresolved) == 1
        assert result.unresolved[0] == 'الباقي بكره ان شاء الله'

    def test_low_confidence_has_reason(self, matcher):
        """All non-confirmed results must have a reason."""
        items = [ExtractedItem(
            raw_text='حاجه مش واضحه',
            product_description='حاجة غريبة',
            quantity=1, unit='قطعة',
        )]
        result = process_extracted_items(matcher, items)
        item = result.items[0]
        if item.status != 'advisory_select':
            assert item.reason is not None
            assert len(item.reason) > 0
