from datetime import datetime

from app import router
from app import responder


def test_smalltalk_only_matches_pure_greeting():
    assert router.is_smalltalk("你好")
    assert router.is_smalltalk("谢谢")
    assert router.is_smalltalk("你是哪个")
    assert router.is_smalltalk("你能做什么")
    assert not router.is_smalltalk("你好，我的耳机坏了")
    assert not router.is_smalltalk("查一下我的订单")


def test_escalation_intent_wins_for_complaint_signals():
    assert router._is_escalate_intent("我要投诉物流")
    assert router._is_escalate_intent("请帮我转人工")
    assert not router._is_escalate_intent("我的快递到哪了")


def test_data_query_detection_excludes_complaints():
    assert router._is_data_query("查一下我的订单")
    assert router._is_data_query("我的物流到哪了")
    assert not router._is_data_query("我要投诉我的订单")
    assert not router._is_data_query("商品质量太差了")
    assert not router._is_data_query("我想退货，还要开票并查物流")
    assert router._is_data_query("我的退款进度")
    assert router._is_data_query("退款什么时候到账")


def test_live_query_uses_deterministic_data_tools(monkeypatch):
    monkeypatch.setattr(router, "_run_data_query_safe", lambda text, username: "物流：运输中")
    monkeypatch.setattr(router, "_run_agent_safe", lambda *args: (_ for _ in ()).throw(AssertionError("不应调用 LLM Agent")))
    monkeypatch.setattr(router.cache, "get", lambda key: (_ for _ in ()).throw(AssertionError("实时查询不应命中旧响应缓存")))
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("我的快递到哪了？", username="user")
    assert result["status"] == "auto"
    assert result["reply_source"] == "agent"
    assert "运输中" in result["reply"]


def test_live_query_failure_is_evaluated_at_data_query_boundary(monkeypatch):
    monkeypatch.setattr(router, "_run_data_query_safe", lambda text, username: None)
    monkeypatch.setattr(router, "_run_agent_safe", lambda *args: "不应调用的旧 Agent 路径")
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("查一下我的订单", username="user")
    assert result["status"] == "auto"
    assert result["reply_source"] == "service_error"
    assert "data_query:degraded" in result["route_trace"]
    assert "重试" in result["reply"]


def test_pure_service_feedback_is_fast_and_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(router, "_classify_safe", lambda *args: (_ for _ in ()).throw(
        AssertionError("纯服务反馈不应调用分类模型")
    ))
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("你回答好慢啊", username="user")
    assert result["status"] == "auto"
    assert result["reply_source"] == "chat"
    assert "久等" in result["reply"]
    assert "service_feedback" in result["route_trace"]


def test_anger_feedback_is_acknowledged_without_rag_or_human_escalation(monkeypatch):
    monkeypatch.setattr(router, "_classify_safe", lambda *args: (_ for _ in ()).throw(
        AssertionError("纯情绪反馈不应调用分类模型")
    ))
    monkeypatch.setattr(router.rag, "best_answer", lambda *args: (_ for _ in ()).throw(
        AssertionError("纯情绪反馈不应进入 RAG")
    ))
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)

    for text in ("我生气了", "我很生气", "气死我了"):
        result = router.process_ticket(text, username="user")
        assert result["status"] == "auto"
        assert result["reply_source"] == "chat"
        assert "生气" in result["reply"]
        assert "系统开小差" not in result["reply"]
        assert "service_feedback" in result["route_trace"]


def test_rag_hit_with_datetime_is_serializable(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.rag, "best_answer", lambda text: {
        "question": "如何退货",
        "answer": "您可以在订单详情中申请退货。",
        "distance": 0.12,
        "created_at": datetime(2026, 9, 10, 15, 0, 0),
    })

    result = router.process_ticket("如何退货", username="user")

    assert result["status"] == "auto"
    assert result["reply_source"] == "rag"
    assert "退货" in result["reply"]
    assert "datetime" not in result["rag_chunks"]
    assert "2026-09-10 15:00:00" in result["rag_chunks"]


