import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app import main, rag


class KnowledgeAuditTests(unittest.TestCase):
    def test_document_ingest_replaces_previous_chunks_from_same_source(self):
        collection = MagicMock()
        collection.get.return_value = {"ids": ["upload-old-0"]}
        with patch.object(rag, "get_docs_collection", return_value=collection):
            result = rag.ingest_document_text("退货政策\n\n支持七天无理由退货", "policy.md")

        collection.delete.assert_called_once_with(ids=["upload-old-0"])
        collection.upsert.assert_called_once()
        self.assertEqual(result["source"], "policy.md")
        self.assertGreater(result["chunks"], 0)

    def test_agent_knowledge_tool_includes_uploaded_document_chunks(self):
        from app import agent

        faq = [{"question": "退货政策", "answer": "支持七天无理由退货", "distance": 0.4}]
        docs = [{"source": "policy.docx", "text": "商品签收后七天内可申请退货", "distance": 0.5}]
        with patch.object(agent.rag, "retrieve", return_value=faq), \
             patch.object(agent.rag, "retrieve_docs", return_value=docs), \
             patch.object(agent.rag, "rerank", return_value=faq):
            result = agent._search_faq("如何退货？")

        self.assertIn("支持七天无理由退货", result)
        self.assertIn("policy.docx", result)

    def test_add_entry_records_creator_snapshot(self):
        entry = {"id": 7, "question": "如何退货？", "answer": "请提交售后申请", "enabled": 1}
        with patch.object(rag.db, "insert_faq_entry", return_value=7), \
             patch.object(rag.db, "get_faq_entry", return_value=entry), \
             patch.object(rag.db, "insert_faq_audit") as audit, \
             patch.object(rag, "sync_faq_to_chroma"):
            result = rag.add_entry("如何退货？", "请提交售后申请", operator="admin")

        self.assertEqual(result, entry)
        audit.assert_called_once_with(7, "created", "如何退货？", "请提交售后申请", True, "admin")

    def test_admin_can_read_history_for_existing_faq(self):
        history = [{"faq_id": 7, "action": "updated", "operator": "admin"}]
        with patch.object(main.db, "get_faq_entry", return_value={"id": 7}), \
             patch.object(main.db, "list_faq_audits", return_value=history):
            result = main.faq_history(7, "admin")

        self.assertEqual(result, history)

    def test_history_returns_not_found_for_missing_faq(self):
        with patch.object(main.db, "get_faq_entry", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                main.faq_history(999, "admin")

        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
