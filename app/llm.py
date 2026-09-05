"""大模型调用模块：统一封装 DeepSeek API（OpenAI 兼容接口）。

整个系统里所有需要调用大模型的地方都通过这里，好处是：
  - 密钥、模型名、接口地址只在一处配置（config.py）
  - 出错信息统一、友好
  - 支持「JSON 模式」：让模型返回 JSON，本模块负责稳健地解析成 dict
"""
import json
import logging
import base64
import queue
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

import requests

from app import config
from app.config import Config

logger = logging.getLogger("app.llm")

# 单条用户输入 / 工具结果的长度上限，超出部分截断，避免异常长文撑爆上下文窗口
MAX_USER_CHARS = 4000

# 复用同一个 HTTP 会话（keep-alive），避免每次调用都重新 TCP + TLS 握手
_session = requests.Session()


class QueueFullError(Exception):
    """内部执行队列已满。"""


class LLMOverloadedError(RuntimeError):
    """大模型服务当前过载，调用方应执行业务降级。"""


class LLMTimeoutError(RuntimeError):
    """大模型调用超过总时限。"""


class LLMCircuitOpenError(RuntimeError):
    """大模型连续失败，熔断器暂时阻止新请求。"""


class _BoundedExecutor:
    """带固定 worker 和有界队列的轻量任务执行器。"""

    def __init__(self, workers: int, max_queue: int):
        self._tasks = queue.Queue(maxsize=max(1, max_queue))
        self._threads = []
        for index in range(max(1, workers)):
            thread = threading.Thread(
                target=self._run,
                name=f"llm-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, fn, *args, **kwargs) -> Future:
        future = Future()
        try:
            self._tasks.put_nowait((future, fn, args, kwargs))
        except queue.Full as exc:
            raise QueueFullError from exc
        return future

    def _run(self):
        while True:
            future, fn, args, kwargs = self._tasks.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as exc:  # noqa: BLE001
                        future.set_exception(exc)
            finally:
                self._tasks.task_done()


class _CircuitBreaker:
    """按瞬时上游失败次数熔断，成功后自动恢复。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._half_open = False

    def before_call(self):
        now = time.monotonic()
        with self._lock:
            if not self._opened_at:
                return
            if now - self._opened_at < Config.LLM_CIRCUIT_RECOVERY_SECONDS:
                raise LLMCircuitOpenError("大模型服务暂时不可用，请稍后重试")
            if self._half_open:
                raise LLMCircuitOpenError("大模型服务正在恢复，请稍后重试")
            self._half_open = True

    def success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open = False

    def failure(self):
        with self._lock:
            self._failures += 1
            self._half_open = False
            if self._failures >= max(1, Config.LLM_CIRCUIT_FAILURE_THRESHOLD):
                self._opened_at = time.monotonic()


_executor = _BoundedExecutor(Config.LLM_MAX_WORKERS, Config.LLM_QUEUE_SIZE)
_circuit = _CircuitBreaker()


def _truncate(text: str, max_chars: int = MAX_USER_CHARS) -> str:
    """截断过长文本，防止「内容太长」导致上下文被撑爆或回复被截断。"""
    if text and len(text) > max_chars:
        return text[:max_chars] + "…（内容过长，已截断）"
    return text


def _parse_json(text: str) -> dict:
    """从模型输出里稳健地解析出 JSON（兼容代码块包裹、前后多余文字等）。"""
    text = text.strip()
    # 去掉 ```json ... ``` 之类的代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退而求其次：截取第一个 { 到最后一个 } 之间的内容再解析
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析模型返回的 JSON：{text[:200]}")


def _post_once(
    url: str, payload: dict, api_key: str, timeout_seconds: float,
) -> dict:
    """执行一次上游请求；只把可重试的错误标记为瞬时错误。"""
    try:
        resp = _session.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=max(0.1, timeout_seconds),
        )
    except requests.RequestException as exc:
        raise _UpstreamError(f"DeepSeek API 网络错误：{exc}", transient=True) from exc

    if resp.status_code != 200:
        transient = resp.status_code == 408 or resp.status_code == 429 or resp.status_code >= 500
        raise _UpstreamError(
            f"DeepSeek API 调用失败（HTTP {resp.status_code}）：{resp.text[:300]}",
            transient=transient,
        )
    return resp.json()


class _UpstreamError(RuntimeError):
    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


def _perform_api_call(url: str, payload: dict, api_key: str) -> dict:
    """在 worker 中执行请求、重试和熔断状态更新。"""
    _circuit.before_call()
    retries = max(0, Config.LLM_MAX_RETRIES)
    deadline = time.monotonic() + max(0.1, Config.LLM_TOTAL_TIMEOUT_SECONDS)
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _circuit.failure()
            raise _UpstreamError("大模型任务超过总时限", transient=True)
        try:
            result = _post_once(
                url,
                payload,
                api_key,
                min(Config.LLM_REQUEST_TIMEOUT_SECONDS, remaining),
            )
            _circuit.success()
            return result
        except _UpstreamError as exc:
            if not exc.transient or attempt >= retries:
                if exc.transient:
                    _circuit.failure()
                raise
            delay = Config.LLM_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            if delay > 0:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))


def _call_api(messages: list, json_mode: bool = False, max_tokens: int = 1024,
              temperature: float = 0.1, tools: list | None = None,
              model: str | None = None, base_url: str | None = None,
              api_key: str | None = None) -> dict:
    """将一次大模型调用提交到有界队列，并返回完整响应 dict。"""
    api_key = api_key or config.Config.DEEPSEEK_API_KEY
    if not api_key:
        raise RuntimeError(
            "未检测到 API 密钥：请在项目根目录创建 .env 文件，并填写 "
            "DEEPSEEK_API_KEY=sk-... （可参考 .env.example）"
        )

    url = (base_url or config.Config.DEEPSEEK_BASE_URL).rstrip("/") + "/chat/completions"
    payload = {
        "model": model or config.Config.MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        # 要求模型输出 JSON 对象（提示词中需出现 "json" 字样）
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools

    logger.info("提交大模型任务（model=%s）", model or config.Config.MODEL)
    try:
        future = _executor.submit(_perform_api_call, url, payload, api_key)
    except QueueFullError as exc:
        logger.warning("大模型任务队列已满，执行业务降级")
        raise LLMOverloadedError("当前咨询量较大，已为您转接人工客服") from exc
    try:
        return future.result(timeout=max(0.1, Config.LLM_TOTAL_TIMEOUT_SECONDS))
    except FutureTimeoutError as exc:
        logger.warning("大模型任务超过总时限，执行业务降级")
        future.cancel()
        raise LLMTimeoutError("大模型响应超时，请转人工处理") from exc


def reset_runtime_state() -> None:
    """重置熔断状态，供测试和进程内运维使用。"""
    _circuit.success()


def complete_vision(system: str, user: str, image_data: bytes,
                    media_type: str, max_tokens: int = 768) -> str:
    """调用兼容 OpenAI 视觉消息格式的模型识别图片。"""
    if not config.Config.VISION_MODEL:
        raise RuntimeError("未配置 VISION_MODEL，暂未启用图片识别")
    image_base64 = base64.b64encode(image_data).decode("ascii")
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{image_base64}",
                    },
                },
            ],
        },
    ]
    data = _call_api(
        messages,
        max_tokens=max_tokens,
        temperature=0.1,
        model=config.Config.VISION_MODEL,
        base_url=config.Config.VISION_BASE_URL,
        api_key=config.Config.VISION_API_KEY,
    )
    return data["choices"][0]["message"].get("content") or ""


def complete(system: str, user: str, json_mode: bool = False, max_tokens: int = 1024,
             temperature: float = 0.1, history: list | None = None):
    """单轮调用 DeepSeek，返回结果。

    参数：
      system      系统提示词（设定角色、规则）
      user        用户输入内容
      json_mode   若为 True，要求模型返回 JSON 并解析成 dict 返回；否则返回文本
      temperature 采样温度：分类/抽取用低值（0.1），闲聊对话可用较高值（0.7）
      history     多轮对话历史 [{"role": "user"/"assistant", "content": ...}]，按时间顺序
    """
    messages = [{"role": "system", "content": system}]
    for h in (history or []):
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": _truncate(user)})
    data = _call_api(messages, json_mode=json_mode, max_tokens=max_tokens,
                     temperature=temperature)
    text = data["choices"][0]["message"]["content"]
    if json_mode:
        return _parse_json(text)
    return text


def complete_with_tools(system: str, user: str, tools: list, execute,
                        max_steps: int = 4, history: list | None = None) -> str:
    """Agent 循环：让模型根据用户问题自行决定调用哪些工具，最终生成回复。

    参数：
      system    系统提示词（描述可用工具与规则）
      user      用户输入
      tools     工具定义列表（OpenAI function calling 格式）
      execute   工具执行回调：execute(tool_name, args) -> 结果字符串
      max_steps 最多循环几轮（防止死循环）
      history   多轮对话历史（同 complete 的 history）
    """
    messages = [{"role": "system", "content": system}]
    for h in (history or []):
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": _truncate(user)})

    for _ in range(max_steps):
        data = _call_api(messages, tools=tools, max_tokens=512, temperature=0.3)
        message = data["choices"][0]["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # 模型没有再要调工具，返回最终回复
            return message.get("content") or ""

        # 依次执行模型要求的工具，把结果作为 tool 消息回传（结果也做长度护栏）
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.info("Agent 调用工具：%s(%s)", name, args)
            result = execute(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": _truncate(str(result), 2000),
            })

    return "抱歉，我一时没处理好，请稍后重试或转人工客服。"
