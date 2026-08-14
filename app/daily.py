"""日常问答工具：为 Agent 提供「查时间」「查天气」两个实时工具。

为什么要工具而不是直接问大模型：
  大模型（DeepSeek）是语言模型，没有实时时钟、也不能联网，直接问它「现在几点了」
  会编造一个错误时间。所以：
    - 时间：读服务器时钟，零延迟、零网络；
    - 天气：调免费天气接口 wttr.in（无需 key），2.5 秒超时 + 5 分钟缓存，失败友好降级。

  由 DeepSeek（Agent）负责理解问题、调用这些工具，再把真实数据组织成自然语言回答。
"""
import time
from datetime import datetime

import requests

from app import config

_weather_cache: dict = {}   # city -> (timestamp, reply)
_WEATHER_TTL = 300          # 5 分钟
_WEATHER_TIMEOUT = 2.5      # 秒

# 时间类关键词（刻意收窄，避免「发货时间」「几点发货」等业务问题误命中）
_TIME_KEYWORDS = ("现在几点", "几点了", "几点啦", "现在几", "今天几号", "今天星期几", "星期几", "现在时间")
# 天气类关键词
_WEATHER_KEYWORDS = ("天气", "气温", "温度", "下雨", "冷不冷", "热不热", "会下雨", "刮风")

# 误传为城市的时间词，命中则回退默认城市
_CITY_STOPWORDS = {"今天", "明天", "后天", "昨天", "现在", "当地", "这里", "我们"}


def is_daily(text: str) -> bool:
    """判断是否为「时间/天气」类日常问题（用于路由到 Agent 工具）。"""
    t = text.strip()
    if not t:
        return False
    return any(k in t for k in _TIME_KEYWORDS) or any(k in t for k in _WEATHER_KEYWORDS)


def get_time() -> str:
    """返回当前时间（含日期 / 星期），本地计算，毫秒级。"""
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return (
        f"现在是 {now.year} 年 {now.month} 月 {now.day} 日 "
        f"{now.strftime('%H:%M')}（周{week}）"
    )


def get_weather(city: str | None = None) -> str:
    """返回某城市天气。优先读缓存，接口不可用时友好降级。"""
    city = (city or "").strip()
    if not city or city in _CITY_STOPWORDS:
        city = config.Config.DEFAULT_CITY

    hit = _weather_cache.get(city)
    if hit and time.time() - hit[0] < _WEATHER_TTL:
        return hit[1]

    reply = _fetch_weather(city)
    _weather_cache[city] = (time.time(), reply)
    return reply


def _fetch_weather(city: str) -> str:
    """调用 wttr.in 拉取实时天气；失败返回友好降级文案。"""
    try:
        r = requests.get(
            f"https://wttr.in/{city}",
            params={"format": "%c %t（体感 %f）", "lang": "zh"},
            timeout=_WEATHER_TIMEOUT,
        )
        r.raise_for_status()
        text = r.text.strip()
        if not text or "unknown location" in text.lower():
            raise ValueError("未知城市")
        return f"{city}天气：{text}"
    except Exception:
        return (
            f"抱歉，暂时没能获取到「{city}」的实时天气（可能是网络波动）😅，"
            "建议您打开手机天气 App 查看哦。"
        )
