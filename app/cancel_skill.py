"""取消订单多轮流程 Skill。

只负责识别「取消订单 + 订单号」的上下文，实际状态读取和取消动作
仍由数据库/Agent 工具完成，避免把有副作用的取消操作交给只读 MCP。
"""
from app.refund_skill import extract_order_id


def is_cancel_order_followup(text: str | None, history: list[dict] | None) -> bool:
    """判断当前消息是否是在取消订单流程中补充订单号。"""
    if not extract_order_id(text):
        return False
    value = " ".join(str(item.get("content") or "") for item in (history or [])[-6:])
    return "取消订单" in value and any(
        marker in value for marker in ("请提供订单号", "告诉我订单号", "订单号")
    )


def is_direct_cancel_request(text: str | None) -> bool:
    """识别同一条消息中同时包含取消意图和订单号的请求。"""
    value = text or ""
    return bool(extract_order_id(value) and any(k in value for k in ("取消订单", "取消", "不要了")))
