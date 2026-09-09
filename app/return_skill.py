"""退货办理 Skill。

区分「询问退货政策」和「实际申请退货」，并承接订单号/退货原因的多轮输入。
订单状态与工单落库由路由层完成，避免把办理动作交给知识库回答。
"""
from app.refund_skill import extract_order_id


STRONG_RETURN_TERMS = (
    "我要退货", "申请退货", "强烈要求退货", "必须退货", "退货退款",
    "我要退", "帮我退货", "退回去", "退掉这个",
)
RETURN_QUESTION_TERMS = ("退货政策", "退货流程", "怎么退货", "退货多久", "能退货吗", "可以退货吗")
RETURN_REASON_TERMS = (
    "不想要", "不喜欢", "拍错", "买错", "尺寸", "不合适", "质量", "损坏",
    "破损", "与描述不符", "描述不符", "发错", "少件", "漏发", "假货",
)


def is_return_request(text: str | None) -> bool:
    """判断是否为实际办理退货，而不是咨询退货政策。"""
    value = (text or "").strip()
    if "退款" in value and "退货" not in value:
        return False
    if any(term in value for term in RETURN_QUESTION_TERMS):
        return False
    return any(term in value for term in STRONG_RETURN_TERMS)


def is_return_followup(text: str | None, history: list[dict] | None) -> bool:
    """判断当前消息是否在退货办理中补充订单号或退货原因。"""
    value = " ".join(str(item.get("content") or "") for item in (history or [])[-6:])
    if "退款" in value and "退货" not in value:
        return False
    return "退货" in value and any(
        marker in value for marker in ("提供订单号", "退货原因", "核对订单")
    ) and bool(extract_order_id(text) or any(term in (text or "") for term in RETURN_REASON_TERMS))


def extract_reason(text: str | None) -> str | None:
    """提取常见退货原因；没有命中时由客服继续追问。"""
    value = text or ""
    for term in RETURN_REASON_TERMS:
        if term in value:
            return term
    return None
