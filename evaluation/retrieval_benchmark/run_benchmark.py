import json
import csv
import time
import math
import hashlib
from collections import defaultdict
from pathlib import Path
import re
import numpy as np

BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent.parent

import sys
sys.path.insert(0, str(PROJECT_ROOT))

from app.matcher import ProductMatcher
from app.catalog import load_catalog
from rapidfuzz import fuzz
from rapidfuzz.process import extract as rf_extract

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from rank_bm25 import BM25Okapi, BM25Plus

from sentence_transformers import SentenceTransformer

from evaluation.retrieval_benchmark.attribute_parser import AttributeParser

class Document:
    def __init__(self, item, parser):
        self.id = item.product_id
        self.name = item.product_name
        self.aliases = item.aliases
        self.unit = item.unit
        self.text_canonical = self.name
        
        # Build rich text
        self.text_rich = f"{self.name} {self.aliases} {self.unit}"
        
        # Precompute attributes
        self.attrs = parser.parse(self.text_rich)
        
def tokenize_arabic(text):
    text = re.sub(r'[^\w\s]', ' ', text)
    return text.split()

class RetrievalMethod:
    def __init__(self, name):
        self.name = name
        self.index_size = 0
        self.index_time = 0
        self.model_load_time = 0
        self.model_download_time = 0

    def index(self, docs, mode="canonical"):
        pass

    def retrieve(self, query, top_k=10):
        return [] # [(doc_id, score)]

class RapidFuzzBaseline(RetrievalMethod):
    def __init__(self, catalog):
        super().__init__("Method A: RapidFuzz Baseline")
        self.matcher = ProductMatcher(catalog)
        self.catalog = catalog
    
    def retrieve(self, query, top_k=10):
        # We need it to return list of (doc_id, score)
        cands = self.matcher.find_candidates(query, top_k=top_k, min_score=0.0)
        return [(c.product_id, c.score) for c in cands]

class RapidFuzzWRatio(RetrievalMethod):
    def __init__(self):
        super().__init__("Method B: RapidFuzz WRatio")
        self.docs = []
    
    def index(self, docs, mode="canonical"):
        self.docs = docs
        self.mode = mode
        
    def retrieve(self, query, top_k=10):
        choices = {d.id: (d.text_canonical if self.mode == "canonical" else d.text_rich) for d in self.docs}
        results = rf_extract(query, choices, scorer=fuzz.WRatio, limit=top_k)
        return [(match[2], match[1]/100.0) for match in results]

class CharNgramTFIDF(RetrievalMethod):
    def __init__(self):
        super().__init__("Method C: Character N-Gram TF-IDF")
        self.vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(3,5))
        self.doc_ids = []
        self.matrix = None
        
    def index(self, docs, mode="canonical"):
        start = time.time()
        self.doc_ids = [d.id for d in docs]
        texts = [d.text_canonical if mode == "canonical" else d.text_rich for d in docs]
        self.matrix = self.vec.fit_transform(texts)
        self.index_time = time.time() - start
        self.index_size = self.matrix.data.nbytes
        
    def retrieve(self, query, top_k=10):
        q_vec = self.vec.transform([query])
        sims = cosine_similarity(q_vec, self.matrix)[0]
        top_idx = sims.argsort()[-top_k:][::-1]
        return [(self.doc_ids[i], sims[i]) for i in top_idx]

class BM25Retriever(RetrievalMethod):
    def __init__(self, variant="okapi"):
        super().__init__(f"Method D/E: BM25 ({variant})")
        self.variant = variant
        self.doc_ids = []
        self.model = None
        
    def index(self, docs, mode="canonical"):
        start = time.time()
        self.doc_ids = [d.id for d in docs]
        texts = [d.text_canonical if mode == "canonical" else d.text_rich for d in docs]
        tokenized = [tokenize_arabic(t) for t in texts]
        if self.variant == "okapi":
            self.model = BM25Okapi(tokenized)
        else:
            self.model = BM25Plus(tokenized)
        self.index_time = time.time() - start
        
    def retrieve(self, query, top_k=10):
        tokenized_query = tokenize_arabic(query)
        scores = self.model.get_scores(tokenized_query)
        top_idx = np.argsort(scores)[-top_k:][::-1]
        return [(self.doc_ids[i], float(scores[i])) for i in top_idx]