def test_service_feedback_with_business_request_keeps_business_flow(monkeypatch):
    monkeypatch.setattr(router, "_run_data_query_safe", lambda text, username: "物流：运输中")
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("你们怎么这么慢，我的快递到哪了？", username="user")
    assert result["status"] == "auto"
    assert result["reply_source"] == "agent"
    assert result["reply"].startswith("抱歉让您久等了")
    assert "运输中" in result["reply"]


def test_refund_request_starts_reason_clarification(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.rag, "best_answer", lambda text: (_ for _ in ()).throw(
        AssertionError("退款澄清不应先走 RAG")
    ))
    result = router.process_ticket("我要退款", username="user")
    assert result["status"] == "auto"
    assert result["reply_source"] == "clarification"
    assert "退款原因" in result["reply"]
    assert [item["value"] for item in result["quick_replies"]] == [
        "未发货，想取消订单",
        "已收到货，想退货退款",
        "商品有质量问题",
        "退款已经申请，查询进度",
        "其他退款问题",
    ]


def test_refund_progress_remains_a_live_query(monkeypatch):
    monkeypatch.setattr(router, "_run_data_query_safe", lambda text, username: "退款状态：审核中")
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("我的退款进度", username="user")
    assert result["reply_source"] == "agent"
    assert "审核中" in result["reply"]
    assert "quick_replies" not in result


def test_refund_option_for_progress_routes_to_live_query(monkeypatch):
    monkeypatch.setattr(router, "_run_data_query_safe", lambda text, username: "退款状态：审核中")
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("退款已经申请，查询进度", username="user")
    assert result["reply_source"] == "agent"
    assert "审核中" in result["reply"]


def test_refund_progress_option_asks_for_order_id_before_querying(monkeypatch):
    history = [{"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："}]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.agent, "_execute_with_mcp_fallback", lambda *args: (_ for _ in ()).throw(
        AssertionError("还没有订单号，不应查询退款")
    ))
    result = router.process_ticket("退款已经申请，查询进度", username="user")
    assert result["reply_source"] == "agent"
    assert "提供订单号" in result["reply"]


def test_refund_order_id_followup_calls_check_refund(monkeypatch):
    history = [
        {"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："},
        {"role": "user", "content": "退款已经申请，查询进度"},
        {"role": "assistant", "content": "好的，我来帮您查询退款进度。请提供订单号。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.agent, "_execute_with_mcp_fallback", lambda name, args, username: (
        '{"found":true,"order_id":"A20240812001","refund_status":"退款处理中"}'
    ))
    result = router.process_ticket("A20240812001", username="user")
    assert result["reply_source"] == "agent"
    assert "退款处理中" in result["reply"]
    assert "refund_skill" in result["route_trace"]


def test_new_refund_application_wins_over_stale_refund_progress_history(monkeypatch):
    history = [
        {"role": "user", "content": "我的退款进度"},
        {"role": "assistant", "content": "您的退款进度：订单 A20240720003 正在处理中"},
        {"role": "user", "content": "我要退款"},
        {"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："},
        {"role": "user", "content": "商品有质量问题"},
        {"role": "assistant", "content": "好的，商品质量问题需要记录订单号和问题描述。请提供订单号，并说明具体故障。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.agent, "_execute_with_mcp_fallback", lambda *args: (
        (_ for _ in ()).throw(AssertionError("新的退款申请不应走退款进度查询"))
    ))
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id,
        "product": "运动跑鞋",
        "status": "已完成",
    })
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "create_after_sale_request", lambda **kwargs: {
        "request_no": "AS-TEST-REFUND-STALE-HISTORY",
        "status": "pending_confirmation",
        "order_id": "A20240720003",
        "request_type": "refund",
        "reason": "商品有质量问题",
    })

    result = router.process_ticket(
        "A20240720003，商品有质量问题",
        username="user",
    )

    assert result["category"] == "退款申请"
    assert result["reply_source"] == "agent"
    assert "确认提交退款申请" in result["reply"]


