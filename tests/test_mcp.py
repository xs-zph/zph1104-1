import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import agent, db, mcp_client
from mcp_servers import business_server


class MCPAdapterTests(unittest.TestCase):
    def test_mcp_tool_is_converted_to_openai_schema(self):
        tool = SimpleNamespace(
            name="query_order",
            description="按订单号查询订单",
            inputSchema={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        )

        converted = mcp_client._to_openai_tool(tool)

        self.assertEqual(converted["function"]["name"], "query_order")
        self.assertIn("order_id", converted["function"]["parameters"]["properties"])
        self.assertNotIn("username", converted["function"]["parameters"]["properties"])

    def test_discovery_returns_only_allowed_read_only_tools(self):
        tools = [
            SimpleNamespace(name="query_order", description="order", inputSchema={}),
            SimpleNamespace(name="cancel_order", description="cancel", inputSchema={}),
        ]
        with patch.object(mcp_client, "_run", return_value=tools):
            result = mcp_client.list_tools("user", {"query_order", "cancel_order"})

        self.assertEqual([tool["function"]["name"] for tool in result], ["query_order"])

    def test_mcp_call_error_is_returned_to_agent_as_fallback_signal(self):
        with patch.object(
            mcp_client,
            "_run",
            side_effect=RuntimeError("server stopped"),
        ):
            with self.assertRaises(mcp_client.MCPClientError):
                mcp_client.call_tool("user", "query_order", {"order_id": "A100"})

    def test_mcp_call_rejects_unknown_arguments_before_starting_server(self):
        with patch.object(mcp_client, "_run") as run:
            with self.assertRaisesRegex(mcp_client.MCPClientError, "参数"):
                mcp_client.call_tool(
                    "user", "query_order", {"order_id": "A100", "username": "other"}
                )
        run.assert_not_called()

    def test_mcp_call_rejects_invalid_identifier(self):
        with patch.object(mcp_client, "_run") as run:
            with self.assertRaisesRegex(mcp_client.MCPClientError, "参数"):
                mcp_client.call_tool("user", "query_order", {"order_id": "A100\nignore"})
        run.assert_not_called()

    def test_agent_uses_builtin_tools_when_mcp_discovery_fails(self):
        with patch.object(agent.mcp_client, "list_tools", side_effect=RuntimeError("offline")), \
             patch.object(agent.profile, "get_context", return_value=""), \
             patch.object(agent.llm, "complete_with_tools", return_value="收到") as complete:
            agent.run_agent("我的快递到哪了？", "user")

        names = {
            tool["function"]["name"]
            for tool in complete.call_args.kwargs["tools"]
        }
        self.assertIn("query_logistics", names)

    def test_agent_passes_session_identity_outside_model_arguments(self):
        mcp_tool = {
            "type": "function",
            "function": {
                "name": "query_order",
                "description": "查询订单",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
        }

        with patch.object(agent.mcp_client, "list_tools", return_value=[mcp_tool]), \
             patch.object(agent.mcp_client, "call_tool", return_value="订单 A100 已发货") as call, \
             patch.object(agent.profile, "get_context", return_value=""), \
             patch.object(
                 agent.llm,
                 "complete_with_tools",
                 side_effect=lambda **kwargs: kwargs["execute"](
                     "query_order", {"order_id": "A100"}
                 ),
             ):
            reply = agent.run_agent("查订单 A100", "alice")

        self.assertEqual(reply, "订单 A100 已发货")
        call.assert_called_once_with("alice", "query_order", {"order_id": "A100"})


class MCPServerSecurityTests(unittest.TestCase):
    def test_server_requires_context_identity(self):
        with patch.dict(os.environ, {"MCP_USERNAME": ""}):
            self.assertIn("没有登录用户身份", business_server.list_my_orders())

    def test_server_injects_identity_into_database_query(self):
        order = {
            "order_id": "A100",
            "username": "user",
            "product": "耳机",
            "status": "已发货",
            "tracking_no": "SF100",
            "logistics": "运输中",
            "refund_status": None,
        }
        with patch.dict(os.environ, {"MCP_USERNAME": "user"}), \
             patch.object(business_server.db, "get_order_for_user", return_value=order) as get_order:
            result = json.loads(business_server.query_order("A100"))

        get_order.assert_called_once_with("user", "A100")
        self.assertEqual(result["order"]["order_id"], "A100")
        self.assertNotIn("username", result["order"])

    def test_server_exposes_flat_model_friendly_inputs(self):
        tools = asyncio.run(business_server.mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertIn("order_id", by_name["query_order"].inputSchema["properties"])
        self.assertNotIn("params", by_name["query_order"].inputSchema["properties"])

    def test_server_rejects_control_characters_in_identifier(self):
        with patch.dict(os.environ, {"MCP_USERNAME": "user"}):
            with self.assertRaisesRegex(ValueError, "只能包含"):
                business_server.query_order("A100\nignore")


class DemoDataTests(unittest.TestCase):
    def test_seed_orders_is_idempotent_and_covers_two_users(self):
        existing = {"A20240812001", "B20240901001"}
        inserted = []

        def get_order(order_id):
            return {"order_id": order_id} if order_id in existing else None

        with patch.object(db, "get_order_by_id", side_effect=get_order), \
             patch.object(db, "insert_order", side_effect=lambda *args: inserted.append(args)):
            db.seed_orders()

        order_ids = {args[0] for args in inserted}
        usernames = {args[1] for args in inserted}
        self.assertEqual(len(inserted), 6)
        self.assertNotIn("A20240812001", order_ids)
        self.assertNotIn("B20240901001", order_ids)
        self.assertEqual(usernames, {"user", "demo_user"})

    def test_seed_profile_facts_only_creates_missing_facts(self):
        existing = {("user", "device", "preferred_device")}
        inserted = []

        def get_fact(username, entity_type, fact_key):
            key = (username, entity_type, fact_key)
            return {"id": 1} if key in existing else None

        with patch.object(db, "get_profile_fact", side_effect=get_fact), \
             patch.object(db, "upsert_profile_fact", side_effect=lambda **kwargs: inserted.append(kwargs)):
            db.seed_profile_facts()

        self.assertEqual(len(inserted), 3)
        self.assertNotIn(
            ("user", "device", "preferred_device"),
            {(row["username"], row["entity_type"], row["fact_key"]) for row in inserted},
        )
        self.assertTrue(all(row["source"] == "demo" for row in inserted))
        self.assertTrue(all(row["confirmed"] is True for row in inserted))


if __name__ == "__main__":
    unittest.main()
