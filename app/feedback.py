"""人工负反馈迭代优化模块（模块⑦）。

人工坐席给「AI 处理得不对」的工单打上错误标签后，系统针对不同标签
输出一条可落地的优化建议，形成「发现问题 → 标注 → 给建议 → 迭代」的闭环：

  - 分类错误     → 建议补充为分类器 few-shot 样例
  - 知识库无答案 → 建议补一条 FAQ（优先复用人工已写的正确答案）
  - AI回答有误   → 建议修正/回流对应知识库条目
  - 安抚不合适   → 建议优化安抚话术 / 补充情绪识别样例
"""
import logging

from app import llm

logger = logging.getLogger("app.feedback")

# 合法错误标签
TAG_LABELS = ("分类错误", "知识库无答案", "AI回答有误", "安抚不合适")

_DRAFT_SYSTEM = """你是电商客服知识库编辑。根据客户问题，起草一条简洁、准确、可直接上线的知识库答案。

【要求】
1. 直接回答客户问题，语言简洁（150 字以内），语气与官方客服一致。
2. 只写有把握的通用回答；涉及具体金额、时效等不确定数字时，用「以页面/客服核实为准」代替，不要编造。
3. 只输出答案正文，不要加前缀、不要用 JSON。
"""


def suggest(tag: str, ticket: dict) -> dict:
    """根据错误标签和工单内容，输出一条优化建议。"""
    ticket_text = (ticket.get("ticket_text") or "").strip()
    human_answer = (ticket.get("human_answer") or "").strip()

    if tag == "知识库无答案":
        # 优先复用人工已给出的正确答案（最可靠）；没有则让 LLM 起草（需人工审核）
        draft = human_answer or _draft_answer(ticket_text)
        return {
            "action": "add_faq",
            "message": "知识库缺此条，建议补一条 FAQ，AI 下次即可自动回答",
            "faq_question": ticket_text,
            "faq_answer": draft,
            "review_required": not bool(human_answer),
        }
    if tag == "分类错误":
        return {
            "action": "add_fewshot",
            "message": f"该工单被 AI 错分为「{ticket.get('category') or '未知'}」，"
                       "建议把它补进分类器 few-shot 样例并标注真实分类",
        }
    if tag == "AI回答有误":
        return {
            "action": "fix_kb",
            "message": "知识库有相关内容但 AI 组织错了，建议人工给出正确答案后回流 / 修正对应知识库条目",
        }
    if tag == "安抚不合适":
        return {
            "action": "fix_soothe",
            "message": "安抚不到位，建议优化安抚话术（先共情再解决），或把该工单补充为情绪识别样例",
        }
    return {"action": "none", "message": "未知标签"}


def _draft_answer(question: str) -> str:
    """让 LLM 起草一条知识库答案（人工审核后使用）。失败返回空串。"""
    if not question:
        return ""
    try:
        return llm.complete(
            system=_DRAFT_SYSTEM,
            user=question,
            max_tokens=256,
            temperature=0.2,
        ).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("起草知识库答案失败：%s", e)
        return ""
