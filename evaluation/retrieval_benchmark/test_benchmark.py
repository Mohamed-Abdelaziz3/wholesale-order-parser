import sys
import unittest
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.matcher import ProductMatcher
from app.catalog import load_catalog
from evaluation.retrieval_benchmark.run_benchmark import RapidFuzzBaseline
from evaluation.retrieval_benchmark.attribute_parser import AttributeParser

class TestBenchmark(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(str(PROJECT_ROOT / "evaluation" / "data" / "synthetic_catalog_400.csv"))
        self.app_matcher = ProductMatcher(self.catalog)
        self.baseline = RapidFuzzBaseline(self.catalog)
        self.parser = AttributeParser()

    def test_baseline_exact_match(self):
        query = "مسحوق غسيل أوتوماتيك لافندر الفارس وزن 5 كيلو"
        
        # Application matcher
        app_cands = self.app_matcher.find_candidates(query, top_k=5, min_score=0.0)
        app_results = [(c.product_id, c.score) for c in app_cands]
        
        # Benchmark Method A
        bench_results = self.baseline.retrieve(query, top_k=5)
        
        self.assertEqual(app_results, bench_results)
        
    def test_attribute_contradiction(self):
        q = self.parser.parse("5 لتر كلور")
        d = self.parser.parse("كلور 20 لتر")
        is_hard, _ = self.parser.get_penalty(q, d)
        self.assertTrue(is_hard)
        
    def test_attribute_missing(self):
        q = self.parser.parse("كلور")
        d = self.parser.parse("كلور 20 لتر")
        is_hard, _ = self.parser.get_penalty(q, d)
        self.assertFalse(is_hard)

if __name__ == '__main__':
    unittest.main()