class DenseEmbeddingRetriever(RetrievalMethod):
    def __init__(self, model_name, prefix_query="", prefix_passage=""):
        super().__init__(f"Dense: {model_name}")
        self.model_name = model_name
        self.prefix_query = prefix_query
        self.prefix_passage = prefix_passage
        self.doc_ids = []
        self.embeddings = None
        
        t0 = time.time()
        self.model = SentenceTransformer(model_name)
        self.model_load_time = time.time() - t0
        
    def index(self, docs, mode="canonical"):
        start = time.time()
        self.doc_ids = [d.id for d in docs]
        texts = [self.prefix_passage + (d.text_canonical if mode == "canonical" else d.text_rich) for d in docs]
        self.embeddings = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        self.index_time = time.time() - start
        self.index_size = self.embeddings.nbytes
        
    def retrieve(self, query, top_k=10):
        q_emb = self.model.encode([self.prefix_query + query], normalize_embeddings=True, convert_to_numpy=True)[0]
        sims = np.dot(self.embeddings, q_emb)
        top_idx = np.argsort(sims)[-top_k:][::-1]
        return [(self.doc_ids[i], float(sims[i])) for i in top_idx]

def min_max_normalize(scores_dict):
    if not scores_dict: return {}
    vals = list(scores_dict.values())
    min_v, max_v = min(vals), max(vals)
    if max_v - min_v == 0:
        return {k: 1.0 for k in scores_dict}
    return {k: (v - min_v) / (max_v - min_v) for k, v in scores_dict.items()}

class HybridRetriever(RetrievalMethod):
    def __init__(self, name, retrievers, weights):
        super().__init__(name)
        self.retrievers = retrievers
        self.weights = weights
        
    def retrieve(self, query, top_k=10):
        combined = defaultdict(float)
        for r, w in zip(self.retrievers, self.weights):
            res = r.retrieve(query, top_k=50)  # get more for fusion
            scores = {doc_id: score for doc_id, score in res}
            norm_scores = min_max_normalize(scores)
            for doc_id, s in norm_scores.items():
                combined[doc_id] += s * w
                
        sorted_res = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return sorted_res

class HybridWithAttributes(RetrievalMethod):
    def __init__(self, name, base_retriever, parser, docs):
        super().__init__(name)
        self.base = base_retriever
        self.parser = parser
        self.doc_dict = {d.id: d for d in docs}
        
    def retrieve(self, query, top_k=10):
        q_attrs = self.parser.parse(query)
        base_res = self.base.retrieve(query, top_k=50)
        
        final_res = []
        for doc_id, score in base_res:
            d = self.doc_dict[doc_id]
            is_hard, penalty = self.parser.get_penalty(q_attrs, d.attrs)
            if is_hard:
                score -= 1000.0  # huge penalty for explicit contradiction
            else:
                score -= penalty
            final_res.append((doc_id, score))
            
        return sorted(final_res, key=lambda x: x[1], reverse=True)[:top_k]

def run_evaluation(methods, queries, eval_ids):
    # queries is list of dicts from benchmark_queries.jsonl
    eval_queries = [q for q in queries if q['order_id'] in eval_ids]
    
    results = {}
    query_results_csv = []
    
    for method in methods:
        print(f"Running {method.name}...")
        metrics = {
            "recall_1": 0, "recall_3": 0, "recall_5": 0, "recall_10": 0,
            "mrr": 0, "latencies": [], "total_queries": 0,
            "safety_ambiguous_margins": []
        }
        
        # Cold start
        if eval_queries:
            _ = method.retrieve(eval_queries[0]['extracted_text'])
            
        for q in eval_queries:
            text = q['extracted_text']
            if not text: continue
            
            t0 = time.perf_counter()
            res = method.retrieve(text, top_k=10)
            latency = (time.perf_counter() - t0) * 1000
            metrics['latencies'].append(latency)
            metrics['total_queries'] += 1
            
            expected = q['expected_product_code']
            cat = q['inclusion_category']
            
            # evaluate exact match
            if cat == "api_successful_in_catalog" and expected:
                rank = -1
                for i, (doc_id, score) in enumerate(res):
                    if doc_id == expected:
                        rank = i + 1
                        break
                if rank > 0:
                    if rank == 1: metrics['recall_1'] += 1
                    if rank <= 3: metrics['recall_3'] += 1
                    if rank <= 5: metrics['recall_5'] += 1
                    if rank <= 10: metrics['recall_10'] += 1
                    metrics['mrr'] += 1.0 / rank
                
                query_results_csv.append({
                    "method": method.name,
                    "order_id": q['order_id'],
                    "expected_code": expected,
                    "extracted_text": text,
                    "rank": rank,
                    "top_1_doc": res[0][0] if res else None,
                    "top_1_score": res[0][1] if res else None
                })
            
            if cat in ("questionable_ambiguous", "out_of_catalog"):
                if len(res) > 1:
                    margin = res[0][1] - res[1][1]
                    metrics['safety_ambiguous_margins'].append(margin)
                    
        results[method.name] = metrics
        
    return results, query_results_csv

