"""统一售后流程的纯业务规则。

本模块不访问数据库，也不执行订单写操作。路由层负责收集信息，
数据库层负责持久化，真正的业务动作仍由服务端确定性函数执行。
"""

from __future__ import annotations

import hashlib
import re


REQUEST_TYPES = frozenset({"cancel", "return", "refund", "repair"})
STATUSES = frozenset({
    "draft",
    "pending_confirmation",
    "processing",
    "completed",
    "rejected",
    "cancelled",
    "failed",
})

CONFIRM_TERMS = (
    "确认",
    "确定",
    "提交申请",
    "提交",
    "继续",
    "好的",
    "好",
    "是的",
    "同意",
)
REJECT_TERMS = (
    "取消",
    "算了",
    "不用了",
    "不提交",
    "不申请",
    "暂时不要",
    "否",
)

ORDER_ID_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{3,63}\b")

STATUS_LABELS = {
    "draft": "信息收集中",
    "pending_confirmation": "等待确认",
    "processing": "处理中",
    "completed": "已完成",
    "rejected": "已拒绝",
    "cancelled": "已取消",
    "failed": "处理失败，可重试",
}

REQUEST_TYPE_LABELS = {
    "cancel": "取消订单",
    "return": "退货申请",
    "refund": "退款申请",
    "repair": "售后维修",
}

AUDIT_ACTION_LABELS = {
    "after_sale_annotation": "更新售后标注",
    "after_sale_submitted": "提交售后申请",
    "after_sale_completed": "完成取消订单",
    "status_change": "工单状态变更",
    "human_reply": "人工回复",
    "feedback_tag": "添加反馈标签",
}

ANNOTATION_TYPES = frozenset({"cancel", "return", "refund", "exchange", "repair"})

ANNOTATION_TYPE_LABELS = {
    "cancel": "取消订单",
    "return": "退货申请",
    "refund": "退款申请",
    "exchange": "换货申请",
    "repair": "售后维修",
}

ANNOTATION_STAGES = frozenset({
    "pending_info",
    "pending_order_check",
    "pending_customer_confirmation",
    "submitted",
    "processing",
    "completed",
    "closed",
})

ANNOTATION_STAGE_LABELS = {
    "pending_info": "待补充信息",
    "pending_order_check": "待核对订单",
    "pending_customer_confirmation": "待用户确认",
    "submitted": "已提交售后",
    "processing": "处理中",
    "completed": "已完成",
    "closed": "已关闭",
}

ORDER_STATUS_LABELS = {
    "待发货": "待发货",
    "已发货": "已发货",
    "运输中": "运输中",
    "已签收": "已签收",
    "已完成": "已完成",
    "已退货": "已退货",
    "已取消": "已取消",
}


def extract_order_id(text: str | None) -> str | None:
    """提取带数字的订单号，避免把自然语言传给业务写操作。"""
    for candidate in ORDER_ID_RE.findall(text or ""):
        if any(char.isdigit() for char in candidate):
            return candidate
    return None


def is_confirmation(text: str | None) -> bool:
    """判断是否是对上一轮售后确认的明确同意。"""
    value = (text or "").strip().lower()
    if not value:
        return False
    explicit_terms = (
        "确认取消",
        "确定取消",
        "确认提交",
        "确定提交",
        "确认申请",
        "确定申请",
        "我确认",
        "我确定",
        "确定要",
        "确认要",
        "同意提交",
        "同意申请",
    )
    if any(term in value for term in explicit_terms):
        return True
    normalized = value.strip("，。,.!?！？ ")
    return normalized in {
        "确认",
        "确定",
        "提交申请",
        "提交",
        "继续",
        "好的",
        "好",
        "是的",
        "同意",
    }


def is_rejection(text: str | None) -> bool:
    """判断客户是否取消当前售后草稿。"""
    value = (text or "").strip().lower()
    return bool(value) and not is_confirmation(value) and any(
        term in value for term in REJECT_TERMS
    )


def has_confirmation_prompt(history: list[dict] | None, request_type: str) -> bool:
    """从最近对话中判断是否存在同类待确认请求。"""
    if request_type not in REQUEST_TYPES:
        return False
    markers = {
        "cancel": ("确认要取消", "确认取消"),
        "return": ("确认提交退货", "确认申请退货"),
        "refund": ("确认提交退款", "确认申请退款"),
        "repair": ("确认提交售后", "确认提交维修", "确认申请维修"),
    }[request_type]
    return any(
        item.get("role") == "assistant"
        and any(marker in str(item.get("content") or "") for marker in markers)
        for item in (history or [])[-8:]
    )


def latest_confirmation_type(history: list[dict] | None) -> str | None:
    """返回最近一条售后确认提示对应的业务类型。"""
    for item in reversed((history or [])[-8:]):
        if item.get("role") != "assistant":
            continue
        content = str(item.get("content") or "")
        for request_type in REQUEST_TYPES:
            if has_confirmation_prompt([item], request_type):
                return request_type
    return None


def has_conflicting_request_term(text: str | None, request_type: str) -> bool:
    """判断当前消息是否明确在确认另一种售后业务。"""
    value = (text or "").strip().lower()
    if request_type == "cancel":
        return any(term in value for term in (
            "退货", "退款", "维修", "售后", "物流", "快递",
            "查订单", "订单状态", "退款进度", "售后申请进度",
        ))
    if request_type == "return":
        return any(term in value for term in (
            "取消订单", "取消", "退款", "维修", "售后", "物流", "快递",
            "查订单", "订单状态", "退款进度", "售后申请进度",
        ))
    if request_type == "refund":
        return any(term in value for term in ("取消订单", "退货", "维修"))
    if request_type == "repair":
        return any(term in value for term in ("取消订单", "退货", "退款"))
    return False


