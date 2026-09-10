"""售后维修多轮流程的纯业务识别规则。"""

from __future__ import annotations

from app.refund_skill import extract_order_id


REPAIR_TERMS = (
    "维修",
    "报修",
    "售后",
    "故障",
    "坏了",
    "损坏",
    "无法开机",
    "有杂音",
    "不工作",
    "不能用",
)

REPAIR_REASON_TERMS = (
    "无法开机",
    "开不了机",
    "有杂音",
    "没有声音",
    "左耳无声",
    "右耳无声",
    "充不进电",
    "电池不耐用",
    "屏幕碎了",
    "屏幕破裂",
    "损坏",
    "破损",
    "坏了",
    "不工作",
    "不能用",
    "无法使用",
)

REPAIR_APPLICATION_TERMS = (
    "申请售后",
    "申请维修",
    "申请报修",
    "我要维修",
    "我要报修",
    "帮我维修",
    "帮我报修",
    "办理维修",
    "提交维修",
    "强烈要求售后",
)

REPAIR_QUESTION_TERMS = (
    "怎么维修",
    "如何维修",
    "维修流程",
    "维修政策",
    "能维修吗",
    "可以维修吗",
    "维修多久",
    "维修要多久",
    "怎么申请维修",
    "如何申请维修",
)


def is_repair_request(text: str | None) -> bool:
    """判断客户是否要办理维修，而不是只咨询维修政策。"""
    value = (text or "").strip()
    if any(term in value for term in REPAIR_QUESTION_TERMS):
        return any(term in value for term in REPAIR_APPLICATION_TERMS)
    return any(term in value for term in REPAIR_APPLICATION_TERMS)


def is_repair_followup(text: str | None, history: list[dict] | None) -> bool:
    """判断当前消息是否在维修办理中补充订单号或故障描述。"""
    recent = " ".join(
        str(item.get("content") or "") for item in (history or [])[-8:]
    )
    if not any(term in recent for term in REPAIR_TERMS):
        return False
    return any(
        marker in recent
        for marker in ("提供订单号", "故障描述", "具体故障", "核对订单")
    ) and bool(extract_order_id(text) or extract_issue(text))


def extract_issue(text: str | None) -> str | None:
    """提取常见故障描述；未知描述保留给路由层继续追问。"""
    value = text or ""
    for term in REPAIR_REASON_TERMS:
        if term in value:
            return term
    return None
