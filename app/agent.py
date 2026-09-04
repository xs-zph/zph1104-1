"""Agent 模块：让 AI 客服拥有「工具调用」能力，和 RAG 知识库结合。

设计思路：
  - RAG（search_faq）作为其中一个工具，和订单 / 物流 / 退款查询工具并列；
  - 大模型（DeepSeek）根据用户问题自行决定调用哪个工具；
  - 工具返回结果后，模型再汇总成一句自然的客服回复。

关于「订单归属」（这是他的订单吗）：
  订单/物流/退款工具会自动绑定当前登录用户（username），只返回该用户自己的
  数据。实现上靠「服务端注入身份」——大模型只能决定「查哪个订单号」，
  但真正查库时服务端强制限定在当前用户名下，模型无法伪造身份越权查询他人订单。
"""
import logging

from app import config, daily, db, llm, mcp_client, memory, profile, rag, skills
from prompts import agent as agent_prompt

logger = logging.getLogger("app.agent")

# 提供给 DeepSeek 的工具定义（OpenAI function calling 格式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_faq",
            "description": "查询知识库里的通用政策，如退货流程、保修政策、发票开具等",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "要查询的问题"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_orders",
            "description": "查询当前登录客户名下的所有订单（订单号 + 商品 + 状态）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "按订单号查询当前登录客户名下的订单状态（如已发货/待发货/已完成）",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "订单号，如 A20240812001"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logistics",
            "description": "按运单号查询当前登录客户的物流进度",
            "parameters": {
                "type": "object",
                "properties": {"tracking_no": {"type": "string", "description": "快递运单号"}},
                "required": ["tracking_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund",
            "description": "按订单号查询当前登录客户的退款进度",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "订单号"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "取消当前登录客户自己的订单（仅「待发货」可在线取消）",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "订单号"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_my_tickets",
            "description": "查询当前登录客户自己提交的工单及处理进度",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_after_sale",
            "description": "客户确认要申请售后维修时，登记一条售后维修工单（记录订单号+故障问题），返回工单号供客户跟进。必须先和客户确认订单号和故障现象后再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "要申请售后的订单号，如 A20240812001"},
                    "issue": {"type": "string", "description": "故障/问题描述，如 无法开机、有杂音"},
                },
                "required": ["order_id", "issue"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "查询当前日期和时间（客户问「现在几点了 / 今天几号 / 星期几」时调用）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某城市的实时天气（客户问「天气怎么样 / 下雨吗 / 冷不冷」时调用）",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名，如 北京；客户没说具体城市时传空字符串"}},
            },
        },
    },
]


def _search_faq(question: str) -> str:
    """工具：检索知识库（RAG），返回相关条目供大模型判断。

    召回 Top5（向量）→ LLM 重排 Top3（相关性）→ 距离过滤兜底。
    用比 RAG 优先更宽松的阈值（RAG_AGENT_MAX_DISTANCE）过滤掉「毫不相关」的
    条目，但保留弱相关条目——因为这里由大模型再判断一次相关性，多给候选
    比漏掉好，避免「空气炸锅怎么用」这类近似问法被误判为「查不到」。
    """
    chunks = rag.retrieve(question, top_k=config.Config.RAG_TOP_K)
    chunks = rag.rerank(question, chunks, top_n=3)
    relevant = [
        c for c in chunks
        if c.get("distance") is not None and c["distance"] <= config.Config.RAG_AGENT_MAX_DISTANCE
    ]
    if not relevant:
        return "知识库暂无相关内容"
    return "\n".join(f"问：{c['question']}\n答：{c['answer']}" for c in relevant)


