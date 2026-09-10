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
        self.assertEqual(result["precision_at_1"], 1.0)
        self.assertEqual(result["precision_at_k"], 1.0)

    def test_evaluation_does_not_pollute_online_cache_stats(self):
        cases = [{"id": "guard", "question": "我的包裹到哪了", "expect_faq": False}]
        with patch.object(rag_eval, "load_cases", return_value=cases), \
             patch.object(rag_eval.rag, "best_answer", return_value=None) as best_answer, \
             patch.object(rag_eval.rag, "cache_stats", return_value={
                 "query_count": 0, "cache_hit_count": 0,
             }):
            result = rag_eval.evaluate(path="offline-test.jsonl")

        self.assertEqual(result["guard_pass_rate"], 1.0)
        best_answer.assert_called_once_with("我的包裹到哪了")

    def test_evaluation_exposes_guard_and_failure_reason_summaries(self):
        cases = [
            {"id": "wrong", "question": "怎么退货", "expected_questions": ["退货规则"]},
            {
                "id": "live",
                "question": "我的退款进度",
                "expect_faq": False,
                "guard_reason": "personal_refund_status",
            },
        ]
        recalled = [
            {"question": "其他规则", "answer": "答案", "distance": 0.2},
            {"question": "退货规则", "answer": "标准答案", "distance": 0.3},
        ]
        with patch.object(rag_eval, "load_cases", return_value=cases), \
             patch.object(rag_eval.rag, "retrieve", return_value=recalled), \
             patch.object(rag_eval.rag, "rerank", return_value=recalled), \
             patch.object(rag_eval.rag, "best_answer", return_value=None):
            result = rag_eval.evaluate(path="reason-test.jsonl")

        self.assertEqual(result["failure_reason_counts"], {"top1_wrong": 1})
        self.assertEqual(result["guard_reason_counts"], {"personal_refund_status": 1})
        self.assertEqual(result["details"][0]["failure_reason"], "top1_wrong")
        self.assertEqual(result["details"][1]["guard_reason"], "personal_refund_status")


if __name__ == "__main__":
    unittest.main()
