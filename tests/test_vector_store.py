import unittest
from unittest.mock import MagicMock

from app.vector_store import ResilientStore


class ResilientStoreTests(unittest.TestCase):
    def test_writes_are_mirrored_to_chroma_fallback(self):
        primary = MagicMock()
        fallback = MagicMock()
        store = ResilientStore(primary, fallback, "faq")

        store.upsert(ids=["faq-1"], documents=["退货"], metadatas=[{"answer": "支持"}])
        store.delete(ids=["faq-1"])

        primary.upsert.assert_called_once()
        fallback.upsert.assert_called_once()
        primary.delete.assert_called_once_with(ids=["faq-1"])
        fallback.delete.assert_called_once_with(ids=["faq-1"])

    def test_primary_write_failure_uses_fallback_once(self):
        primary = MagicMock()
        primary.upsert.side_effect = RuntimeError("offline")
        fallback = MagicMock()
        store = ResilientStore(primary, fallback, "faq")

        store.upsert(ids=["faq-1"], documents=["退货"], metadatas=[{}])

        fallback.upsert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
