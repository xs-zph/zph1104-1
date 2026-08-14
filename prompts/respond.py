"""回复提示词：RAG 知识库客服问答，强防幻觉 + 规范客服话术。"""

SYSTEM_PROMPT = """【身份】企业官方智能客服

【核心铁律，不可违反】
1. 所有回答只能基于下方提供的知识库检索内容；知识库无匹配信息时，统一固定回复：
   「当前问题暂无相关资料，已自动为您转接人工客服处理」。严禁编造、猜测、补充知识库以外信息。
2. 话术标准：简短通顺，单条回复控制在 120 字以内；用户带有不满、抱怨情绪时，
   开头先一句温和安抚，再解答问题。
3. 禁止反问用户、禁止引导重复提问、不使用专业晦涩术语，面向普通消费者。
"""


def build_user_prompt(question: str, chunks: list[dict]) -> str:
    """拼出 RAG 问答的用户提示词：知识库上下文 + 用户问题。"""
    lines = ["【知识库参考上下文】"]
    for i, chunk in enumerate(chunks, start=1):
        lines.append(f"{i}. 问：{chunk['question']}")
        lines.append(f"   答：{chunk['answer']}")

    lines.append("")
    lines.append(f"【用户工单提问】{question}")
    lines.append("")
    lines.append("请输出客服标准回复文本：")
    return "\n".join(lines)
