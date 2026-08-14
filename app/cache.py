"""响应缓存：把重复问题的处理结果缓存起来，跳过分类 + 大模型，大幅降低响应耗时。

设计取舍：
  - 用内存 OrderedDict 做 LRU 缓存，进程重启即清空，零持久化成本；
  - 只缓存「确定性回复」（闲聊 / 模板），不缓存订单/物流/退款等会随时间变化的
    Agent 结果，避免返回过期数据；
  - 每条 5 分钟自动过期（TTL），防止长期占用内存或返回陈旧内容。
"""
import time
from collections import OrderedDict

MAX_SIZE = 500        # 最多缓存 500 条
TTL_SECONDS = 300     # 5 分钟过期

_cache: OrderedDict = OrderedDict()


def get(key: str):
    """命中返回缓存值；未命中或已过期返回 None。"""
    item = _cache.get(key)
    if item is None:
        return None
    ts, value = item
    if time.time() - ts > TTL_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)  # LRU：命中后移到末尾
    return value


def set(key: str, value) -> None:
    """写入缓存；超过容量时淘汰最久未用的条目。"""
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > MAX_SIZE:
        _cache.popitem(last=False)


def clear() -> None:
    """清空缓存。"""
    _cache.clear()


def size() -> int:
    """当前缓存条数（调试 / 演示用）。"""
    return len(_cache)
