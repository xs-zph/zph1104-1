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
        self.assertIn("开发票", rag.normalize_question("电子发票怎么开"))

    def test_normalize_question_understands_colloquial_and_typo_variants(self):
        self.assertIn("退货", rag.normalize_question("签收后几天能退呀？"))
        self.assertIn("条件", rag.normalize_question("退货有啥条见啊"))
        self.assertIn("保修", rag.normalize_question("耳机坏了还能保不？"))
        self.assertIn("申请维修", rag.normalize_question("耳机坏了咋报修？"))
        self.assertIn("修改收货地址", rag.normalize_question("地址填错了还能改么？"))

    def test_cache_hit_skips_retrieval(self):
        rag.clear_faq_cache()
        answer = {"question": "退货", "answer": "可退", "distance": 0.1}
        with patch.object(rag, "retrieve", return_value=[answer]), patch.object(rag, "rerank", return_value=[answer]):
            first = rag.best_answer("退货")
        self.assertEqual(first["answer"], answer["answer"])
        self.assertEqual(first["cache_layer"], "vector")
        with patch.object(rag, "retrieve", side_effect=AssertionError("不应再次检索")):
            cached = rag.best_answer("退货！")
        self.assertEqual(cached["answer"], "可退")
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["cache_layer"], "memory")

    def test_cache_stats_deduplicate_llm_savings_by_request(self):
        rag.clear_faq_cache()
        rag.reset_cache_stats()
        answer = {"question": "退货", "answer": "可退", "distance": 0.1}
        with patch.object(rag, "_get_embedding_fn", return_value=lambda items: [[1.0, 0.0] for _ in items]), \
             patch.object(rag, "retrieve", return_value=[answer]), \
             patch.object(rag, "rerank", return_value=[answer]):
            rag.best_answer("退货")
            rag.best_answer("退货！")

        stats = rag.cache_stats()
        self.assertEqual(stats["query_count"], 2)
        self.assertEqual(stats["cache_hit_count"], 1)
        self.assertEqual(stats["vector_hit_count"], 1)
        self.assertEqual(stats["answer_hit_count"], 2)
        self.assertEqual(stats["llm_saved"], 2)
        self.assertEqual(stats["layer_hits"]["memory"], 1)
        self.assertEqual(stats["layer_hits"]["vector"], 1)
        self.assertEqual(stats["hit_rate"], 0.5)

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

    def test_reranker_fallback_prefers_matching_chinese_phrase(self):
        chunks = [
            {"question": "退款多久能到账？", "answer": "退款答案", "distance": 0.08},
            {"question": "退货需要满足什么条件？", "answer": "退货答案", "distance": 0.2},
        ]
        with patch.object(Config, "RERANKER_ENABLED", True), patch.object(
            rag, "_get_reranker", side_effect=RuntimeError("model unavailable")
        ):
            result = rag.rerank("我收到货后几天内可以退货？", chunks, top_n=2)

        self.assertEqual(result[0]["question"], "退货需要满足什么条件？")
        self.assertGreater(result[0]["rerank_score"], result[1]["rerank_score"])


if __name__ == "__main__":
    unittest.main()
