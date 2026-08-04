"""Product matching engine using RapidFuzz for fuzzy string matching.

Scaling note
------------
The original implementation scored every catalog entry in a Python loop: four
fuzzy comparisons per name and per alias, for every line of every order. On the
50-product demo catalog that cost ~2 ms per line and was invisible. On a real
5,000-SKU distributor catalog it cost ~210 ms per line — a 20-line order spent
over four seconds of pure CPU before the operator saw anything.

Matching now runs in two stages. A vectorised RapidFuzz prefilter (C++, releases
the GIL) narrows thousands of names and aliases to a few hundred plausible
products; the original four-metric blend then scores only those. Typo tolerance
is preserved because the prefilter is itself fuzzy, not a token or prefix lookup.
"""

from rapidfuzz import fuzz, process

from .models import CatalogProduct, ExtractedItem, MatchCandidate, OrderLineResult
from .normalizer import normalize_arabic

# Advisory display thresholds only; neither threshold can approve an item.
CONFIDENCE_HIGH = 0.85
CONFIDENCE_MEDIUM = 0.60  # Low confidence - show alternatives
# Below CONFIDENCE_MEDIUM = not found

# Prefilter width. Generous on purpose: the cost of one extra scored product is
# microseconds, the cost of dropping the correct one is a wrong order.
PREFILTER_LIMIT = 400
PREFILTER_CUTOFF = 25.0
# Below this catalog size the prefilter costs more than it saves.
PREFILTER_MIN_CATALOG = 250