def _list_my_orders(username: str | None) -> str:
    """工具：列出当前登录用户名下的所有订单，并带上物流/退款状态。

    一次把订单的关键信息给全，让大模型无需再逐个调 query_order / query_logistics，
    少绕几圈、回答更快更准。
    """
    if not username:
        return "当前会话未登录，无法查询订单列表"
    orders = db.list_orders_for_user(username)
    if not orders:
        return f"您（{username}）名下暂无订单"
    parts = []
    for o in orders:
        p = f"{o['order_id']}（{o['product']}，状态：{o['status']}"
        if o.get("tracking_no"):
            p += f"，运单号：{o['tracking_no']}"
        if o.get("logistics"):
            p += f"，物流：{o['logistics']}"
        if o.get("refund_status"):
            p += f"，退款：{o['refund_status']}"
        p += "）"
        parts.append(p)
    return "；".join(parts)


def _query_order(order_id: str, username: str | None) -> str:
    """工具：按订单号查询订单，并校验「这是不是他的订单」。"""
    if username:
        order = db.get_order_for_user(username, order_id.strip())
    else:
        order = db.get_order_by_id(order_id.strip())
    if not order:
        if username:
            return f"未查询到您（{username}）名下的订单 {order_id}，请核对订单号"
        return f"未查询到订单 {order_id}，请核对订单号"
    parts = [f"订单 {order['order_id']}，商品：{order['product']}，状态：{order['status']}"]
    if order.get("tracking_no"):
        parts.append(f"运单号：{order['tracking_no']}")
    if order.get("refund_status"):
        parts.append(f"退款状态：{order['refund_status']}")
    return "；".join(parts)


def _query_logistics(tracking_no: str, username: str | None) -> str:
    """工具：按运单号查询物流，只查当前用户自己的订单。"""
    orders = db.list_orders_for_user(username) if username else db.list_all_orders()
    for order in orders:
        if order.get("tracking_no") == tracking_no.strip():
            return f"运单号 {tracking_no} 当前物流：{order['logistics']}"
    return f"未查询到运单号 {tracking_no} 的物流信息，请核对后重试"


def _check_refund(order_id: str, username: str | None) -> str:
    """工具：按订单号查询退款进度，并校验归属。"""
    if username:
        order = db.get_order_for_user(username, order_id.strip())
    else:
        order = db.get_order_by_id(order_id.strip())
    if not order:
        if username:
            return f"未查询到您（{username}）名下的订单 {order_id}，请核对订单号"
        return f"未查询到订单 {order_id}，请核对订单号"
    if order.get("refund_status"):
        return f"订单 {order['order_id']} 的退款状态：{order['refund_status']}"
    return f"订单 {order['order_id']} 暂无退款记录"


def _cancel_order(order_id: str, username: str | None) -> str:
    """工具：取消当前用户自己的订单（仅「待发货」可在线取消）。"""
    if not username:
        return "当前会话未登录，无法办理取消订单"
    order = db.cancel_order(username, order_id.strip())
    if order is None:
        return f"无法取消订单 {order_id}：请核对订单号，或该订单已发货/已完成，不支持在线取消"
    return f"订单 {order['order_id']}（{order['product']}）已成功取消"


def _check_my_tickets(username: str | None) -> str:
    """工具：查询当前用户自己提交的工单及处理进度。"""
    if not username:
        return "当前会话未登录，无法查询工单"
    tickets = db.list_tickets_for_user(username)
    if not tickets:
        return "您暂时没有提交过工单"
    return "；".join(
        f"工单#{t['id']}（{t['category'] or '未分类'}，状态：{t['status']}）" for t in tickets
    )


def _apply_after_sale(order_id: str, issue: str, username: str | None) -> str:
    """工具：登记售后维修工单（记录订单号 + 故障问题），供人工客服跟进。"""
    if not username:
        return "当前会话未登录，无法登记售后申请"
    order = db.get_order_for_user(username, order_id.strip())
    if not order:
        return f"未查询到您（{username}）名下的订单 {order_id}，请核对订单号"
    ticket_id = db.insert_ticket(
        ticket_text=f"[售后维修申请] 订单 {order_id}（{order['product']}）问题：{issue}",
        category="售后维修",
        status="escalated",
        username=username,
    )
    return (
        f"已登记售后维修工单 #{ticket_id}（订单 {order_id}，商品 {order['product']}，问题：{issue}）。"
        "客服会在 24 小时内响应，请您保留好故障照片/视频凭证。"
    )


