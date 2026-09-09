import unittest
from unittest.mock import patch

from app import rag_eval


class RagEvaluationTests(unittest.TestCase):
    def test_evaluate_reports_recall_and_precision(self):
        cases = [{"id": "1", "question": "怎么退货", "expected_questions": ["退货规则"]}]
        hits = [{"question": "退货规则", "answer": "答案", "distance": 0.1}]
        with patch.object(rag_eval, "load_cases", return_value=cases), patch.object(rag_eval.rag, "retrieve", return_value=hits), patch.object(rag_eval.rag, "rerank", return_value=hits):
            result = rag_eval.evaluate()
        self.assertEqual(result["recall_at_k"], 1.0)
        self.assertEqual(result["precision_at_k"], 1.0)


if __name__ == "__main__":
    unittest.main()
