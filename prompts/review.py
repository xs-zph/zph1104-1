"""异常工单复盘优化提示词（针对分类错误的工单做迭代优化）。"""

SYSTEM_PROMPT = """针对本条处理异常的工单做完整复盘，输出结构化优化建议：
1. 故障定位：分析 AI 分类错误/知识库回答失效的根本原因
2. 缺失内容：列出当前知识库缺少的关键信息点
3. 落地优化方案：分三类给出可执行方案（补充FAQ知识库、优化分块策略、调整分类提示词）
"""


def build_user_prompt(error_workorder: str, wrong_class: str, wrong_answer: str) -> str:
    """拼出复盘的用户提示词。"""
    return (
        f"工单原文：{error_workorder}\n"
        f"AI错误分类结果：{wrong_class}\n"
        f"知识库错误回复：{wrong_answer}\n"
    )