def test_refund_application_collects_order_and_waits_for_confirmation(monkeypatch):
    history = [
        {"role": "user", "content": "我要退款"},
        {"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："},
        {"role": "user", "content": "商品有质量问题"},
        {"role": "assistant", "content": "请提供订单号，并说明具体故障；如方便，也可以上传照片。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id,
        "product": "无线耳机",
        "status": "已签收",
    })
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "create_after_sale_request", lambda **kwargs: {
        "request_no": "AS-TEST-REFUND",
        "status": "pending_confirmation",
        "order_id": "A20240812001",
        "request_type": "refund",
        "reason": "商品质量问题",
    })
    result = router.process_ticket("A20240812001，左耳无声", username="user")

    assert result["category"] == "退款申请"
    assert result["status"] == "auto"
    assert result["reply_source"] == "agent"
    assert "退款原因" in result["reply"]
    assert "确认提交退款申请" in result["reply"]
    assert "AS-TEST-REFUND" in result["reply"]


def test_refund_application_submits_only_after_confirmation(monkeypatch):
    history = [
        {"role": "user", "content": "A20240812001，左耳无声"},
        {"role": "assistant", "content": "订单 A20240812001 当前可以申请退款，确认提交退款申请吗？"},
    ]
    inserted = []
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: {
        "request_no": "AS-TEST-REFUND",
        "status": "pending_confirmation",
        "order_id": "A20240812001",
        "request_type": "refund",
        "reason": "商品质量问题",
    })
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id,
        "product": "无线耳机",
        "status": "已签收",
    })
    monkeypatch.setattr(router.db, "update_after_sale_request", lambda *args: None)
    monkeypatch.setattr(router.db, "insert_ticket", lambda **kwargs: (
        inserted.append(kwargs) or 701
    ))
    result = router.process_ticket("确认提交退款", username="user")

    assert result["category"] == "退款申请"
    assert result["status"] == "escalated"
    assert result["reply_source"] == "escalate"
    assert "AS-TEST-REFUND" in result["reply"]
    assert "701" in result["reply"]
    assert inserted[0]["category"] == "退款申请"
    assert inserted[0]["status"] == "escalated"


def test_refund_dispute_still_escalates(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("商家拒绝退款，一直不处理", username="user")
    assert result["status"] == "escalated"
    assert result["reply_source"] == "escalate"


def test_cancel_choice_explains_unshipped_and_shipped_paths(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [
        {"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："},
    ])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("未发货，想取消订单", username="user")
    assert "未发货的订单可以直接取消" in result["reply"]
    assert "已发货" in result["reply"]
    assert "提供订单号" in result["reply"]


def test_cancel_order_followup_cancels_unshipped_order(monkeypatch):
    history = [
        {"role": "user", "content": "未发货，想取消订单"},
        {"role": "assistant", "content": "好的，我可以帮您取消订单，请提供订单号，我先帮您核对状态。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "create_after_sale_request", lambda **kwargs: {
        "request_no": "AS-TEST-DRAFT",
        "status": "pending_confirmation",
        "order_id": "A20240815002",
    })
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "空气炸锅", "status": "待发货",
    })
    monkeypatch.setattr(router.agent, "_cancel_order", lambda order_id, username: (
        f"订单 {order_id}（空气炸锅）已成功取消"
    ))
    result = router.process_ticket("A20240815002", username="user")
    assert result["reply_source"] == "agent"
    assert "确认要取消" in result["reply"]
    assert "已成功取消" not in result["reply"]
    assert "cancel_skill" in result["route_trace"]


def test_cancel_order_requires_explicit_confirmation_before_write(monkeypatch):
    history = [
        {"role": "user", "content": "A20240815002，我要取消订单"},
        {"role": "assistant", "content": "订单 A20240815002 尚未发货，确认要取消吗？"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "空气炸锅", "status": "待发货",
    })
    monkeypatch.setattr(router.agent, "_cancel_order", lambda *args: (
        (_ for _ in ()).throw(AssertionError("未确认前不应执行取消写操作"))
    ))
    result = router.process_ticket("我还没想好", username="user")
    assert "确认是否取消" in result["reply"]


