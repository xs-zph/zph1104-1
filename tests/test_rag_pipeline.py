import unittest
from unittest.mock import patch

from app import rag
from app.config import Config


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores
        self.pairs = None

    def predict(self, pairs):
        self.pairs = pairs
        return self.scores


class RerankerTests(unittest.TestCase):
    def test_cross_encoder_score_controls_candidate_order(self):
        model = FakeReranker([0.2, 0.95, 0.4])
        chunks = [
            {"question": "物流", "answer": "物流回答", "distance": 0.1},
            {"question": "退货", "answer": "退货回答", "distance": 0.3},
            {"question": "发票", "answer": "发票回答", "distance": 0.2},
        ]
        with patch.object(Config, "RERANKER_ENABLED", True), patch.object(rag, "_get_reranker", return_value=model):
            result = rag.rerank("我想退货", chunks, top_n=2)

        self.assertEqual([item["question"] for item in result], ["退货", "发票"])
        self.assertEqual(result[0]["rerank_score"], 0.95)
        self.assertEqual(model.pairs[0], ["我想退货", "物流\n物流回答"])

    def test_reranker_failure_falls_back_to_vector_distance(self):
        chunks = [
            {"question": "弱相关", "answer": "", "distance": 0.6},
            {"question": "强相关", "answer": "", "distance": 0.1},
        ]
        with patch.object(Config, "RERANKER_ENABLED", True), patch.object(
            rag, "_get_reranker", side_effect=RuntimeError("model unavailable")
        ):
            result = rag.rerank("问题", chunks, top_n=2)

        self.assertEqual([item["question"] for item in result], ["强相关", "弱相关"])


if __name__ == "__main__":
    unittest.main()
