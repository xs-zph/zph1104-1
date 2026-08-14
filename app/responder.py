"""回复模块：根据分类结果，生成两种类型的回复。

  1. 模板回复（template_reply）：物流查询 / 退货申请 / 退款申请 / 订单查询
     这类问题是标准化的，直接套用固定模板，秒回、零成本。
  2. RAG 回复（rag_reply）：FAQ咨询
     从知识库检索相关条目，再由大模型基于检索内容生成准确回答。
"""
import json
import logging

from app import config, llm, rag
from prompts import chat as chat_prompt
from prompts import respond as respond_prompt

logger = logging.getLogger("app.responder")

_TEMPLATES = None


def _load_templates() -> dict:
    """加载模板回复（只加载一次，缓存到内存）。"""
    global _TEMPLATES
    if _TEMPLATES is None:
        path = config.BASE_DIR / "templates" / "replies.json"
        _TEMPLATES = json.loads(path.read_text(encoding="utf-8"))
    return _TEMPLATES


def template_reply(category: str) -> str:
    """返回某个类别的固定模板回复。"""
    templates = _load_templates()
    reply = templates.get(category)
    if reply is None:
        reply = "您好，您的问题我们已收到，会尽快为您处理。"
    logger.info("使用模板回复（类别=%s）", category)
    return reply


def rag_reply(question: str) -> str:
    """RAG 回复：检索知识库 + 大模型生成回答。"""
    chunks = rag.retrieve(question, top_k=config.Config.RAG_TOP_K)
    logger.info("知识库检索到 %d 条相关条目", len(chunks))

    # 如果知识库里没有任何内容，直接走人工升级提示
    if not chunks:
        return "您好，知识库暂无相关内容，我已为您转接人工客服，请稍候。"

    return llm.complete(
        system=respond_prompt.SYSTEM_PROMPT,
        user=respond_prompt.build_user_prompt(question, chunks),
        max_tokens=512,
    )


def chat_reply(text: str, history: list | None = None) -> str:
    """闲聊回复：调用大模型像真人客服一样打招呼、自我介绍、寒暄。"""
    return llm.complete(
        system=chat_prompt.SYSTEM_PROMPT,
        user=text,
        max_tokens=256,
        temperature=0.7,
        history=history,
    )
