"""回复模块：闲聊回复（问候、寒暄、自我介绍）。

说明：模板回复 / RAG 回复已随 2.0 三级链路重构移除——
  - RAG 优先由 app/rag.py 的 best_answer() + router 直接返回，不再走大模型；
  - 业务问题统一由 Agent（大模型 + 工具）处理。
"""
import logging

from app import llm
from prompts import chat as chat_prompt

logger = logging.getLogger("app.responder")


def chat_reply(text: str, history: list | None = None) -> str:
    """闲聊回复：调用大模型像真人客服一样打招呼、自我介绍、寒暄。"""
    return llm.complete(
        system=chat_prompt.SYSTEM_PROMPT,
        user=text,
        max_tokens=256,
        temperature=0.7,
        history=history,
    )
