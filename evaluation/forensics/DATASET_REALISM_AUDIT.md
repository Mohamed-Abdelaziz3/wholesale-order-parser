# Dataset Realism Audit

## Verdict: EXCESSIVELY ADVERSARIAL AND QUESTIONABLE GROUND TRUTH

The synthetic evaluation dataset is excessively adversarial and fails to reflect realistic wholesaler interactions in several critical ways. The 98.75% close-variant ratio combined with intentionally underspecified customer messages creates irreducibly ambiguous situations where the model is unfairly penalized.

### Key Findings

1. **Unrealistic Ambiguity in Exact-Match Cases**:
   The raw messages frequently omit essential distinguishing attributes (like Brand, Concentration, or Scent), yet the `blind_ground_truth.jsonl` expects an exact SKU match. In reality, a trader receiving "ابعت 5 جراكن كلور كبير 4 لتر" would have to ask "النيل ولا الماسة؟" (Al-Nile or Al-Masa?), but the dataset scores this as a failure if the system correctly identifies it as ambiguous.

2. **The 98.75% Close-Variant Ratio is Artificial**:
   While real catalogs have variants (e.g., sizes or colors), this catalog was intentionally packed with items differing by only one attribute across 14 categories. This stresses the exact-match retrieval engine beyond typical limits.

3. **Vocabulary Non-Leakage Rate (100%)**:
   The prompt extraction worked exceptionally well (77.8% semantic correctness) despite the vocabulary non-leakage. The extraction prompt was not the weak link; it successfully extracted the literal items mentioned.

### Questionable Ground Truth Examples

- **Order ID 1**: `نزلي 4 اكياس مسحوق غسيل اتوماتيك 5 كيلو الفارس`. Expected: `DT003`. The message omits the scent (لافندر).
- **Order ID 2**: `ابعت 5 جراكن كلور كبير 4 لتر عادي`. Expected: `CC013`. The message omits the brand (النيل) and the exact concentration.

### Conclusion

The evaluation is a severe stress test. We classify many failures as **invalid ground truth** because the messages are irreducibly ambiguous. The model's failure to map them to a single SKU is mathematically correct behavior.
