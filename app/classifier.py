"""工单分类模块：调用大模型，把工单文本分到 7 个类别之一。

对外只暴露一个函数 classify()，返回 {category, confidence, reason}。
这里做了两层兜底，保证系统稳定：
  1. confidence 限制在 0~1 之间；
  2. 如果模型返回的类别不在 7 类之内，退回到「其他」。
"""
import logging

from app import categories, llm
from prompts import classify as classify_prompt

logger = logging.getLogger("app.classifier")


def classify(ticket_text: str) -> dict:
    """对一条工单文本进行分类。

    返回：{"category": str, "confidence": float, "reason": str}
    """
    result = llm.complete(
        system=classify_prompt.SYSTEM_PROMPT,
        user=classify_prompt.build_user_prompt(ticket_text),
        json_mode=True,
        max_tokens=512,
    )

    category = str(result.get("category", "其他")).strip()
    reason = str(result.get("reason", "")).strip()

    # confidence 限制到 0~1 之间
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # 类别兜底
    if category not in categories.CATEGORIES:
        logger.warning("模型返回了未知类别 %r，已回退为「其他」", category)
        category = "其他"

    logger.info("分类结果：类别=%s，置信度=%.2f", category, confidence)
    return {"category": category, "confidence": confidence, "reason": reason}