def is_confirmation_for(text: str | None, request_type: str) -> bool:
    """判断确认语句是否与指定售后类型一致。"""
    if has_conflicting_request_term(text, request_type):
        return False
    return is_confirmation(text)


def idempotency_key(
    username: str,
    request_type: str,
    order_id: str,
    reason: str | None = None,
) -> str:
    """为同一用户的同一售后意图生成稳定幂等键。"""
    raw = "|".join([
        (username or "").strip().lower(),
        (request_type or "").strip().lower(),
        (order_id or "").strip().upper(),
        " ".join((reason or "").strip().lower().split()),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", "处理中")


def public_request(request: dict) -> dict:
    """转换为客户可见的售后记录，不暴露内部用户字段和数据库 id。"""
    request_type = request.get("request_type")
    return {
        "request_no": request.get("request_no"),
        "request_type": request_type,
        "request_type_label": REQUEST_TYPE_LABELS.get(request_type, "售后申请"),
        "order_id": request.get("order_id"),
        "status": request.get("status"),
        "status_label": status_label(request.get("status")),
        "reason": request.get("reason"),
        "result": request.get("result"),
        "created_at": request.get("created_at"),
        "updated_at": request.get("updated_at"),
        "completed_at": request.get("completed_at"),
        "attempt_count": request.get("attempt_count", 0),
        "last_attempt_status": request.get("last_attempt_status"),
        "last_attempt_error": request.get("last_attempt_error"),
    }


def public_staff_request(request: dict) -> dict:
    """转换客服/经理查询使用的售后记录，附带来源工单摘要。"""
    result = public_request(request)
    result.update(
        {
            "ticket_id": request.get("ticket_id"),
            "source_ticket_text": request.get("source_ticket_text"),
            "source_ticket_status": request.get("source_ticket_status"),
            "source_assigned_to": request.get("source_assigned_to"),
            "last_attempt_operator": request.get("last_attempt_operator"),
        }
    )
    return result


def public_attempt(attempt: dict) -> dict:
    """转换售后执行尝试，隐藏客户身份和数据库内部 id。"""
    return {
        "request_no": attempt.get("request_no"),
        "attempt_no": attempt.get("attempt_no"),
        "status": attempt.get("status"),
        "result": attempt.get("result"),
        "error_code": attempt.get("error_code"),
        "error_message": attempt.get("error_message"),
        "operator": attempt.get("operator"),
        "created_at": attempt.get("created_at"),
        "updated_at": attempt.get("updated_at"),
    }


def public_audit_log(log: dict) -> dict:
    """转换工单审计日志，统一为客服可读的动作名称。"""
    action = log.get("action")
    return {
        "id": log.get("id"),
        "action": action,
        "action_label": AUDIT_ACTION_LABELS.get(action, action or "系统记录"),
        "detail": log.get("detail"),
        "operator": log.get("operator"),
        "created_at": log.get("created_at"),
    }


def public_annotation(annotation: dict | None) -> dict | None:
    """转换客服工作台使用的售后标注，保留业务字段但不暴露数据库内部 id。"""
    if not annotation:
        return None
    request_type = annotation.get("request_type")
    stage = annotation.get("stage")
    return {
        "ticket_id": annotation.get("ticket_id"),
        "request_type": request_type,
        "request_type_label": ANNOTATION_TYPE_LABELS.get(request_type, "售后事项"),
        "stage": stage,
        "stage_label": ANNOTATION_STAGE_LABELS.get(stage, "处理中"),
        "order_id": annotation.get("order_id"),
        "product": annotation.get("product"),
        "reason": annotation.get("reason"),
        "item_status": annotation.get("item_status"),
        "note": annotation.get("note"),
        "operator": annotation.get("operator"),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
    }


def public_order_context(order: dict | None) -> dict | None:
    """转换客服订单核验卡，只暴露处理售后所需的业务字段。"""
    if not order:
        return None
    status = order.get("status")
    context = {
        "order_id": order.get("order_id"),
        "product": order.get("product"),
        "status": status,
        "status_label": ORDER_STATUS_LABELS.get(status, status or "未知"),
        "tracking_no": order.get("tracking_no"),
        "logistics": order.get("logistics"),
        "refund_status": order.get("refund_status"),
    }
    if status in {"待发货", "已发货", "运输中", "已签收", "已完成", "已退货", "已取消"}:
        context["handling_hint"] = {
            "待发货": "尚未发货，可继续核对取消或售后规则",
            "已发货": "已发货，取消需转为物流拒收或签收后退货流程",
            "运输中": "运输中，建议核对拒收或签收后退货流程",
            "已签收": "已签收，请继续核对退货时效和商品状态",
            "已完成": "订单已完成，请核对售后规则和商品状态",
            "已退货": "订单已退货，请避免重复提交退货",
            "已取消": "订单已取消，请核对是否还存在退款事项",
        }[status]
    if order.get("refund_status"):
        context["refund_hint"] = "该订单已有退款记录，请先核对退款进度，避免重复申请"
    return context
