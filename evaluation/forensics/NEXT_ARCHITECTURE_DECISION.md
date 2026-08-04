# Next Architecture Decision

## Primary Recommendation: D. Hybrid lexical and semantic retrieval plus deterministic attribute filtering

### Rationale
The forensic evidence shows that `RapidFuzz` achieved a remarkable 90.38% Top-5 candidate recall on API-successful orders. However, its ranking accuracy failed catastrophically (78 items ranked below incorrect items) because fuzzy string matching cannot understand that "5%" vs "12%" is a critical semantic difference, whereas "غسيل" vs "للغسيل" is merely morphological noise. 

By combining character n-gram/BM25 (to handle spelling noise) with a semantic embedding model (to handle morphological variations) and **deterministic attribute filtering** (explicitly parsing numbers, units, sizes, and brands from the LLM extraction), we can mathematically eliminate incorrect candidates from the Top 5.

### Fallback Recommendation: E. Hybrid retrieval followed by a cross-encoder reranker
If deterministic filtering proves too brittle for the Egyptian dialect, a cross-encoder reranker provides the highest theoretical accuracy by directly comparing the query and the candidate. 

### Assessment

- **Expected Impact**: Will directly resolve the 78 ranking failures and the 48 false-ambiguity threshold failures, pushing API-successful accuracy from 14.4% to >85%.
- **Implementation Complexity**: Medium. Requires adding a vector store (or in-memory FAISS) and an attribute parsing schema to Gemini.
- **Inference Cost & Latency**: LLM extraction remains the bottleneck (1.02s median). Local retrieval and attribute filtering take <50ms. 
- **Suitability for Small Egyptian Wholesaler**: High. It avoids complex fine-tuning and operates efficiently on modest hardware.
- **Purpose of DigitalOcean GPU Compute**: The GPU is **NOT genuinely required** for the primary architecture (D), as dense embeddings (e.g., MiniLM-L6-v2) and lexical search can run efficiently on CPU for a 400-item catalog. If the fallback (E) is adopted, the GPU would host the cross-encoder reranker to keep latency under 100ms. Do not waste GPU credits on training a model when the catalog is this small.

### Next Implementation Step
1. Update the Gemini extraction prompt to return a structured JSON with explicit `attributes` (brand, size, color, concentration).
2. Implement an in-memory BM25 + dense embedding hybrid retriever to generate Top-10 candidates.
3. Apply a deterministic hard-filter based on the extracted attributes to select the Top-1 candidate.
