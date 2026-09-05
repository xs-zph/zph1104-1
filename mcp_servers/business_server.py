"""AI 客服业务只读 MCP Server。

这个 Server 通过 stdio 启动，每个子进程只服务一个当前登录用户。
用户身份从 MCP_USERNAME 环境变量读取，不出现在工具参数中，订单查询仍由
数据库层按 username 强制过滤。
"""
import json
import os
import re
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app import db

mcp = FastMCP("business_mcp")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _current_username() -> str | None:
    username = os.getenv("MCP_USERNAME", "").strip()
    return username or None


def _auth_error() -> str:
    return "Error: 当前 MCP 会话没有登录用户身份，无法查询客户数据"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _order_view(order: dict[str, Any]) -> dict[str, Any]:
    """只返回客服查询需要的字段，避免把内部用户字段暴露给模型。"""
    return {
        key: order.get(key)
        for key in (
            "order_id", "product", "status", "tracking_no", "logistics",
            "refund_status",
        )
    }


def _clean_identifier(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label}不能为空、长度不能超过 64 个字符且只能包含字母、数字、下划线或短横线")
    return value


@mcp.tool(
    name="list_my_orders",
    title="查询我的订单",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def list_my_orders() -> str:
    """查询当前登录用户的订单列表，只返回该用户自己的订单。"""
    username = _current_username()
    if not username:
        return _auth_error()
    orders = db.list_orders_for_user(username)
    return _json({"count": len(orders), "orders": [_order_view(order) for order in orders]})


@mcp.tool(
    name="query_order",
    title="查询订单状态",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def query_order(
    order_id: Annotated[str, Field(
        min_length=1,
        max_length=64,
        description="订单号，例如 A20240812001",
    )],
) -> str:
    """按订单号查询当前登录用户自己的订单状态。"""
    username = _current_username()
    if not username:
        return _auth_error()
    order_id = _clean_identifier(order_id, "订单号")
    order = db.get_order_for_user(username, order_id)
    if not order:
        return _json({"found": False, "message": f"未查询到订单 {order_id}"})
    return _json({"found": True, "order": _order_view(order)})


@mcp.tool(
    name="query_logistics",
    title="查询物流进度",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def query_logistics(
    tracking_no: Annotated[str, Field(
        min_length=1,
        max_length=64,
        description="快递运单号",
    )],
) -> str:
    """按运单号查询当前登录用户自己的物流进度。"""
    username = _current_username()
    if not username:
        return _auth_error()
    tracking_no = _clean_identifier(tracking_no, "运单号")
    for order in db.list_orders_for_user(username):
        if order.get("tracking_no") == tracking_no:
            return _json({
                "found": True,
                "tracking_no": tracking_no,
                "logistics": order.get("logistics") or "暂无物流更新",
                "order_id": order.get("order_id"),
            })
    return _json({"found": False, "message": f"未查询到运单 {tracking_no}"})


@mcp.tool(
    name="check_refund",
    title="查询退款进度",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def check_refund(
    order_id: Annotated[str, Field(
        min_length=1,
        max_length=64,
        description="订单号，例如 A20240812001",
    )],
) -> str:
    """按订单号查询当前登录用户自己的退款状态。"""
    username = _current_username()
    if not username:
        return _auth_error()
    order_id = _clean_identifier(order_id, "订单号")
    order = db.get_order_for_user(username, order_id)
    if not order:
        return _json({"found": False, "message": f"未查询到订单 {order_id}"})
    return _json({
        "found": True,
        "order_id": order.get("order_id"),
        "refund_status": order.get("refund_status") or "暂无退款记录",
    })


if __name__ == "__main__":
    mcp.run("stdio")
