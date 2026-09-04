import unittest
from unittest.mock import patch

from app import db


class FakeConnection:
    def ping(self, reconnect=True):
        return None

    def close(self):
        return None


class ConnectionPoolTests(unittest.TestCase):
    def test_pool_reuses_connection_and_caps_creation(self):
        with patch.object(db, "_new_connection", side_effect=FakeConnection) as create:
            pool = db._ConnectionPool(2)
            first = pool.acquire()
            second = pool.acquire()
            first.close()
            reused = pool.acquire()
            second.close()
            reused.close()

        self.assertEqual(create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