def test_cancel_order_confirmation_executes_once(monkeypatch):
    history = [
        {"role": "user", "content": "A20240815002，我要取消订单"},
        {"role": "assistant", "content": "订单 A20240815002 尚未发货，确认要取消吗？"},
    ]
    calls = []
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: {
        "request_no": "AS-TEST-CANCEL",
        "status": "pending_confirmation",
        "order_id": "A20240815002",
    })
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "空气炸锅", "status": "待发货",
    })
    monkeypatch.setattr(router.db, "find_after_sale_by_idempotency", lambda *args: {
        "request_no": "AS-TEST-CANCEL",
        "status": "pending_confirmation",
    })
    monkeypatch.setattr(router.db, "update_after_sale_request", lambda *args: None)
    monkeypatch.setattr(router.agent, "_cancel_order", lambda order_id, username: (
        calls.append((order_id, username)) or f"订单 {order_id}（空气炸锅）已成功取消"
    ))
    result = router.process_ticket("确认取消", username="user")
    assert "已成功取消" in result["reply"]
    assert calls == [("A20240815002", "user")]


def test_cancel_order_followup_for_shipped_order_gives_carrier_guidance(monkeypatch):
    history = [
        {"role": "user", "content": "未发货，想取消订单"},
        {"role": "assistant", "content": "好的，我可以帮您取消订单，请提供订单号，我先帮您核对状态。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "无线耳机", "status": "已发货", "tracking_no": "SF100",
    })
    monkeypatch.setattr(router.agent, "_cancel_order", lambda *args: (_ for _ in ()).throw(
        AssertionError("已发货订单不应执行取消写操作")
    ))
    result = router.process_ticket("A20240812001", username="user")
    assert "已经发货" in result["reply"]
    assert "联系承运商申请拦截" in result["reply"]
    assert "SF100" in result["reply"]


def test_cancel_colloquial_followup_stays_in_cancel_flow(monkeypatch):
    history = [{"role": "assistant", "content": "为了给您匹配正确流程，请先告诉我退款原因："}]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("给我取消", username="user")
    assert result["reply_source"] == "clarification"
    assert "提供订单号" in result["reply"]
    assert "分类器" not in result["reply"]


def test_strong_return_request_asks_for_order_and_reason(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("我强烈要求退货", username="user")
    assert result["reply_source"] == "agent"
    assert "订单号" in result["reply"]
    assert "退货原因" in result["reply"]
    assert "return_skill" in result["route_trace"]


def test_refund_request_is_not_misclassified_as_return(monkeypatch):
    monkeypatch.setattr(router.memory, "get_history", lambda username: [])
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    result = router.process_ticket("我要退款", username="user")
    assert result["reply_source"] == "clarification"
    assert "退款原因" in result["reply"]


def test_return_request_for_shipped_order_gives_interception_guidance(monkeypatch):
    history = [{"role": "assistant", "content": "请提供订单号，并告诉我退货原因。"}]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "耳机", "status": "已发货", "tracking_no": "SF100",
    })
    result = router.process_ticket("A20240812001", username="user")
    assert "申请拦截" in result["reply"]
    assert "拒收" in result["reply"]


def test_return_request_for_signed_order_is_saved_by_outer_ticket_flow(monkeypatch):
    history = [{"role": "assistant", "content": "请提供订单号，并告诉我退货原因。"}]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "create_after_sale_request", lambda **kwargs: {
        "request_no": "AS-TEST-RETURN",
        "status": "pending_confirmation",
        "order_id": "A20240720003",
        "reason": "不合适",
    })
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "空气炸锅", "status": "已签收",
    })
    result = router.process_ticket("A20240720003，商品不合适，我要退货", username="user")
    assert result["status"] == "auto"
    assert result["category"] == "退货申请"
    assert result["reply_source"] == "agent"
    assert "确认提交退货申请" in result["reply"]


