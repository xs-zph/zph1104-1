import unittest
from unittest.mock import patch

from app import memory


class PersistentMemoryTests(unittest.TestCase):
    def setUp(self):
        memory._HISTORY.clear()

    def tearDown(self):
        memory._HISTORY.clear()

    def test_get_history_reads_current_persistent_conversation(self):
        expected = [
            {"role": "user", "content": "我的订单呢？"},
            {"role": "assistant", "content": "我来帮您查询。"},
        ]
        with patch.object(memory.db, "list_conversation_history", return_value=expected) as read:
            result = memory.get_history("user")

        self.assertEqual(result, expected)
        read.assert_called_once_with("user", max_messages=memory.MAX_TURNS * 2)

    def test_append_writes_persistent_message(self):
        with patch.object(memory.db, "append_conversation_message") as append:
            memory.append("user", "user", "我常用耳机")

        append.assert_called_once_with("user", "user", "我常用耳机", max_messages=memory.MAX_TURNS * 2)

    def test_clear_rotates_persistent_conversation(self):
        with patch.object(memory.db, "clear_conversation_memory") as clear:
            memory.clear("user")

        clear.assert_called_once_with("user")
        self.assertNotIn("user", memory._HISTORY)

    def test_database_failure_falls_back_to_process_memory(self):
        with patch.object(memory.db, "append_conversation_message", side_effect=RuntimeError("db down")):
            memory.append("user", "user", "临时消息")

        with patch.object(memory.db, "list_conversation_history", side_effect=RuntimeError("db down")):
            result = memory.get_history("user")

        self.assertEqual(result, [{"role": "user", "content": "临时消息"}])


if __name__ == "__main__":
    unittest.main()
