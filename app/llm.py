"""大模型调用模块：统一封装 DeepSeek API（OpenAI 兼容接口）。

整个系统里所有需要调用大模型的地方都通过这里，好处是：
  - 密钥、模型名、接口地址只在一处配置（config.py）
  - 出错信息统一、友好
  - 支持「JSON 模式」：让模型返回 JSON，本模块负责稳健地解析成 dict
"""
import json
import logging
import re

import requests

from app import config

logger = logging.getLogger("app.llm")

# 单条用户输入 / 工具结果的长度上限，超出部分截断，避免异常长文撑爆上下文窗口
MAX_USER_CHARS = 4000


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


def _call_api(messages: list, json_mode: bool = False, max_tokens: int = 1024,
              temperature: float = 0.1, tools: list | None = None) -> dict:
    """调用 DeepSeek 的底层方法，返回完整响应 dict。"""
    if not config.Config.DEEPSEEK_API_KEY:
        raise RuntimeError(
            "未检测到 API 密钥：请在项目根目录创建 .env 文件，并填写 "
            "DEEPSEEK_API_KEY=sk-... （可参考 .env.example）"
        )

    url = config.Config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.Config.MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        # 要求模型输出 JSON 对象（提示词中需出现 "json" 字样）
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools

    logger.info("调用 DeepSeek（model=%s）", config.Config.MODEL)
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {config.Config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"DeepSeek API 调用失败（HTTP {resp.status_code}）：{resp.text[:300]}"
        )
    return resp.json()


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
        data = _call_api(messages, tools=tools, max_tokens=1024, temperature=0.3)
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
