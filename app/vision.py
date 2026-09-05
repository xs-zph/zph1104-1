"""图片输入校验与视觉识别。图片仅在请求期间驻留内存，不落盘。"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app import llm
from app.config import Config

logger = logging.getLogger("app.vision")

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF", b"WEBP"),
    "image/gif": (b"GIF87a", b"GIF89a"),
}

VISION_SYSTEM_PROMPT = """
你是电商客服的图片识别器。只描述图片中能直接观察到的事实，供客服处理问题。
重点关注：商品外观、破损/污渍/缺件、物流单号、订单截图中的状态、报错文字。
不要猜测看不清的内容；不确定时明确写“无法确认”。不要输出 Markdown，不要编造订单、金额或身份信息。
请用简短中文输出，最多 600 字。
""".strip()


def validate_image(content_type: str | None, data: bytes) -> str:
    """校验 MIME、文件头和真实图片结构，返回规范 MIME 类型。"""
    media_type = (content_type or "").lower().split(";", 1)[0].strip()
    signatures = ALLOWED_IMAGE_TYPES.get(media_type)
    if signatures is None:
        raise ValueError("仅支持 JPG、PNG、WEBP 或 GIF 图片")
    if not data or len(data) > Config.MAX_IMAGE_BYTES:
        raise ValueError(f"图片大小不能超过 {Config.MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    if media_type == "image/webp":
        valid = data[:4] == signatures[0] and data[8:12] == signatures[1]
    else:
        valid = any(data.startswith(signature) for signature in signatures)
    if not valid:
        raise ValueError("图片内容与文件类型不匹配")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("图片无法解码或结构无效") from exc
    if width <= 0 or height <= 0 or width > 10000 or height > 10000:
        raise ValueError("图片尺寸超出安全范围")
    return media_type


def analyze(data: bytes, media_type: str, user_hint: str = "") -> str:
    """识别图片中的客服相关事实。"""
    prompt = "请识别这张图片中的客服相关信息。"
    if user_hint:
        prompt += f"\n用户补充说明：{user_hint[:1000]}"
    result = llm.complete_vision(
        system=VISION_SYSTEM_PROMPT,
        user=prompt,
        image_data=data,
        media_type=media_type,
    ).strip()
    if not result:
        raise RuntimeError("视觉模型没有返回识别结果")
    return result[:2000]