def test_return_request_is_submitted_only_after_confirmation(monkeypatch):
    history = [
        {"role": "user", "content": "A20240720003，商品不合适，我要退货"},
        {"role": "assistant", "content": "订单 A20240720003 已签收，确认提交退货申请吗？"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "find_after_sale_by_idempotency", lambda *args: {
        "request_no": "AS-TEST-RETURN",
        "status": "pending_confirmation",
        "order_id": "A20240720003",
        "reason": "不合适",
    })
    monkeypatch.setattr(router.db, "update_after_sale_request", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id, "product": "空气炸锅", "status": "已签收",
    })
    result = router.process_ticket("确认提交退货", username="user")
    assert result["status"] == "escalated"
    assert result["reply_source"] == "escalate"
    assert "已提交" in result["reply"]
    assert "人工审核" in result["reply"]


def test_repair_application_collects_issue_and_waits_for_confirmation(monkeypatch):
    history = [
        {"role": "user", "content": "我的耳机坏了，想申请售后维修"},
        {"role": "assistant", "content": "请提供订单号，并描述具体故障。"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id,
        "product": "无线耳机",
        "status": "已签收",
    })
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: None)
    monkeypatch.setattr(router.db, "create_after_sale_request", lambda **kwargs: {
        "request_no": "AS-TEST-REPAIR",
        "status": "pending_confirmation",
        "order_id": "A20240812001",
        "request_type": "repair",
        "reason": "左耳无声",
    })
    result = router.process_ticket("A20240812001，左耳无声", username="user")

    assert result["category"] == "售后维修"
    assert result["status"] == "auto"
    assert result["reply_source"] == "agent"
    assert "确认提交维修申请" in result["reply"]
    assert "AS-TEST-REPAIR" in result["reply"]


def test_repair_application_submits_unified_request_and_ticket(monkeypatch):
    history = [
        {"role": "user", "content": "A20240812001，左耳无声"},
        {"role": "assistant", "content": "已核对订单，确认提交维修申请吗？"},
    ]
    inserted = []
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: {
        "request_no": "AS-TEST-REPAIR",
        "status": "pending_confirmation",
        "order_id": "A20240812001",
        "request_type": "repair",
        "reason": "左耳无声",
    })
    monkeypatch.setattr(router.db, "get_order_for_user", lambda username, order_id: {
        "order_id": order_id,
        "product": "无线耳机",
        "status": "已签收",
    })
    monkeypatch.setattr(router.db, "update_after_sale_request", lambda *args: None)
    monkeypatch.setattr(router.db, "insert_ticket", lambda **kwargs: (
        inserted.append(kwargs) or 702
    ))
    result = router.process_ticket("确认提交维修", username="user")

    assert result["category"] == "售后维修"
    assert result["status"] == "escalated"
    assert result["reply_source"] == "escalate"
    assert "AS-TEST-REPAIR" in result["reply"]
    assert "702" in result["reply"]
    assert inserted[0]["category"] == "售后维修"


def test_repair_application_does_not_create_duplicate_ticket(monkeypatch):
    history = [
        {"role": "assistant", "content": "已核对订单，确认提交维修申请吗？"},
    ]
    monkeypatch.setattr(router.memory, "get_history", lambda username: history)
    monkeypatch.setattr(router.profile, "schedule_extraction", lambda *args: None)
    monkeypatch.setattr(router.db, "get_active_after_sale", lambda *args: {
        "request_no": "AS-TEST-REPAIR",
        "status": "processing",
        "order_id": "A20240812001",
        "request_type": "repair",
        "reason": "左耳无声",
    })
    monkeypatch.setattr(router.db, "insert_ticket", lambda **kwargs: (
        (_ for _ in ()).throw(AssertionError("处理中不能重复创建维修工单"))
    ))
    result = router.process_ticket("确认提交维修", username="user")

    assert "已提交" in result["reply"]
    assert "不要重复提交" in result["reply"]


def test_chat_reply_has_local_fallback_for_meta_intent(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(responder.llm, "complete", boom)
    reply = responder.chat_reply("你是哪个")
    assert "智能客服" in reply