class ProductMatcher:
    """Match extracted product descriptions against a catalog using fuzzy matching."""

    def __init__(self, catalog: list[CatalogProduct]):
        self.catalog = catalog
        # Pre-normalize all product names and aliases
        self._normalized_names: list[list[tuple[str, str]]] = []
        for product in catalog:
            names = [(product.product_name, normalize_arabic(product.product_name))]
            for alias in product.aliases:
                names.append((alias, normalize_arabic(alias)))
            self._normalized_names.append(names)
        # O(1) product lookup; the previous linear rescan ran per matched line.
        self._by_id: dict[str, CatalogProduct] = {p.product_id: p for p in catalog}

        # Flattened corpus for the vectorised prefilter: every normalised name
        # and alias, with a parallel array pointing back at its product.
        self._corpus: list[str] = []
        self._corpus_owner: list[int] = []
        for index, names in enumerate(self._normalized_names):
            for _original, normalized in names:
                self._corpus.append(normalized)
                self._corpus_owner.append(index)
        self._use_prefilter = len(catalog) >= PREFILTER_MIN_CATALOG

    def get(self, product_id: str) -> CatalogProduct | None:
        """Return a catalog product by id, or ``None``."""
        return self._by_id.get(product_id)

    def _shortlist(self, normalized_query: str) -> list[int]:
        """Product indices worth scoring with the full metric blend.

        Returns every index for a small catalog; for a large one, runs a single
        vectorised RapidFuzz pass over the flattened name/alias corpus.
        """
        if not self._use_prefilter:
            return list(range(len(self.catalog)))

        hits = process.extract(
            normalized_query,
            self._corpus,
            scorer=fuzz.token_set_ratio,
            limit=PREFILTER_LIMIT,
            score_cutoff=PREFILTER_CUTOFF,
        )
        seen: dict[int, None] = {}
        for _text, _score, position in hits:
            seen.setdefault(self._corpus_owner[position], None)
        return list(seen)

    def _compute_score(self, query: str, target: str) -> float:
        """Compute similarity score between query and target using multiple methods.

        Returns a score between 0.0 and 1.0.
        """
        # Token set ratio: best for unordered partial matches
        token_set = fuzz.token_set_ratio(query, target)
        # Token sort ratio: good for same words different order
        token_sort = fuzz.token_sort_ratio(query, target)
        # Partial ratio: good for substring matching
        partial = fuzz.partial_ratio(query, target)
        # Standard ratio: penalizes length differences
        ratio = fuzz.ratio(query, target)

        # Weighted combination favoring token_set (best for product matching)
        score = (
            token_set * 0.35 +
            token_sort * 0.25 +
            partial * 0.25 +
            ratio * 0.15
        ) / 100.0

        return round(score, 4)

    def find_candidates(
        self,
        product_description: str,
        top_k: int = 5,
        min_score: float = 0.35,
    ) -> list[MatchCandidate]:
        """Find top-k matching products for a description.

        Args:
            product_description: The cleaned product description to match.
            top_k: Maximum number of candidates to return.
            min_score: Minimum score threshold for inclusion.

        Returns:
            List of MatchCandidate sorted by score descending.
        """
        normalized_query = normalize_arabic(product_description)
        candidates: list[MatchCandidate] = []

        for i in self._shortlist(normalized_query):
            product = self.catalog[i]
            best_score = 0.0
            for _original, normalized_name in self._normalized_names[i]:
                score = self._compute_score(normalized_query, normalized_name)
                best_score = max(best_score, score)

            if best_score >= min_score:
                candidates.append(MatchCandidate(
                    product_id=product.product_id,
                    product_name=product.product_name,
                    score=best_score,
                    unit=product.unit,
                    price=product.price,
                ))

        # Sort by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:top_k]

    def match_item(self, item: ExtractedItem) -> OrderLineResult:
        """Match a single extracted item against the catalog.

        Returns an OrderLineResult with confidence status:
        - advisory_select: high confidence advisory match (≥ 85%)
        - low_confidence: possible match but needs review (60-84%)
        - ambiguous: multiple similar candidates
        - not_found: no good match found (< 60%)
        """
        candidates = self.find_candidates(item.product_description)

        if not candidates:
            return OrderLineResult(
                raw_text=item.raw_text,
                extracted_product=item.product_description,
                extracted_quantity=item.quantity,
                extracted_unit=item.unit,
                confidence=0.0,
                status='not_found',
                reason='لم يتم العثور على منتج مطابق في الكتالوج',
                alternatives=[],
            )

        top = candidates[0]

        # Ambiguity: the runner-up is too close to call.
        #
        # A size-aware relaxation was tried here — treat candidates whose stated
        # size contradicts the order as non-competitors, so a dense catalog does
        # not flag every line. Measured on the 400-SKU close-variant catalog it
        # made 24% of lines batch-eligible, and 15 of those 46 picks were the
        # WRONG product: a 33% error rate on the one path where a human is not
        # looking at each line individually. That is precisely the silent
        # wrong-product failure this whole design exists to prevent, so the rule
        # is deliberately NOT relaxed. On a dense catalog every line is reviewed
        # one at a time; the leverage comes from the pre-filled shortlist, not
        # from batching.
        is_ambiguous = (
            len(candidates) >= 2
            and candidates[1].score >= CONFIDENCE_MEDIUM
            and (top.score - candidates[1].score) < 0.08
        )

        if is_ambiguous:
            # An ambiguous line still surfaces its best candidate when that
            # candidate scores highly. Suppressing it entirely added no safety
            # (a human confirms every line either way) and forced the reviewer
            # to search the whole catalog for a product the matcher had already
            # ranked first. The ambiguity is reported through `status` and
            # `reason` instead, so the UI can warn without hiding the answer.
            return OrderLineResult(
                raw_text=item.raw_text,
                extracted_product=item.product_description,
                extracted_quantity=item.quantity,
                extracted_unit=item.unit,
                matched_product=self.get(top.product_id) if top.score >= CONFIDENCE_HIGH else None,
                confidence=top.score,
                status='ambiguous',
                reason=(
                    f'يوجد أكثر من منتج محتمل: '
                    f'"{candidates[0].product_name}" ({candidates[0].score:.0%}) و '
                    f'"{candidates[1].product_name}" ({candidates[1].score:.0%}). '
                    f'يرجى التوضيح.'
                ),
                alternatives=candidates[:5],
            )

        if top.score >= CONFIDENCE_HIGH:
            matched = self.get(top.product_id)

            return OrderLineResult(
                raw_text=item.raw_text,
                extracted_product=item.product_description,
                extracted_quantity=item.quantity,
                extracted_unit=item.unit,
                matched_product=matched,
                confidence=top.score,
                status='advisory_select',
                alternatives=candidates[:5],
            )

        if top.score >= CONFIDENCE_MEDIUM:
            matched = self.get(top.product_id)

            return OrderLineResult(
                raw_text=item.raw_text,
                extracted_product=item.product_description,
                extracted_quantity=item.quantity,
                extracted_unit=item.unit,
                matched_product=matched,
                confidence=top.score,
                status='low_confidence',
                reason=(
                    f'الثقة منخفضة ({top.score:.0%}). '
                    f'أفضل تطابق: "{top.product_name}". '
                    f'يرجى التأكيد أو التوضيح.'
                ),
                alternatives=candidates[:5],
            )

        # Below medium threshold
        return OrderLineResult(
            raw_text=item.raw_text,
            extracted_product=item.product_description,
            extracted_quantity=item.quantity,
            extracted_unit=item.unit,
            confidence=top.score,
            status='not_found',
            reason=(
                f'لم يتم العثور على تطابق كافٍ. '
                f'أقرب منتج: "{top.product_name}" ({top.score:.0%})'
            ),
            alternatives=candidates[:5],
        )

    def match_items(self, items: list[ExtractedItem]) -> list[OrderLineResult]:
        """Match a list of extracted items against the catalog."""
        return [self.match_item(item) for item in items]
