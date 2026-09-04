import unittest
from unittest.mock import patch

from fastapi import HTTPException, Response

from app import db, main


class WorkflowDbTests(unittest.TestCase):
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

    class Connection:
        def __init__(self, cursor):
            self.cursor_obj = cursor

        def cursor(self):
            return self.cursor_obj

        def close(self):
            pass

        def begin(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    def test_claim_is_atomic_and_requires_unassigned_escalated_ticket(self):
        cursor = self.Cursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            self.assertTrue(db.claim_ticket(7, "agent-a"))

        sql, params = cursor.calls[0]
        self.assertIn("status = 'escalated'", sql)
        self.assertIn("assigned_to IS NULL", sql)
        self.assertEqual(params, ("agent-a", 7))

    def test_transfer_is_limited_to_current_holder(self):
        cursor = self.Cursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            self.assertTrue(db.transfer_ticket(7, "agent-a", "agent-b"))

        sql, params = cursor.calls[0]
        self.assertIn("assigned_to = %s", sql)
        self.assertEqual(params, ("agent-b", 7, "agent-a"))

    def test_reply_keeps_ticket_in_progress_and_checks_current_agent(self):
        cursor = self.Cursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            self.assertTrue(db.update_ticket_reply(7, "正在为您核实", "agent-a"))

        sql, params = cursor.calls[0]
        self.assertIn("status = 'in_progress'", sql)
        self.assertIn("resolved_at = NULL", sql)
        self.assertIn("assigned_to = %s", sql)
        self.assertEqual(params, ("正在为您核实", "agent-a", 7, "agent-a"))

    def test_close_active_human_tickets_only_closes_current_users_open_tickets(self):
        class CloseCursor(self.Cursor):
            def fetchall(self):
                return [{"id": 42}, {"id": 41}]

        cursor = CloseCursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            self.assertEqual(db.close_active_human_tickets("alice"), [42, 41])

        select_sql, select_params = cursor.calls[0]
        update_sql, update_params = cursor.calls[1]
        self.assertIn("username = %s", select_sql)
        self.assertIn("FOR UPDATE", select_sql)
        self.assertEqual(select_params, ("alice",))
        self.assertIn("status = 'closed'", update_sql)
        self.assertEqual(update_params, [42, 41])


class WorkflowApiTests(unittest.TestCase):
    def test_claim_rejects_ticket_owned_by_another_agent(self):
        ticket = {"id": 7, "status": "in_progress", "assigned_to": "agent-b"}
        with patch.object(main.db, "get_ticket", return_value=ticket):
            with self.assertRaises(HTTPException) as ctx:
                main.claim_escalation(7, "agent-a")

        self.assertEqual(ctx.exception.status_code, 409)

    def test_resolve_rejects_ticket_owned_by_another_agent(self):
        ticket = {"id": 7, "status": "in_progress", "assigned_to": "agent-b"}
        with patch.object(main.db, "get_ticket", return_value=ticket):
            with self.assertRaises(HTTPException) as ctx:
                main.resolve_escalation(
                    7,
                    main.ResolveRequest(human_answer="已处理"),
                    "agent-a",
                )

        self.assertEqual(ctx.exception.status_code, 409)


class HumanHandoffApiTests(unittest.TestCase):
    def test_customer_can_end_human_session(self):
        active = [
            {"id": 42, "username": "alice", "status": "in_progress"},
            {"id": 41, "username": "alice", "status": "escalated"},
        ]
        with patch.object(main.db, "close_active_human_tickets", return_value=[42, 41]), \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.end_human_session("alice")

        self.assertEqual(result, {"status": "ok", "closed_ticket_ids": [42, 41]})
        self.assertEqual(insert_log.call_count, 2)
        self.assertEqual(publish.call_count, 2)

    def test_logout_ends_human_session_before_deleting_cookie_session(self):
        with patch.object(main.auth, "extract_token", return_value="session-token"), \
             patch.object(main.auth, "get_username", return_value="alice"), \
             patch.object(main, "_end_human_session") as end_session, \
             patch.object(main.auth, "delete_session") as delete_session:
            result = main.logout(Response(), session_token="session-token")

        self.assertEqual(result, {"status": "ok"})
        end_session.assert_called_once_with("alice", reason="用户退出登录，结束人工会话")
        delete_session.assert_called_once_with("session-token")

    def test_followup_during_open_human_ticket_does_not_call_ai(self):
        active_ticket = {
            "id": 42,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "admin",
            "human_answer": None,
        }
        with patch.object(main.db, "get_active_human_ticket_for_user", return_value=active_ticket), \
             patch.object(main.db, "append_customer_message", return_value=active_ticket), \
             patch.object(main.db, "insert_ticket_log"), \
             patch.object(main.events.broker, "publish"), \
             patch.object(
                 main.router,
                 "process_ticket",
                 side_effect=AssertionError("人工接管中的补充消息不应再次调用 AI"),
             ):
            result = main.create_ticket(
                main.TicketCreate(ticket_text="补充订单信息"),
                "alice",
            )

        self.assertEqual(result["id"], 42)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["reply_source"], "human_queue")
        self.assertIn("人工", result["reply"])

    def test_customer_followup_wakes_waiting_ticket_and_notifies_agent(self):
        active_ticket = {
            "id": 43,
            "username": "alice",
            "status": "waiting_customer",
            "assigned_to": "admin",
            "human_answer": None,
        }
        updated_ticket = dict(active_ticket, status="in_progress")
        with patch.object(main.db, "get_active_human_ticket_for_user", return_value=active_ticket), \
             patch.object(main.db, "append_customer_message", return_value=updated_ticket), \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.db, "log_status_change") as log_status, \
             patch.object(main.events.broker, "publish") as publish, \
             patch.object(main.router, "process_ticket") as process_ticket:
            result = main.create_ticket(
                main.TicketCreate(ticket_text="我补充一下故障现象"),
                "alice",
            )

        process_ticket.assert_not_called()
        log_status.assert_called_once_with(43, "in_progress", "alice")
        insert_log.assert_called_once_with(
            43,
            "customer_message",
            "客户在人工处理中补充消息",
            "alice",
        )
        publish.assert_called_once()
        self.assertEqual(result["status"], "in_progress")

    def test_human_reply_keeps_handoff_active(self):
        ticket = {
            "id": 44,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "admin",
            "ticket_text": "我要投诉物流问题",
        }
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "update_ticket_reply", return_value=True), \
             patch.object(main.db, "insert_ticket_message"), \
             patch.object(main.db, "log_status_change"), \
             patch.object(main.events.broker, "publish") as publish:
            result = main.reply_escalation(
                44,
                main.ResolveRequest(human_answer="我正在为您核实，请稍候", save_to_kb=False),
                "admin",
            )

        self.assertEqual(result["workflow_status"], "in_progress")
        self.assertTrue(result["handoff_active"])
        event_payload = publish.call_args.args[1]
        self.assertTrue(event_payload["handoff_active"])
        self.assertEqual(event_payload["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