def _get_time() -> str:
    """工具：查询当前时间。"""
    return daily.get_time()


def _get_weather(city: str) -> str:
    """工具：查询天气。city 为空时查默认城市。"""
    return daily.get_weather(city.strip() if city else None)


def _execute_tool(name: str, args: dict, username: str | None) -> str:
    """执行单个工具，返回结果字符串。username 是当前登录用户（服务端注入）。"""
    if name == "search_faq":
        return _search_faq(args.get("question", ""))
    if name == "list_my_orders":
        return _list_my_orders(username)
    if name == "query_order":
        return _query_order(args.get("order_id", ""), username)
    if name == "query_logistics":
        return _query_logistics(args.get("tracking_no", ""), username)
    if name == "check_refund":
        return _check_refund(args.get("order_id", ""), username)
    if name == "cancel_order":
        return _cancel_order(args.get("order_id", ""), username)
    if name == "check_my_tickets":
        return _check_my_tickets(username)
    if name == "apply_after_sale":
        return _apply_after_sale(args.get("order_id", ""), args.get("issue", ""), username)
    if name == "get_time":
        return _get_time()
    if name == "get_weather":
        return _get_weather(args.get("city", ""))
    return "未知工具"


def run_agent(question: str, username: str | None = None, *,
              tool_names: set[str] | None = None,
              system_context: str | None = None) -> str:
    """运行 Agent：让模型自行决定调工具，最终返回客服回复。

    参数：
      question  用户问题
      username  当前登录用户名（用于订单归属校验；None 表示未登录/微信场景）
      tool_names  可选的服务端工具白名单；多 Agent 子 Agent 只能使用其职责内工具
      system_context  可选的主 Agent 监督上下文
    """
    # 把身份注入工具执行闭包：模型只能决定「查哪个订单号」，
    # 但「查出来是不是这个用户的」由服务端在工具内部强制校验，模型无法越权。
    fallback_tools = skills.select_tools(question, TOOLS)
    if tool_names is not None:
        fallback_tools = [
            tool for tool in fallback_tools
            if tool.get("function", {}).get("name") in tool_names
        ]
    selected_names = {
        tool.get("function", {}).get("name") for tool in fallback_tools
    }
    try:
        mcp_tools = mcp_client.list_tools(username, selected_names)
    except Exception as exc:  # noqa: BLE001 - MCP 是可选增强，发现失败需降级
        logger.warning("MCP 工具发现异常，使用内置工具：%s", exc)
        mcp_tools = []
    mcp_names = {
        tool.get("function", {}).get("name") for tool in mcp_tools
    }

    def execute(name, args):
        if name in mcp_names:
            try:
                return mcp_client.call_tool(username, name, args)
            except mcp_client.MCPClientError as exc:
                logger.warning("MCP 工具 %s 调用失败，回退内置工具：%s", name, exc)
        return _execute_tool(name, args, username)

    selected_tools = [
        next((tool for tool in mcp_tools
              if tool["function"]["name"] == fallback["function"]["name"]), fallback)
        for fallback in fallback_tools
    ]

    system = agent_prompt.SYSTEM_PROMPT + "\n\n" + skills.context(question)
    if system_context:
        system += "\n\n" + system_context
    profile_context = profile.get_context_for_question(username, question)
    if profile_context:
        system += (
            "\n\n【用户实体画像卡片】\n"
            "以下是数据库中保存的用户明确事实。涉及用户身份、设备或偏好时优先使用；"
            "如果客户当前消息明确修正了某项事实，以当前消息为准。不要把画像卡片内容当作订单实时状态。\n"
            + profile_context
        )

    return llm.complete_with_tools(
        system=system,
        user=question,
        tools=selected_tools,
        execute=execute,
        history=memory.get_history(username),
    )
