import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import main, rag


class KnowledgeAuditTests(unittest.TestCase):
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
