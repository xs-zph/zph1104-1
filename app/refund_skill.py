"""退款多轮流程 Skill。

负责识别退款流程中的实体和上下文，不直接访问数据库或 MCP。
工具执行仍统一交给 Agent，确保身份校验和降级策略只有一个入口。
"""
import re


ORDER_ID_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{3,63}\b")
REFUND_TERMS = ("退款", "退钱", "退货退款", "退款进度", "退款状态", "退款到账")


def extract_order_id(text: str | None) -> str | None:
    """从客户消息中提取订单号，避免把自然语言交给工具参数校验。"""
    for candidate in ORDER_ID_RE.findall(text or ""):
        if any(char.isdigit() for char in candidate):
            return candidate
    return None


def is_refund_order_followup(text: str | None, history: list[dict] | None) -> bool:
    """判断当前消息是否是退款流程中补充的订单号。"""
    if not extract_order_id(text):
        return False
    recent = (history or [])[-6:]
    recent_text = " ".join(str(item.get("content") or "") for item in recent)
    if not any(term in recent_text for term in REFUND_TERMS):
        return False
    return any(
        marker in recent_text
        for marker in ("请提供订单号", "告诉我订单号", "订单号，并", "输入订单号")
    )


def needs_order_id_for_refund_choice(text: str | None, history: list[dict] | None) -> bool:
    """识别客户刚选择「查询退款进度」，需要先收集订单号。"""
    value = text or ""
    if "退款已经申请" not in value or "查询进度" not in value:
        return False
    return any(
        item.get("role") == "assistant" and "退款原因" in str(item.get("content") or "")
        for item in (history or [])[-4:]
    )
