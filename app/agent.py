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
import json
import logging

from app import config, daily, db, llm, mcp_client, memory, profile, rag, skills
from prompts import agent as agent_prompt

logger = logging.getLogger("app.agent")

# 高风险写操作不交给模型自由选择，必须由路由层的确认流程调用。
MODEL_BLOCKED_WRITE_TOOLS = frozenset({"cancel_order", "apply_after_sale"})

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

    召回 Top10（向量）→ 本地 CrossEncoder 重排 Top3（相关性）→ 距离过滤兜底。
    用比 RAG 优先更宽松的阈值（RAG_AGENT_MAX_DISTANCE）过滤掉「毫不相关」的
    条目，但保留弱相关条目——因为这里由大模型再判断一次相关性，多给候选
    比漏掉好，避免「空气炸锅怎么用」这类近似问法被误判为「查不到」。
    """
    faq_chunks = rag.retrieve(question, top_k=config.Config.RAG_TOP_K)
    faq_chunks = rag.rerank(question, faq_chunks, top_n=3)
    relevant = [
        c for c in faq_chunks
        if c.get("distance") is not None and c["distance"] <= config.Config.RAG_AGENT_MAX_DISTANCE
    ]
    document_chunks = rag.retrieve_docs(question, top_k=config.Config.RAG_TOP_K)
    if len(document_chunks) > 1:
        document_chunks = rag.rerank_documents(question, document_chunks, top_n=3)
    relevant_documents = [
        c for c in document_chunks
        if c.get("distance") is not None and c["distance"] <= config.Config.RAG_AGENT_MAX_DISTANCE
    ][:3]
    if not relevant and not relevant_documents:
        return "知识库暂无相关内容"
    parts = [
        f"问：{c['question']}\n答：{c['answer']}" for c in relevant
    ]
    parts.extend(
        f"文档片段（来源：{c['source']}）：\n{c['text']}" for c in relevant_documents
    )
    return "\n\n".join(parts)


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
        return "您目前没有正在处理的售后或人工工单。"
    category_names = {
        "人工处理工单": "人工服务",
        "自助查询": "订单/物流查询",
        "知识库命中": "常见问题咨询",
        "退款纠纷": "退款问题",
        "退款咨询": "退款咨询",
        "闲聊": "一般咨询",
        "商品咨询": "商品咨询",
        "售后维修": "售后服务",
        "退货申请": "退货申请",
        "物流查询": "物流查询",
        "发票问题": "发票问题",
    }
    status_names = {
        "auto": "已自动处理",
        "closed": "已完成",
        "resolved": "已解决",
        "escalated": "等待人工处理",
        "in_progress": "人工处理中",
        "waiting_customer": "等待您补充信息",
    }

    def customer_category(ticket: dict) -> str:
        category = ticket.get("category") or ""
        text = str(ticket.get("ticket_text") or "")
        if "工单" in text or "售后进度" in text:
            return "售后进度"
        if category == "人工处理工单":
            if any(k in text for k in ("退款", "退钱")):
                return "退款问题"
            if any(k in text for k in ("物流", "快递", "运单")):
                return "物流查询"
            if any(k in text for k in ("退货", "换货")):
                return "退换货问题"
            if any(k in text for k in ("维修", "售后", "质量", "损坏")):
                return "售后服务"
        return category_names.get(category, "客服咨询")

    business_keywords = (
        "退款", "退钱", "退货", "换货", "售后", "维修", "报修", "质量", "损坏", "破损",
        "投诉", "赔偿", "纠纷", "人工",
    )
    active_statuses = {"escalated", "in_progress", "waiting_customer"}
    completed_statuses = {"resolved", "closed"}

    def is_customer_ticket(ticket: dict) -> bool:
        status = ticket.get("status") or ""
        category = ticket.get("category") or ""
        text = str(ticket.get("ticket_text") or "")
        if status in active_statuses:
            return True
        if status not in completed_statuses:
            return False
        if category in {"退款纠纷", "退货申请", "售后维修"}:
            return True
        return category == "人工处理工单" and any(k in text for k in business_keywords)

    visible = [ticket for ticket in tickets if is_customer_ticket(ticket)]
    visible.sort(key=lambda ticket: ticket.get("status") not in active_statuses)
    visible = visible[:5]
    if not visible:
        return "您目前没有正在处理的售后或人工工单。订单和物流记录可以通过对应入口单独查询。"

    active_count = sum(ticket.get("status") in active_statuses for ticket in visible)
    if active_count:
        lines = [f"您有 {active_count} 个事项正在处理中："]
    else:
        lines = ["您目前没有正在处理的工单。最近已完成的事项："]
    for index, ticket in enumerate(visible, 1):
        category = customer_category(ticket)
        status = status_names.get(ticket.get("status"), "处理中")
        summary = " ".join(str(ticket.get("ticket_text") or "").split())
        if len(summary) > 42:
            summary = summary[:42] + "…"
        created_at = ticket.get("created_at")
        created = str(created_at)[:16] if created_at else ""
        detail = f"{index}. {category}：{status}"
        if summary:
            detail += f"\n   {summary}"
        if created:
            detail += f"\n   提交时间：{created}"
        lines.append(detail)
    return "\n".join(lines)


def _check_my_after_sales(username: str | None) -> str:
    """工具：查询当前用户提交的统一售后申请进度。"""
    if not username:
        return "当前会话未登录，无法查询售后申请"
    try:
        requests = db.list_after_sale_requests(username, limit=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取售后申请失败：%s", exc)
        return "售后申请进度暂时无法查询，请稍后重试。"
    if not requests:
        return "您目前没有统一售后申请记录。"
    labels = {
        "cancel": "取消订单",
        "return": "退货申请",
        "refund": "退款申请",
        "repair": "售后维修",
    }
    status_labels = {
        "draft": "信息收集中",
        "pending_confirmation": "等待确认",
        "processing": "处理中",
        "completed": "已完成",
        "rejected": "已拒绝",
        "cancelled": "已取消",
        "failed": "处理失败，可重试",
    }
    lines = ["您的售后申请进度："]
    for request in requests:
        request_type = labels.get(request.get("request_type"), "售后申请")
        status = status_labels.get(request.get("status"), "处理中")
        line = (
            f"{request_type}（申请号 {request.get('request_no')}，"
            f"订单 {request.get('order_id')}）：{status}"
        )
        if request.get("reason"):
            line += f"，原因：{request['reason']}"
        if request.get("result"):
            line += f"。{request['result']}"
        lines.append(line)
    return "\n".join(lines)


def _apply_after_sale(order_id: str, issue: str, username: str | None) -> str:
    """兼容旧调用方，但禁止绕过统一维修申请流程写入工单。

    维修申请现在必须由 router 的确认流程创建统一售后申请和人工工单。
    该函数保留旧名字，避免历史调用方导入失败，但不再执行任何数据库写操作。
    """
    del order_id, issue, username
    return (
        "维修申请需要先核对订单号和故障描述，并在您确认后提交。"
        "请通过客服对话中的“确认提交维修”完成申请。"
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


def _execute_with_mcp_fallback(name: str, args: dict, username: str | None) -> str:
    """业务查询的确定性执行器：MCP 优先，失败后回退同名内置工具。"""
    try:
        mcp_tools = mcp_client.list_tools(username, {name})
        if any(tool.get("function", {}).get("name") == name for tool in mcp_tools):
            try:
                return mcp_client.call_tool(username, name, args)
            except mcp_client.MCPClientError as exc:
                logger.warning("确定性查询的 MCP 工具 %s 失败，回退内置工具：%s", name, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("确定性查询发现 MCP 工具 %s 失败，回退内置工具：%s", name, exc)
    return _execute_tool(name, args, username)


def run_data_query(question: str, username: str | None = None) -> str:
    """处理不需要 LLM 猜参数的个人数据查询。

    用户只说“我的快递到哪了”时没有运单号，必须先查订单列表，
    再从当前用户自己的订单数据中返回物流信息，而不是让模型猜 tracking_no。
    """
    if not username:
        return "当前会话未登录，无法查询您的订单、物流或退款进度。"
    text = (question or "").lower()
    if any(term in text for term in ("售后申请进度", "退货申请进度", "取消申请进度")):
        return _check_my_after_sales(username)
    if "工单" in text or "售后进度" in text:
        return _execute_with_mcp_fallback("check_my_tickets", {}, username)

    raw = _execute_with_mcp_fallback("list_my_orders", {}, username)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw
    orders = payload.get("orders") or []
    if not orders:
        return "您目前没有可查询的订单记录。"

    if "退款" in text or "退钱" in text:
        rows = [
            f"订单 {order.get('order_id')}（{order.get('product')}）：{order.get('refund_status') or '暂无退款记录'}"
            for order in orders
        ]
        return "您的退款进度：" + "；".join(rows)

    if "快递" in text or "物流" in text or "到哪" in text or "什么时候到" in text:
        rows = []
        for order in orders:
            if order.get("tracking_no") or order.get("logistics"):
                rows.append(
                    f"订单 {order.get('order_id')}（{order.get('product')}）："
                    f"{order.get('logistics') or '暂无物流更新'}"
                )
        return "您的物流进度：" + ("；".join(rows) if rows else "目前还没有可查询的物流单号。")

    return "您的订单：" + "；".join(
        f"{order.get('order_id')}（{order.get('product')}，{order.get('status')}）"
        for order in orders
    )


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
    fallback_tools = [
        tool for tool in fallback_tools
        if tool.get("function", {}).get("name") not in MODEL_BLOCKED_WRITE_TOOLS
    ]
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
