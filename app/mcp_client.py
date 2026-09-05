"""MCP 客户端适配层。

对 Agent 暴露两个同步函数：发现工具、调用工具。MCP 会话和 stdio 进程
生命周期都隐藏在这里，调用失败由上层决定是否回退到内置工具。
"""
import asyncio
import logging
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from app import config
from app.config import Config

logger = logging.getLogger("app.mcp_client")

MCP_READ_ONLY_TOOLS = frozenset({
    "list_my_orders",
    "query_order",
    "query_logistics",
    "check_refund",
})

MCP_TOOL_ARGUMENTS = {
    "list_my_orders": frozenset(),
    "query_order": frozenset({"order_id"}),
    "query_logistics": frozenset({"tracking_no"}),
    "check_refund": frozenset({"order_id"}),
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class MCPClientError(RuntimeError):
    """MCP 服务不可用或返回了工具错误。"""


def _enabled() -> bool:
    return Config.MCP_ENABLED and bool(Config.MCP_SERVER_PATH)


def _server_parameters(username: str):
    from mcp import StdioServerParameters

    env = os.environ.copy()
    env["MCP_USERNAME"] = username
    env["PYTHONUNBUFFERED"] = "1"
    project_path = str(config.BASE_DIR)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (project_path, env.get("PYTHONPATH", "")) if part
    )
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(Config.MCP_SERVER_PATH))],
        env=env,
        cwd=str(config.BASE_DIR),
    )


async def _list_tools_async(username: str) -> list[Any]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(_server_parameters(username), errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)


async def _call_tool_async(username: str, name: str, arguments: dict[str, Any]) -> Any:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(_server_parameters(username), errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    name,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=Config.MCP_TIMEOUT_SECONDS),
                )


def _run(coro_factory):
    """在现有同步 Agent 中运行一次短生命周期 MCP 会话。"""
    try:
        return asyncio.run(coro_factory())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" in str(exc):
            raise MCPClientError("当前事件循环中无法启动同步 MCP 会话") from exc
        raise


def _to_openai_tool(tool: Any) -> dict[str, Any]:
    """把 MCP Tool 转成现有 LLM 适配器使用的 OpenAI 工具格式。"""
    schema = dict(tool.inputSchema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }


def list_tools(username: str | None, allowed_names: set[str]) -> list[dict[str, Any]]:
    """发现并筛选本轮允许使用的 MCP 工具。"""
    if not username or not _enabled() or not (allowed_names & MCP_READ_ONLY_TOOLS):
        return []
    try:
        tools = _run(lambda: _list_tools_async(username.strip()))
    except Exception as exc:  # noqa: BLE001 - MCP 协议/子进程异常都应触发降级
        logger.warning("MCP 工具发现失败，使用内置工具：%s", exc)
        return []
    return [
        _to_openai_tool(tool)
        for tool in tools
        if tool.name in allowed_names and tool.name in MCP_READ_ONLY_TOOLS
    ]


def call_tool(username: str | None, name: str, arguments: dict[str, Any]) -> str:
    """调用 MCP 工具；username 不来自模型，而来自当前 Agent 会话。"""
    if not username:
        raise MCPClientError("MCP 查询需要当前登录用户")
    if name not in MCP_READ_ONLY_TOOLS:
        raise MCPClientError(f"MCP 工具不在只读白名单中：{name}")
    if not isinstance(arguments, dict):
        raise MCPClientError("MCP 工具参数必须是对象")
    allowed_arguments = MCP_TOOL_ARGUMENTS[name]
    if set(arguments) != allowed_arguments:
        raise MCPClientError(f"MCP 工具参数不符合约束：{name}")
    for value in arguments.values():
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value.strip()):
            raise MCPClientError(f"MCP 工具参数不符合约束：{name}")
    try:
        result = _run(lambda: _call_tool_async(username.strip(), name, arguments))
    except Exception as exc:  # noqa: BLE001 - MCP 协议/子进程异常统一转业务错误
        raise MCPClientError(f"MCP 工具调用失败：{exc}") from exc
    if result.isError:
        raise MCPClientError(_content_text(result) or "MCP 工具返回错误")
    return _content_text(result)


def _content_text(result: Any) -> str:
    texts = [item.text for item in result.content if getattr(item, "text", None)]
    if texts:
        return "\n".join(texts)
    if result.structuredContent:
        import json

        return json.dumps(result.structuredContent, ensure_ascii=False)
    return ""
