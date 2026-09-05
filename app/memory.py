"""会话记忆模块：让 AI 客服拥有「多轮对话」能力。

按用户名保存最近 N 轮对话历史。Agent / 闲聊回复时把历史带进上下文，
这样用户追问「那第二单物流呢」时，AI 能记得上一句说的是「我的订单」。

历史优先持久化到 MySQL，服务重启后仍能恢复当前会话；数据库暂时不可用时回退到进程内存，
并限制最大轮数，避免无限累积撑爆上下文窗口。
"""
from collections import deque
import logging

from app import db

logger = logging.getLogger("app.memory")

# 每个用户最多保留的对话轮数（一轮 = 用户一句 + 客服一句）
MAX_TURNS = 12

# 单条历史消息的最大字符数（防止超长回复被记进历史）
_MAX_MSG_CHARS = 500

# 数据库不可用时的故障回退缓存：username -> deque[{"role": ..., "content": ...}]
_HISTORY: dict[str, deque] = {}


def get_history(username: str | None, max_turns: int = MAX_TURNS) -> list[dict]:
    """返回该用户最近的对话历史（旧的在前），供拼接进提示词。"""
    if not username:
        return []
    max_messages = max(1, int(max_turns)) * 2
    try:
        return db.list_conversation_history(username, max_messages=max_messages)
    except Exception:  # noqa: BLE001
        logger.warning("持久化会话读取失败，回退进程内记忆：%s", username, exc_info=True)
        q = _HISTORY.get(username)
        return list(q)[-max_messages:] if q else []


def append(username: str | None, role: str, content: str) -> None:
    """记录一条消息（role: user / assistant）。"""
    if not username:
        return
    safe_content = (content or "")[:_MAX_MSG_CHARS]
    try:
        db.append_conversation_message(
            username, role, safe_content, max_messages=MAX_TURNS * 2,
        )
    except Exception:  # noqa: BLE001
        logger.warning("持久化会话写入失败，回退进程内记忆：%s", username, exc_info=True)
        q = _HISTORY.setdefault(username, deque(maxlen=MAX_TURNS * 2))
        q.append({"role": role, "content": safe_content})


def clear(username: str | None) -> None:
    """清空某个用户的对话历史（前端「新对话」按钮触发）。"""
    if username:
        try:
            db.clear_conversation_memory(username)
        except Exception:  # noqa: BLE001
            logger.warning("持久化会话清理失败，继续清理进程内记忆：%s", username, exc_info=True)
        _HISTORY.pop(username, None)