def main():
    catalog = load_catalog(str(PROJECT_ROOT / "evaluation" / "data" / "synthetic_catalog_400.csv"))
    parser = AttributeParser()
    docs = [Document(c, parser) for c in catalog]
    
    with open(BENCHMARK_DIR / "split_manifest.json", encoding="utf-8") as f:
        splits = json.load(f)
        
    queries = []
    with open(BENCHMARK_DIR / "benchmark_queries.jsonl", encoding="utf-8") as f:
        for line in f:
            queries.append(json.loads(line))
            
    # Initialize methods
    m_rf_base = RapidFuzzBaseline(catalog)
    m_rf_wratio = RapidFuzzWRatio()
    m_tfidf = CharNgramTFIDF()
    m_bm25o = BM25Retriever("okapi")
    m_bm25p = BM25Retriever("plus")
    
    # We load models if available
    methods = [m_rf_base, m_rf_wratio, m_tfidf, m_bm25o, m_bm25p]
    
    print("Loading Dense Models...")
    try:
        m_minilm = DenseEmbeddingRetriever("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        m_e5 = DenseEmbeddingRetriever("intfloat/multilingual-e5-small", prefix_query="query: ", prefix_passage="passage: ")
        methods.extend([m_minilm, m_e5])
        
        m_hybrid_lex = HybridRetriever("Method H: Hybrid Lexical (BM25+TFIDF)", [m_bm25p, m_tfidf], [0.5, 0.5])
        m_hybrid_minilm = HybridRetriever("Method I: Lexical + MiniLM", [m_hybrid_lex, m_minilm], [0.4, 0.6])
        m_hybrid_e5 = HybridRetriever("Method J: Lexical + E5", [m_hybrid_lex, m_e5], [0.4, 0.6])
        
        m_best_hybrid = m_hybrid_e5 # Assuming E5 is best for now
        m_attr_hybrid = HybridWithAttributes("Method K: Hybrid + Attributes", m_best_hybrid, parser, docs)
        
        methods.extend([m_hybrid_lex, m_hybrid_minilm, m_hybrid_e5, m_attr_hybrid])
    except Exception as e:
        print(f"Skipping dense models due to error: {e}")
        
    print("Indexing...")
    for m in methods:
        if hasattr(m, 'index'):
            m.index(docs, mode="rich")
            
    print("Evaluating Dev Split for metrics tuning...")
    dev_results, _ = run_evaluation(methods, queries, splits['dev_split'])
    
    print("Evaluating Held-Out Split...")
    eval_results, q_csv = run_evaluation(methods, queries, splits['eval_split'])
    
    # Save Metrics CSV
    with open(BENCHMARK_DIR / "method_metrics.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["method", "recall_1", "recall_5", "recall_10", "mrr", "median_latency_ms", "index_size_bytes"])
        for name, res in eval_results.items():
            total = sum(1 for q in queries if q['order_id'] in splits['eval_split'] and q['inclusion_category'] == 'api_successful_in_catalog' and q['expected_product_code'])
            if total == 0: continue
            
            lats = sorted(res['latencies'])
            med_lat = lats[len(lats)//2] if lats else 0
            
            writer.writerow([
                name,
                res['recall_1'] / total,
                res['recall_5'] / total,
                res['recall_10'] / total,
                res['mrr'] / total,
                med_lat,
                0 # index size (TODO: populate from method if tracked)
            ])
            
    with open(BENCHMARK_DIR / "query_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["method", "order_id", "expected_code", "extracted_text", "rank", "top_1_doc", "top_1_score"])
        writer.writeheader()
        for row in q_csv:
            writer.writerow(row)
            
    # Save report
    with open(BENCHMARK_DIR / "RETRIEVAL_BENCHMARK_REPORT.md", "w", encoding="utf-8") as f:
        f.write("# Offline Retrieval Benchmark Report\n\n")
        f.write("Evaluation complete. Please refer to CSVs for details.\n")
        
if __name__ == "__main__":
    main()
