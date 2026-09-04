import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app import db, main, multi_agent, router


class BlackboardStub:
    def __init__(self):
        self.entries = []

    def publish(self, task_id, entry_type, payload, **kwargs):
        entry = {
            "task_id": task_id,
            "entry_type": entry_type,
            "source_agent": kwargs.get("source_agent", multi_agent.MAIN_AGENT),
            "payload": payload,
        }
        self.entries.append(entry)
        return entry

    def read(self, task_id):
        return [entry for entry in self.entries if entry["task_id"] == task_id]


class MultiAgentProtocolTests(unittest.TestCase):
    def test_child_to_child_message_is_rejected(self):
        with self.assertRaises(ValidationError) as context:
            multi_agent.AgentMessage(
                task_id="task-1",
                message_id="message-1",
                sender="order_agent",
                receiver="knowledge_agent",
                message_type="task_result",
            )

        self.assertIn("子 Agent 之间禁止直接通信", str(context.exception))

    def test_child_cannot_write_blackboard(self):
        board = multi_agent.Blackboard()
        with self.assertRaises(multi_agent.AgentProtocolError):
            board.publish(
                "task-1", "fact", {"value": "x"}, source_agent="order_agent"
            )

    def test_database_layer_rejects_non_main_agent_blackboard_writes(self):
        with self.assertRaises(ValueError):
            db.insert_blackboard_entry(
                "task-1", "fact", {"value": "x"}, "knowledge_agent"
            )

    def test_protocol_round_trip_is_json_only(self):
        message = multi_agent.AgentMessage(
            task_id="task-1",
            message_id="message-1",
            sender=multi_agent.MAIN_AGENT,
            receiver="knowledge_agent",
            message_type="task_request",
            payload={"question": "保修多久"},
        )

        restored = multi_agent.AgentMessage.from_json(message.to_json())

        self.assertEqual(restored.payload["question"], "保修多久")
        self.assertEqual(restored.message_type, "task_request")


class MainAgentTests(unittest.TestCase):
    def test_main_agent_selects_specialist_and_returns_customer_reply(self):
        board = BlackboardStub()
        child = multi_agent.SpecialistAgent(
            "knowledge_agent", {"knowledge"}, {"search_faq"}, "处理知识库问题"
        )
        main = multi_agent.MainAgent(board, (child,))

        with patch.object(
            multi_agent.agent,
            "run_agent",
            return_value="保修期为一年",
        ) as run_agent:
            reply = main.run("耳机保修多久", "alice")

        self.assertEqual(reply, "保修期为一年")
        run_agent.assert_called_once()
        kwargs = run_agent.call_args.kwargs
        self.assertEqual(kwargs["tool_names"], {"search_faq"})
        self.assertIn("结构化共享黑板", kwargs["system_context"])
        self.assertEqual(
            [entry["entry_type"] for entry in board.entries],
            ["task_created", "task_dispatched", "task_completed"],
        )

    def test_main_agent_routes_order_question_to_order_child(self):
        main = multi_agent.MainAgent(BlackboardStub())
        child = main.select_child("我的订单现在到哪了")

        self.assertEqual(child.name, "order_agent")

    def test_specialist_serializes_datetime_blackboard_values(self):
        child = multi_agent.SpecialistAgent(
            "knowledge_agent", {"knowledge"}, {"search_faq"}, "处理知识库问题"
        )
        request = multi_agent.AgentMessage(
            message_id="message-1",
            task_id="task-1",
            sender=multi_agent.MAIN_AGENT,
            receiver="knowledge_agent",
            message_type="task_request",
            payload={"question": "保修多久", "username": "alice"},
        )
        blackboard = [{"created_at": datetime(2026, 9, 4, 15, 0, 0)}]

        with patch.object(multi_agent.agent, "run_agent", return_value="保修一年"):
            result = child.handle(request, blackboard)

        self.assertEqual(result.payload["customer_reply"], "保修一年")

    def test_main_agent_marks_child_execution_failure_on_blackboard(self):
        class FailingChild:
            name = "general_agent"
            scenes = set()

            def handle(self, request, blackboard):
                raise RuntimeError("upstream unavailable")

        board = BlackboardStub()
        main = multi_agent.MainAgent(board, (FailingChild(),))

        with self.assertRaises(RuntimeError):
            main.run("一个普通问题", "alice")

        self.assertEqual(board.entries[-1]["entry_type"], "task_rejected")
        self.assertEqual(
            board.entries[-1]["payload"]["reason_code"], "child_execution_failed"
        )


class RouterIntegrationTests(unittest.TestCase):
    def test_router_agent_entry_uses_center_orchestrator(self):
        with patch.object(router.multi_agent, "run", return_value="已处理") as run:
            reply = router._run_agent_safe("查询我的订单", "alice")

        self.assertEqual(reply, "已处理")
        run.assert_called_once_with("查询我的订单", "alice")


class AgentAuditApiTests(unittest.TestCase):
    def test_agent_run_summary_is_admin_only_and_omits_events(self):
        with patch.object(main.db, "list_recent_blackboard_entries", return_value=[
            {"id": 1, "task_id": "task-1", "entry_type": "task_created",
             "source_agent": "main_agent", "payload": {"question": "我的手机号 13812345678 怎么改"},
             "confidence": None, "created_at": "2026-09-04 15:00:00"},
            {"id": 2, "task_id": "task-1", "entry_type": "task_dispatched",
             "source_agent": "main_agent", "payload": {"receiver": "profile_agent"},
             "confidence": None, "created_at": "2026-09-04 15:00:01"},
            {"id": 3, "task_id": "task-1", "entry_type": "task_completed",
             "source_agent": "main_agent", "payload": {"result": "ok"},
             "confidence": None, "created_at": "2026-09-04 15:00:02"},
        ]) as list_entries:
            result = main.list_agent_runs(limit=10, username="admin")

        list_entries.assert_called_once_with(limit=100)
        self.assertEqual(result[0]["specialist"], "profile_agent")
        self.assertEqual(result[0]["status"], "success")
        self.assertNotIn("events", result[0])
        self.assertNotIn("13812345678", result[0]["question_summary"])

    def test_agent_run_detail_requires_existing_task_and_returns_events(self):
        entries = [{
            "id": 1, "task_id": "task-2", "entry_type": "task_created",
            "source_agent": "main_agent", "payload": {"question": "查订单"},
            "confidence": None, "created_at": "2026-09-04 15:00:00",
        }]
        with patch.object(main.db, "list_blackboard_entries", return_value=entries):
            result = main.get_agent_run("task-2", username="admin")

        self.assertEqual(result["task_id"], "task-2")
        self.assertEqual(len(result["events"]), 1)

        with patch.object(main.db, "list_blackboard_entries", return_value=[]):
            with self.assertRaises(main.HTTPException) as context:
                main.get_agent_run("missing", username="admin")
        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
