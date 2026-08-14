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

from app import daily, db, llm, memory, rag
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
    """工具：检索知识库（RAG）。"""
    chunks = rag.retrieve(question, top_k=3)
    if not chunks:
        return "知识库暂无相关内容"
    return "\n".join(f"问：{c['question']}\n答：{c['answer']}" for c in chunks)


def _list_my_orders(username: str | None) -> str:
    """工具：列出当前登录用户名下的所有订单。"""
    if not username:
        return "当前会话未登录，无法查询订单列表"
    orders = db.list_orders_for_user(username)
    if not orders:
        return f"您（{username}）名下暂无订单"
    return "；".join(
        f"{o['order_id']}（{o['product']}，状态：{o['status']}）" for o in orders
    )


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
    if name == "get_time":
        return _get_time()
    if name == "get_weather":
        return _get_weather(args.get("city", ""))
    return "未知工具"


def run_agent(question: str, username: str | None = None) -> str:
    """运行 Agent：让模型自行决定调工具，最终返回客服回复。

    参数：
      question  用户问题
      username  当前登录用户名（用于订单归属校验；None 表示未登录/微信场景）
    """
    # 把身份注入工具执行闭包：模型只能决定「查哪个订单号」，
    # 但「查出来是不是这个用户的」由服务端在工具内部强制校验，模型无法越权。
    def execute(name, args):
        return _execute_tool(name, args, username)

    return llm.complete_with_tools(
        system=agent_prompt.SYSTEM_PROMPT,
        user=question,
        tools=TOOLS,
        execute=execute,
        history=memory.get_history(username),
    )
