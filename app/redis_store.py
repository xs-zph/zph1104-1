"""Redis 可选适配层；连接失败时由上层回退本地实现。"""

from __future__ import annotations

import logging
import threading

from app.config import Config

logger = logging.getLogger("app.redis")
_lock = threading.Lock()
_client = None
_disabled = False


def get_client():
    """懒加载并探测 Redis；失败后本进程不重复阻塞请求。"""
    global _client, _disabled
    if _client is not None or _disabled or not Config.REDIS_URL:
        return _client
    with _lock:
        if _client is not None or _disabled:
            return _client
        try:
            import redis

            client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True)
            client.ping()
            _client = client
            logger.info("Redis 已连接")
        except Exception as exc:  # noqa: BLE001
            _disabled = True
            logger.warning("Redis 不可用，回退本地模式：%s", exc)
    return _client


def session_key(token: str) -> str:
    return f"{Config.REDIS_KEY_PREFIX}session:{token}"


def save_session(token: str, username: str, ttl: int) -> bool:
    client = get_client()
    if client is None:
        return False
    client.setex(session_key(token), ttl, username)
    return True


def load_session(token: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    return client.get(session_key(token))


def delete_session(token: str) -> bool:
    client = get_client()
    if client is None:
        return False
    client.delete(session_key(token))
    return True
