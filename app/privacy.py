"""隐私脱敏模块：给客服系统划定「隐私边界」。

客服场景下，用户可能无意中在对话里写出手机号、身份证号、银行卡号、邮箱等
敏感信息。这些信息：
  1. 不该发给第三方大模型（DeepSeek）；
  2. 不该明文落盘到 MySQL / 日志。

本模块提供 mask_sensitive()，在「发送给 LLM」「写入数据库」「写入日志」之前
统一脱敏，把明文敏感信息替换成 138****5678 这样的掩码，实现数据最小化。
"""
import re

# 各类敏感信息的识别规则 + 脱敏方式（按顺序应用）
_PATTERNS = [
    # 手机号：1 开头、第二位 3-9、共 11 位 → 138****5678
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
     lambda m: m.group()[:3] + "****" + m.group()[-4:]),
    # 身份证号：18 位（末位可能是 X）→ 110***********1234
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
     lambda m: m.group()[:3] + "***********" + m.group()[-4:]),
    # 银行卡号：16~19 位数字 → 6222 **** 1234
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
     lambda m: m.group()[:4] + " **** " + m.group()[-4:]),
    # 邮箱 → z***@example.com
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
     lambda m: m.group()[0] + "***@" + m.group().split("@")[-1]),
]


def mask_sensitive(text: str) -> str:
    """把文本中的手机号/身份证/银行卡/邮箱脱敏，返回掩码后的文本。

    保留首尾几位，让客服仍能粗略辨认，但拿不到完整明文。
    """
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
