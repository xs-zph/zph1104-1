"""本地图片 OCR。

OCR 只在请求内存中处理图片，不写入磁盘。RapidOCR 未安装、模型不可用或没有
识别到文字时返回明确状态，由多模态接口继续使用视觉模型或转人工。
"""
from __future__ import annotations

import logging
import threading
from io import BytesIO

import numpy as np
from PIL import Image

from app.config import Config

logger = logging.getLogger("app.ocr")

_engine = None
_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


def recognize(data: bytes, media_type: str | None = None) -> str:
    """识别图片中的文字，返回清洗后的文本；不可用时抛出明确异常。"""
    if not Config.OCR_ENABLED:
        raise RuntimeError("OCR_ENABLED 已关闭")
    try:
        image = Image.open(BytesIO(data)).convert("RGB")
        result, _ = _get_engine()(np.asarray(image))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("本地 OCR 不可用") from exc

    texts = []
    for item in result or []:
        if len(item) < 2:
            continue
        text = str(item[1]).strip()
        score = float(item[2]) if len(item) > 2 else 1.0
        if text and score >= 0.35:
            texts.append(text)
    return "\n".join(texts)[:4000]
