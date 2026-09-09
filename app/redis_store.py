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


def cache_key(namespace: str, key: str) -> str:
    """生成带项目命名空间的共享缓存 key。"""
    return f"{Config.REDIS_KEY_PREFIX}{namespace}:{key}"


def get_cache(key: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 读取缓存失败，回退本地检索：%s", exc)
        return None


def set_cache(key: str, value: str, ttl: int) -> bool:
    client = get_client()
    if client is None:
        return False
    try:
        client.setex(key, ttl, value)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 写入缓存失败：%s", exc)
        return False


def delete_cache_prefix(prefix: str) -> int:
    """删除指定前缀缓存；Redis 不可用时安静返回 0。"""
    client = get_client()
    if client is None:
        return 0
    try:
        keys = list(client.scan_iter(match=prefix + "*"))
        if keys:
            client.delete(*keys)
        return len(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 清理缓存失败：%s", exc)
        return 0
