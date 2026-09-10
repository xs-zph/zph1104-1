from app import after_sale


def test_confirmation_phrases_have_priority_over_cancel_word():
    assert after_sale.is_confirmation("确认取消")
    assert after_sale.is_confirmation("确定提交退货")
    assert not after_sale.is_confirmation("取消")
    assert after_sale.is_rejection("暂时不要")


def test_confirmation_rejects_cross_flow_terms():
    assert not after_sale.is_confirmation_for("确认退货", "cancel")
    assert not after_sale.is_confirmation_for("确认取消", "return")
    assert after_sale.is_confirmation_for("确认取消", "cancel")
    assert after_sale.is_confirmation_for("确认提交退货", "return")
    assert after_sale.is_confirmation_for("确认提交维修", "repair")


def test_idempotency_key_is_stable_and_sensitive_to_business_fields():
    first = after_sale.idempotency_key("User", "cancel", "A100")
    second = after_sale.idempotency_key("user", "cancel", "a100")
    other_reason = after_sale.idempotency_key("user", "return", "A100", "不合适")

    assert first == second
    assert first != other_reason


def test_public_request_hides_internal_fields():
    public = after_sale.public_request({
        "id": 9,
        "username": "alice",
        "request_no": "AS202609100001",
        "request_type": "return",
        "order_id": "A100",
        "status": "processing",
        "reason": "不合适",
        "result": "等待人工审核",
        "created_at": "2026-09-10 10:00:00",
        "updated_at": "2026-09-10 10:01:00",
        "last_attempt_operator": "agent",
    })

    assert public["request_type_label"] == "退货申请"
    assert public["status_label"] == "处理中"
    assert "username" not in public
    assert "id" not in public
    assert "last_attempt_operator" not in public


def test_public_staff_request_hides_customer_identity_but_keeps_source_summary():
    public = after_sale.public_staff_request({
        "id": 9,
        "username": "alice",
        "ticket_id": 7,
        "request_no": "AS202609100001",
        "request_type": "refund",
        "order_id": "A100",
        "status": "processing",
        "source_ticket_text": "用户申请退款",
        "source_ticket_status": "in_progress",
        "source_assigned_to": "agent",
        "attempt_count": 2,
        "last_attempt_status": "failed",
        "last_attempt_error": "退款审核工单创建失败",
        "last_attempt_operator": "agent",
    })

    assert public["request_type_label"] == "退款申请"
    assert public["source_ticket_status"] == "in_progress"
    assert public["source_assigned_to"] == "agent"
    assert public["ticket_id"] == 7
    assert public["attempt_count"] == 2
    assert public["last_attempt_status"] == "failed"
    assert public["last_attempt_error"] == "退款审核工单创建失败"
    assert "username" not in public
    assert "customer_username" not in public
    assert "id" not in public


def test_public_audit_log_uses_business_action_label():
    public = after_sale.public_audit_log({
        "id": 3,
        "action": "after_sale_submitted",
        "detail": "已提交退款申请 AS-1",
        "operator": "agent",
        "created_at": "2026-09-10 10:00:00",
    })

    assert public["action_label"] == "提交售后申请"
    assert public["operator"] == "agent"
