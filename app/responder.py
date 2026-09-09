"""回复模块：闲聊回复（问候、寒暄、自我介绍）。

说明：模板回复 / RAG 回复已随 2.0 三级链路重构移除——
  - RAG 优先由 app/rag.py 的 best_answer() + router 直接返回，不再走大模型；
  - 业务问题统一由 Agent（大模型 + 工具）处理。
"""
import logging

from app import llm
from prompts import chat as chat_prompt

logger = logging.getLogger("app.responder")


def _fallback_chat_reply(text: str) -> str:
    """本地兜底闲聊回复：模型不可用时保证用户仍有可读反馈。"""
    t = (text or "").strip()
    if any(k in t for k in ("你是谁", "你叫什么", "你是哪个", "你是哪个机器人", "介绍一下", "介绍你自己", "自我介绍")):
        return "我是智能客服，可以帮您查询订单、物流、退款、售后和知识库问题。"
    if any(k in t for k in ("你能做什么", "你能干什么", "你能帮我什么", "有什么功能", "你会什么", "你可以做什么")):
        return "我可以帮您查订单、物流、退款、售后，也可以回答常见政策问题。"
    if any(k in t for k in ("谢谢", "感谢", "辛苦了")):
        return "不客气，很高兴帮到您。"
    return "我在的，可以继续告诉我您的问题。"


def chat_reply(text: str, history: list | None = None,
               profile_context: str = "") -> str:
    """闲聊回复：调用大模型像真人客服一样打招呼、自我介绍、寒暄。"""
    system = chat_prompt.SYSTEM_PROMPT
    if profile_context:
        system += (
            "\n\n【用户实体画像卡片】\n"
            "以下是用户明确确认或高置信度事实；自然交流时可以使用，但当前消息有新说法时以当前消息为准。\n"
            + profile_context
        )
    try:
        return llm.complete(
            system=system,
            user=text,
            max_tokens=256,
            temperature=0.7,
            history=history,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("闲聊回复失败，使用本地兜底：%s", exc)
        return _fallback_chat_reply(text)
