import unittest
from unittest.mock import patch

from app import db, events
from app.sla import SLAWorker


class SLAWorkerTests(unittest.TestCase):
    def test_run_once_marks_a_ticket_and_notifies_admin(self):
        overdue = [{"id": 7, "username": "alice", "status": "in_progress"}]
        with patch.object(db, "mark_overdue_tickets", return_value=overdue), \
             patch.object(db, "insert_ticket_log") as insert_log, \
             patch.object(events.broker, "publish") as publish:
            result = SLAWorker(interval_seconds=60).run_once()

        self.assertEqual(result, overdue)
        insert_log.assert_called_once_with(
            7,
            "sla_breached",
            "人工工单超过 SLA 时限，系统提升为紧急优先级",
            "system",
        )
        publish.assert_called_once_with(
            "ticket_updated",
            {
                "ticket_id": 7,
                "username": "alice",
                "status": "in_progress",
                "priority": "urgent",
                "sla_breached": True,
            },
        )

    def test_run_idle_close_once_closes_ticket_and_notifies_staff(self):
        idle = [{"id": 8, "username": "alice", "status": "waiting_customer"}]
        with patch.object(db, "close_idle_human_tickets", return_value=idle), \
             patch.object(db, "insert_ticket_log") as insert_log, \
             patch.object(events.broker, "publish") as publish:
            result = SLAWorker(interval_seconds=60).run_idle_close_once()

        self.assertEqual(result, idle)
        insert_log.assert_called_once_with(
            8,
            "human_idle_closed",
            "客户长时间无活动，系统自动结束人工会话",
            "system",
        )
        publish.assert_called_once_with(
            "ticket_updated",
            {
                "ticket_id": 8,
                "username": "alice",
                "status": "closed",
                "reason": "客户长时间无活动，系统自动结束人工会话",
            },
        )
    def test_db_update_is_atomic_and_only_marks_unmarked_active_tickets(self):
        class Cursor:
            rowcount = 1

            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchall(self):
                return [{"id": 7, "username": "alice", "status": "escalated"}]

        class Connection:
            def __init__(self, cursor):
                self.cursor_obj = cursor

            def begin(self):
                pass

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        cursor = Cursor()
        with patch.object(db, "_connect", return_value=Connection(cursor)):
            result = db.mark_overdue_tickets()

        self.assertEqual(result[0]["id"], 7)
        select_sql, select_params = cursor.calls[0]
        update_sql, update_params = cursor.calls[1]
        self.assertIn("sla_due_at <= NOW()", select_sql)
        self.assertIn("COALESCE(sla_breached, 0) = 0", select_sql)
        self.assertIn("FOR UPDATE", select_sql)
        self.assertEqual(select_params, (100,))
        self.assertIn("sla_breached = 1", update_sql)
        self.assertIn("priority = 'urgent'", update_sql)
        self.assertEqual(update_params, [7])

    def test_db_idle_close_uses_last_customer_message_and_active_status_guard(self):
        class Cursor:
            rowcount = 1

            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params):
                self.calls.append((sql, params))

            def fetchall(self):
                return [{"id": 8, "username": "alice", "status": "waiting_customer"}]

        class Connection:
            def __init__(self, cursor):
                self.cursor_obj = cursor

            def begin(self):
                pass

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        cursor = Cursor()
        with patch.object(db, "_connect", return_value=Connection(cursor)):
            result = db.close_idle_human_tickets(idle_minutes=30, limit=50)

        self.assertEqual(result[0]["id"], 8)
        select_sql, select_params = cursor.calls[0]
        update_sql, update_params = cursor.calls[1]
        self.assertIn("sender_type = 'customer'", select_sql)
        self.assertIn("DATE_SUB(NOW(), INTERVAL %s MINUTE)", select_sql)
        self.assertIn("FOR UPDATE", select_sql)
        self.assertEqual(select_params, (30, 50))
        self.assertIn("status = 'closed'", update_sql)
        self.assertIn("status IN ('escalated', 'in_progress', 'waiting_customer')", update_sql)
        self.assertEqual(update_params, [8])


if __name__ == "__main__":
    unittest.main()
