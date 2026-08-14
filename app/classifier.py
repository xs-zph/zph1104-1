"""工单分类模块：调用大模型，把工单文本分到 7 个类别之一，并同时识别情绪与多诉求。

对外只暴露一个函数 classify()，返回：
  {category, confidence, reason, emotion, emotion_intensity, multi_intent}
在「同一次大模型调用」里同时完成分类 + 情绪识别 + 多诉求判断，避免额外
一次 LLM 调用拖慢回复（此前用户反馈过速度问题）。

这里做了多层兜底，保证系统稳定：
  1. confidence 限制在 0~1 之间；
  2. 类别不在 7 类之内，退回到「其他」；
  3. emotion 不在 负面/中性/正面 之内，按「中性」兜底。
"""
import logging

from app import categories, llm
from prompts import classify as classify_prompt

logger = logging.getLogger("app.classifier")

# 合法情绪枚举
_EMOTIONS = {"负面", "中性", "正面"}


def classify(ticket_text: str, history: list | None = None) -> dict:
    """对一条工单文本进行分类 + 情绪识别 + 多诉求判断。

    返回：{"category", "confidence", "reason", "emotion",
           "emotion_intensity", "multi_intent"}
    """
    result = llm.complete(
        system=classify_prompt.SYSTEM_PROMPT,
        user=classify_prompt.build_user_prompt(ticket_text, history),
        json_mode=True,
        max_tokens=300,
    )

    category = str(result.get("category", "其他")).strip()
    reason = str(result.get("reason", "")).strip()

    # confidence 限制到 0~1 之间
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # 情绪兜底：非法值按「中性」处理
    emotion = str(result.get("emotion", "中性")).strip()
    if emotion not in _EMOTIONS:
        emotion = "中性"
    intensity = str(result.get("emotion_intensity", "")).strip()
    if intensity not in ("normal", "extreme"):
        intensity = "normal" if emotion == "负面" else ""
    multi_intent = bool(result.get("multi_intent", False))

    # 类别兜底
    if category not in categories.CATEGORIES:
        logger.warning("模型返回了未知类别 %r，已回退为「其他」", category)
        category = "其他"

    logger.info("分类结果：类别=%s，置信度=%.2f，情绪=%s(%s)，多诉求=%s",
                category, confidence, emotion, intensity, multi_intent)
    return {
        "category": category,
        "confidence": confidence,
        "reason": reason,
        "emotion": emotion,
        "emotion_intensity": intensity,
        "multi_intent": multi_intent,
    }
