import unittest
from unittest.mock import MagicMock, patch

from app import agent, profile, router, skills


class ProfileMemoryTests(unittest.TestCase):
    def test_extract_filters_unknown_and_low_confidence_facts(self):
        result = {
            "facts": [
                {
                    "entity_type": "device",
                    "fact_key": "preferred_device",
                    "fact_value": "无线蓝牙耳机",
                    "confidence": 0.95,
                    "confirmed": True,
                },
                {
                    "entity_type": "person",
                    "fact_key": "password",
                    "fact_value": "secret",
                    "confidence": 1,
                    "confirmed": True,
                },
                {
                    "entity_type": "preference",
                    "fact_key": "favorite_category",
                    "fact_value": "厨房小家电",
                    "confidence": 0.3,
                    "confirmed": False,
                },
            ]
        }
        with patch.object(profile.llm, "complete", return_value=result), \
             patch.object(profile, "get_context", return_value=""), \
             patch.object(profile.db, "get_profile_fact", return_value=None), \
             patch.object(profile.db, "upsert_profile_fact") as upsert:
            profile._extract_and_save("user", "我常用无线蓝牙耳机", "好的")

        upsert.assert_called_once_with(
            username="user",
            entity_type="device",
            fact_key="preferred_device",
            fact_value="无线蓝牙耳机",
            source="conversation",
            evidence="我常用无线蓝牙耳机",
            confidence=0.95,
            confirmed=True,
        )

    def test_schedule_skips_anonymous_user_and_submits_logged_in_user(self):
        executor = MagicMock()
        with patch.object(profile, "_executor", executor):
            profile.schedule_extraction(None, "我喜欢耳机", "记住了")
            profile.schedule_extraction("user", "我喜欢耳机", "记住了")

        executor.submit.assert_called_once_with(
            profile._extract_and_save, "user", "我喜欢耳机", "记住了"
        )

    def test_agent_receives_profile_card(self):
        with patch.object(agent.profile, "get_context_for_question", return_value="- device.preferred_device = 耳机"), \
             patch.object(agent.llm, "complete_with_tools", return_value="收到") as complete:
            reply = agent.run_agent("我平时用什么设备？", "user")

        self.assertEqual(reply, "收到")
        system = complete.call_args.kwargs["system"]
        self.assertIn("用户实体画像卡片", system)
        self.assertIn("device.preferred_device = 耳机", system)

    def test_skill_recall_is_limited_to_current_scene(self):
        logistics = [spec.name for spec in skills.retrieve("我的快递到哪了？")]
        cancel = [spec.name for spec in skills.retrieve("我想取消订单 A100")]

        self.assertIn("query_logistics", logistics)
        self.assertNotIn("get_weather", logistics)
        self.assertIn("cancel_order", cancel)

    def test_agent_passes_scene_tools_only(self):
        with patch.object(agent.profile, "get_context_for_question", return_value=""), \
             patch.object(agent.llm, "complete_with_tools", return_value="收到") as complete:
            agent.run_agent("我的快递到哪了？", "user")

        tool_names = {
            tool["function"]["name"]
            for tool in complete.call_args.kwargs["tools"]
        }
        self.assertIn("query_logistics", tool_names)
        self.assertNotIn("get_weather", tool_names)
        self.assertLess(len(tool_names), len(agent.TOOLS))

    def test_context_includes_subject_and_matching_order_only(self):
        facts = [{
            "entity_type": "device", "fact_key": "preferred_device",
            "fact_value": "无线蓝牙耳机",
        }]
        orders = [
            {"order_id": "A20240812001", "product": "无线蓝牙耳机", "status": "已发货", "logistics": "运输中"},
            {"order_id": "A20240815002", "product": "智能空气炸锅", "status": "待发货"},
        ]
        with patch.object(profile.db, "list_profile_facts", return_value=facts), \
             patch.object(profile.db, "list_orders_for_user", return_value=orders), \
             patch.object(profile.db, "list_tickets_for_user") as tickets:
            context = profile.get_context_for_question("user", "订单 A20240812001 的物流到哪了？")

        self.assertIn("【主体事实】", context)
        self.assertIn("A20240812001", context)
        self.assertNotIn("A20240815002", context)
        tickets.assert_not_called()

    def test_smalltalk_context_does_not_include_business_objects(self):
        facts = [{"entity_type": "person", "fact_key": "name", "fact_value": "小王"}]
        with patch.object(profile.db, "list_profile_facts", return_value=facts), \
             patch.object(profile.db, "list_orders_for_user") as orders, \
             patch.object(profile.db, "list_tickets_for_user") as tickets:
            context = profile.get_context_for_question("user", "你好呀")

        self.assertIn("小王", context)
        self.assertNotIn("当前相关客体", context)
        orders.assert_not_called()
        tickets.assert_not_called()

    def test_router_remembers_turn_and_schedules_profile_extraction(self):
        with patch.object(router.memory, "append") as append, \
             patch.object(router.profile, "schedule_extraction") as schedule:
            router._remember_and_schedule("user", "我喜欢耳机", "好的，我记住了")

        self.assertEqual(append.call_count, 2)
        append.assert_any_call("user", "user", "我喜欢耳机")
        append.assert_any_call("user", "assistant", "好的，我记住了")
        schedule.assert_called_once_with("user", "我喜欢耳机", "好的，我记住了")


if __name__ == "__main__":
    unittest.main()
