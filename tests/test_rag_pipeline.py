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
    def test_normalize_question_collapses_common_variants(self):
        self.assertEqual(rag.normalize_question("请问 退货需要几天？"), rag.normalize_question("退货需要几天"))
        self.assertEqual(rag.normalize_question("我的快递到哪了"), "物流查询")

    def test_cache_hit_skips_retrieval(self):
        rag.clear_faq_cache()
        answer = {"question": "退货", "answer": "可退", "distance": 0.1}
        with patch.object(rag, "retrieve", return_value=[answer]), patch.object(rag, "rerank", return_value=[answer]):
            self.assertEqual(rag.best_answer("退货"), answer)
        with patch.object(rag, "retrieve", side_effect=AssertionError("不应再次检索")):
            cached = rag.best_answer("退货！")
        self.assertEqual(cached["answer"], "可退")
        self.assertTrue(cached["cache_hit"])

    def test_semantic_cache_reuses_confirmed_faq_answer(self):
        rag.clear_faq_cache()
        answer = {"question": "退货需要满足什么条件？", "answer": "七天无理由", "distance": 0.1}
        with patch.object(rag, "_get_embedding_fn", return_value=lambda items: [[1.0, 0.0] for _ in items]):
            rag._faq_semantic_cache.append({"vector": [1.0, 0.0], "answer": answer})
            with patch.object(rag, "retrieve", side_effect=AssertionError("高置信语义命中不应检索")):
                result = rag.best_answer("商品签收后多久能退？")
        self.assertEqual(result["answer"], "七天无理由")
        self.assertEqual(result["cache_layer"], "semantic")

    def test_low_similarity_falls_back_to_retrieval(self):
        rag.clear_faq_cache()
        answer = {"question": "退货需要满足什么条件？", "answer": "七天无理由", "distance": 0.1}
        fallback = {"question": "天气", "answer": "天气答案", "distance": 0.1}
        with patch.object(rag, "_get_embedding_fn", return_value=lambda items: [[1.0, 0.0] if item == "退货" else [0.0, 1.0] for item in items]):
            rag._faq_semantic_cache.append({"vector": [1.0, 0.0], "answer": answer})
            with patch.object(rag, "retrieve", return_value=[fallback]), patch.object(rag, "rerank", return_value=[fallback]):
                result = rag.best_answer("明天会下雨吗")
        self.assertEqual(result["answer"], "天气答案")

    def test_live_order_question_bypasses_faq_cache(self):
        rag.clear_faq_cache()
        with patch.object(rag, "retrieve", side_effect=AssertionError("实时查询不应进入 FAQ")):
            self.assertIsNone(rag.best_answer("我的包裹现在到哪儿了？"))

    def test_known_variant_is_preloaded_as_exact_cache(self):
        rag.clear_faq_cache()
        entries = [{"id": 1, "question": "退货条件", "answer": "七天可退"}]
        with patch.object(rag, "load_faq_variants", return_value={"退货条件": ["这个商品能退吗"]}), patch.object(
            rag, "_get_embedding_fn", return_value=lambda items: [[1.0, 0.0] for _ in items]
        ):
            rag._warm_semantic_cache(entries)
        with patch.object(rag, "retrieve", side_effect=AssertionError("已知问法不应检索")):
            result = rag.best_answer("这个商品能退吗？")
        self.assertEqual(result["answer"], "七天可退")
        self.assertEqual(result["cache_layer"], "memory")

    def test_chunk_defaults_are_medium_sized_and_sentence_aware(self):
        text = "。".join(["第一段内容说明退货规则" * 50, "第二段内容说明保修规则" * 50])
        chunks = rag.chunk_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= Config.RAG_CHUNK_SIZE for chunk in chunks))

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
