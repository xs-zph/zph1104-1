"""路由模块：系统的「大脑」，决定每张工单走哪条处理路径。

处理流程（按优先级从高到低）：
  0. 日常问答（时间/天气）         → Agent 调实时工具
  1. 实时查询（订单/工单/物流/退款）→ Agent 调工具，每次读取当前状态
  2. 响应缓存命中                  → 直接返回
  3. 闲聊（问候/寒暄）             → 聊天回复
  4. 明确投诉/纠纷/赔偿/转人工     → 直接升级人工（先安抚）
  5. RAG 知识库命中                → 多问法缓存 / 语义缓存 / 向量检索
  6. 多轮追问承接                  → 交 Agent（带历史）
  7. 分类 → 低置信或投诉/纠纷类 → 升级人工；可自动类 → Agent
  8. 兜底 → 转人工
"""
import json
import logging
import time

from app import agent, cache, cancel_skill, categories, classifier, config, daily, db, memory, multi_agent, privacy, profile, rag, responder, refund_skill, return_skill

logger = logging.getLogger("app.router")
ticket_logger = logging.getLogger("tickets")

# 常见问候/寒暄关键词：命中则走「闲聊」通道，不进入工单分类
_SMALLTALK_KEYWORDS = (
    "你好", "您好", "在吗", "在不在", "hi", "hello", "hey", "嗨", "哈喽",
    "早上好", "中午好", "下午好", "晚上好", "早安", "晚安",
    "你是谁", "你叫什么", "你是哪个", "你是哪个机器人", "介绍一下", "介绍你自己",
    "你是机器人", "你是真人", "你是ai", "你是AI", "你是智能",
    "你能做什么", "你能干什么", "你能帮我什么", "有什么功能", "你会什么", "你可以做什么",
    "谢谢", "感谢", "辛苦了", "再见", "拜拜",
)


# 助手元对话意图：不是业务工单，而是身份/能力/介绍类问题。
_ASSISTANT_META_KEYWORDS = (
    "你是谁", "你叫什么", "你是哪个", "你是哪个机器人", "你是谁呀", "你叫什么名字",
    "你能做什么", "你能干什么", "你能帮我什么", "有什么功能", "你会什么", "你可以做什么",
    "介绍一下", "介绍你自己", "自我介绍", "说说你自己",
)

_SERVICE_FEEDBACK_KEYWORDS = (
    "回答好慢", "回答太慢", "回复太慢", "反应太慢", "怎么这么慢", "怎么那么慢",
    "等了很久", "等半天", "半天没回复", "怎么还没回答", "还没回答", "太慢了",
    "等了好久", "等很久了", "一直没回复", "快点回复", "赶紧回复", "麻烦快点",
    "我很着急", "有点着急", "很着急", "急死了", "不耐烦", "不满意", "很失望",
    "服务真差", "服务太差", "不靠谱", "无语", "服了",
)


def _is_assistant_meta_intent(text: str) -> bool:
    """判断是否为助手身份/能力/介绍类元对话。"""
    t = (text or "").strip().lower()
    return any(k in t for k in _ASSISTANT_META_KEYWORDS)


def _has_service_feedback(text: str) -> bool:
    """判断客户是否表达了轻度着急、不满、催促或等待过久。"""
    t = (text or "").strip().lower()
    return any(k in t for k in _SERVICE_FEEDBACK_KEYWORDS)


def _is_service_feedback(text: str) -> bool:
    """判断是否是单纯服务反馈，不应进入分类或人工路由。"""
    t = (text or "").strip().lower()
    if not _has_service_feedback(t):
        return False
    # 混合了明确业务诉求时，保留业务词交给后续退款/订单/售后流程。
    return not any(k in t for k in (
        "退款", "退钱", "订单", "物流", "快递", "售后", "退货", "换货", "维修", "发票",
        "商品", "质量", "坏了", "损坏", "破损",
    ))


def _with_empathy(reply: str, service_feedback: bool) -> str:
    """轻度负面情绪先共情一句，再接原业务答复。"""
    if not service_feedback:
        return reply
    if reply.startswith(("抱歉", "非常抱歉", "我理解")):
        return reply
    return "抱歉让您久等了，我理解您现在比较着急。" + reply


def _service_feedback_record(text: str, trace: list, mk) -> dict:
    """本地生成服务反馈安抚语，避免再次调用 LLM。"""
    trace.append("service_feedback")
    reply = "抱歉让您久等了，我在这里。您可以直接告诉我想查订单、物流、退款还是售后，我马上帮您处理。"
    record = mk(
        category="服务反馈", confidence=1.0,
        reason="客户反馈响应较慢，本地即时安抚，不调用模型或转人工",
        status="auto", reply=reply, reply_source="chat",
    )
    record["quick_replies"] = [
        {"label": "查订单 / 物流", "value": "我的快递到哪了？"},
        {"label": "退款 / 售后", "value": "我要退款"},
        {"label": "常见问题", "value": "我想了解退货政策"},
    ]
    return record


def _service_error_record(trace: list, mk, message: str = "抱歉，当前服务响应有些慢，您的问题没有丢失。请稍后重试；您也可以先选择下面的常见问题。") -> dict:
    """模型或上游故障时返回可重试状态，不创建人工工单。"""
    trace.append("service_error")
    record = mk(
        category="服务异常", confidence=0.0,
        reason="上游服务暂时不可用，返回可重试状态，不创建人工工单",
        status="auto", reply=message, reply_source="service_error",
    )
    record["quick_replies"] = [
        {"label": "查订单 / 物流", "value": "我的快递到哪了？"},
        {"label": "退款 / 售后", "value": "我要退款"},
        {"label": "常见问题", "value": "我想了解退货政策"},
    ]
    return record


def is_smalltalk(text: str) -> bool:
    """判断是否为「纯闲聊」——只有问候/寒暄/道谢，不含实质诉求。

    「你好，我买的耳机坏了」这类「问候 + 真实问题」不算闲聊，
    要交给后面的流程（售后/查询）处理，不能被一个「你好」带偏。
    """
    t = text.strip().lower()
    if not t:
        return True
    if _is_assistant_meta_intent(t):
        return True
    if not any(k in t for k in _SMALLTALK_KEYWORDS):
        return False
    # 命中了闲聊词，但去掉问候/客套后仍有实质内容 → 不是纯闲聊
    core = t
    for k in _SMALLTALK_KEYWORDS:
        core = core.replace(k, "")
    core = "".join(ch for ch in core if ch not in "，。,. !?！？~～、 ")
    return len(core) < 4


def _is_followup(text: str, username: str | None) -> bool:
    """判断是否为「对上一轮客服追问的简短承接/回答」。

    场景：客服上一轮问「需要我帮您查具体哪笔订单吗？」，客户回「需要」。
    这种短消息孤立分类必然失败，应直接交给带历史上下文的 Agent 处理。
    """
    t = (text or "").strip()
    if not t or len(t) > 10:
        return False
    history = memory.get_history(username)
    if len(history) < 2:
        return False
    last = history[-1]
    if last.get("role") != "assistant":
        return False
    content = last.get("content", "")
    return "?" in content or "？" in content or "吗" in content


# 查「我自己的数据」的意图关键词（订单/工单/物流/退款进度）。
# 命中则直接交给 Agent 用工具查询，绝不能因分类器「拿不准」而升级人工。
_DATA_QUERY_KEYWORDS = (
    # 查我的订单
    "我的订单", "我的所有订单", "订单列表", "我买了什么", "查订单", "查一下订单", "查下订单", "看下订单",
    # 查我的工单
    "我的工单", "查工单", "工单进度", "我提交的工单", "处理得怎么样",
    # 查我的物流 / 快递
    "我的快递", "查物流", "查快递", "物流到哪", "快递到哪", "到哪了", "什么时候到", "运单",
    # 查我的退款进度
    "退款进度", "退款到账", "退款状态", "退款到哪", "钱什么时候到", "退款已经申请",
)

# 明显的跨场景组合诉求。个人数据查询只有在没有混入其它业务场景时，
# 才能安全地跳过分类器；否则应交给分类器识别多意图并升级人工统一跟进。
_OTHER_BUSINESS_INTENT_KEYWORDS = (
    "退货", "换货", "开票", "发票", "维修", "报修", "保修",
    "参数", "规格", "赔偿",
)

# 含投诉/纠纷/找人工等负面诉求时，不按自助查询处理，仍走分类器正常判定转人工
_COMPLAINT_KEYWORDS = (
    "投诉", "赔偿", "纠纷", "扯皮", "找人工", "转人工", "人工客服", "人工处理",
    "拒绝退款", "拒不退款", "不给退款", "不处理退款", "退款一直不处理",
)


def _is_data_query(text: str) -> bool:
    """判断是否为「查询我自己的订单/工单/物流/退款」这类自助查询意图。"""
    t = text or ""
    if any(k in t for k in _COMPLAINT_KEYWORDS):
        return False
    if any(k in t for k in _OTHER_BUSINESS_INTENT_KEYWORDS):
        return False
    return any(k in t for k in _DATA_QUERY_KEYWORDS) or any(
        k in t for k in ("我的退款", "退款进度", "退款状态", "退款到账")
    )


# 明确要求投诉 / 赔偿 / 纠纷 / 转人工的信号：直接升级人工，绝不查 RAG、不自动作答
_ESCALATE_INTENT_KEYWORDS = (
    "投诉", "赔偿", "纠纷", "扯皮", "转人工", "找人工",
    "拒绝退款", "拒不退款", "不给退款", "不处理退款", "退款一直不处理",
)

_REFUND_OPTIONS = [
    {"label": "未发货，想取消订单", "value": "未发货，想取消订单"},
    {"label": "已收到货，想退货退款", "value": "已收到货，想退货退款"},
    {"label": "商品有质量问题", "value": "商品有质量问题"},
    {"label": "退款已经申请，查询进度", "value": "退款已经申请，查询进度"},
    {"label": "其他退款问题", "value": "其他退款问题"},
]


def _is_refund_clarification_request(text: str) -> bool:
    """判断是否是信息不足的退款申请，而不是退款查询或退款纠纷。"""
    t = (text or "").strip()
    if not any(k in t for k in ("退款", "退钱")):
        return False
    if any(k in t for k in _COMPLAINT_KEYWORDS):
        return False
    if any(k in t for k in ("进度", "状态", "到账", "多久", "什么时候", "查询", "查一下", "查下")):
        return False
    if any(k in t for k in ("物流", "快递", "发票", "维修", "保修")):
        return False
    return True


def _refund_reason_reply(text: str, history: list[dict] | None = None) -> str | None:
    """把退款原因选择转换成下一步，不调用 LLM 猜测业务动作。"""
    t = (text or "").strip()
    in_refund_choice = any(
        item.get("role") == "assistant" and "退款原因" in item.get("content", "")
        for item in (history or [])[-3:]
    )
    if any(k in t for k in ("未发货", "取消订单", "给我取消", "帮我取消", "我要取消")):
        return "好的，我可以帮您取消订单：未发货的订单可以直接取消；如果已发货，需要联系承运商申请拦截。请提供订单号，我先帮您核对状态。"
    if "已收到货" in t or ("退货" in t and "退款" in t):
        return "好的，我来帮您处理退货退款。请提供订单号，并告诉我商品是否已经寄回。"
    if ("质量" in t or "损坏" in t or "破损" in t) and (in_refund_choice or "退款" in t):
        return "好的，商品质量问题需要记录订单号和问题描述。请提供订单号，并说明具体故障；如方便，也可以上传照片。"
    if "其他退款" in t:
        return "请描述一下退款原因和订单号，我会根据具体情况为您安排下一步。"
    return None


def _is_escalate_intent(text: str) -> bool:
    """判断是否为「明确要求升级人工」的意图（投诉/纠纷/赔偿/转人工）。"""
    return any(k in (text or "") for k in _ESCALATE_INTENT_KEYWORDS)


def _is_refund_dispute_intent(text: str) -> bool:
    """识别退款拒绝/拖延等纠纷，保留「退款纠纷」业务分类。"""
    return any(k in (text or "") for k in (
        "拒绝退款", "拒不退款", "不给退款", "不处理退款", "退款一直不处理",
    ))


def _run_agent_safe(question: str, username: str | None) -> str | None:
    """调用 Agent（大模型 + 工具），LLM/API 出错时返回 None，由调用方提示重试。

    这是「接口降级容错」：DeepSeek 超时/报错/未配密钥时，绝不把普通问题误转人工。
    """
    try:
        # 中心 Agent 负责选择专业子 Agent，并统一记录协议和黑板轨迹。
        return multi_agent.run(question, username)
    except Exception as e:  # noqa: BLE001
        logger.exception("Agent 调用失败，返回可重试状态：%s", e)
        return None


def _run_data_query_safe(question: str, username: str | None) -> str | None:
    """个人数据查询走确定性工具编排，不让 LLM 猜订单号或运单号。"""
    try:
        return agent.run_data_query(question, username)
    except Exception as exc:  # noqa: BLE001
        logger.exception("实时业务查询失败，保留真实异常用于诊断：%s", exc)
        return None


def _refund_skill_reply(safe_text: str, history: list[dict], username: str | None) -> str | None:
    """执行退款多轮 Skill；只在上下文确认后处理单独订单号。"""
    if refund_skill.needs_order_id_for_refund_choice(safe_text, history):
        return "好的，我来帮您查询退款进度。请提供订单号，我会先核对这笔订单。"
    order_id = refund_skill.extract_order_id(safe_text)
    if not refund_skill.is_refund_order_followup(safe_text, history):
        return None
    raw = agent._execute_with_mcp_fallback("check_refund", {"order_id": order_id}, username)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw
    if payload.get("found") is False:
        return payload.get("message") or f"未查询到订单 {order_id}，请核对订单号。"
    if payload.get("found") is True:
        return f"订单 {payload.get('order_id') or order_id} 的退款状态：{payload.get('refund_status') or '暂无退款记录'}"
    return raw


def _cancel_skill_reply(safe_text: str, history: list[dict], username: str | None) -> str | None:
    """执行取消订单多轮 Skill，按订单状态选择取消或物流拦截指引。"""
    if not (cancel_skill.is_cancel_order_followup(safe_text, history)
            or cancel_skill.is_direct_cancel_request(safe_text)):
        return None
    if not username:
        return "当前会话未登录，无法办理取消订单。"
    order_id = cancel_skill.extract_order_id(safe_text)
    order = db.get_order_for_user(username, order_id)
    if not order:
        return f"未查询到您名下的订单 {order_id}，请核对订单号后重试。"
    status = str(order.get("status") or "")
    if status == "待发货":
        return agent._cancel_order(order_id, username)
    if status in {"已发货", "运输中", "派送中"}:
        tracking = order.get("tracking_no")
        tracking_hint = f"运单号是 {tracking}，" if tracking else ""
        return (
            f"订单 {order_id} 已经发货，暂时不能直接取消。请尽快联系承运商申请拦截，"
            f"{tracking_hint}如果拦截失败，收到包裹时可以拒收，或签收后申请退货退款。"
        )
    if status in {"已完成", "已签收", "已退货"}:
        return f"订单 {order_id} 当前状态为“{status}”，已经不能取消；如需退回商品，可以申请退货退款。"
    if status == "已取消":
        return f"订单 {order_id} 已经取消，无需重复操作。"
    return f"订单 {order_id} 当前状态为“{status or '未知'}”，暂时不能直接取消，我可以继续帮您核对。"


def _return_skill_reply(safe_text: str, history: list[dict], username: str | None) -> str | None:
    """执行退货办理 Skill，强退诉求不再落入 FAQ 或泛化分类。"""
    active = return_skill.is_return_request(safe_text) or return_skill.is_return_followup(safe_text, history)
    if not active:
        return None
    order_id = return_skill.extract_order_id(safe_text)
    if not order_id:
        return "我可以帮您办理退货。请提供订单号，并告诉我退货原因（例如不想要、商品破损或与描述不符）。"
    if not username:
        return "当前会话未登录，无法办理退货申请。"
    order = db.get_order_for_user(username, order_id)
    if not order:
        return f"未查询到您名下的订单 {order_id}，请核对订单号后重试。"
    reason = return_skill.extract_reason(safe_text)
    status = str(order.get("status") or "")
    if status == "待发货":
        return f"订单 {order_id} 还未发货，直接取消会更快。需要我帮您取消这笔订单吗？"
    if status in {"已发货", "运输中", "派送中"}:
        tracking = order.get("tracking_no")
        tracking_hint = f"运单号是 {tracking}，" if tracking else ""
        return (
            f"订单 {order_id} 已经发货，{tracking_hint}请尽快联系承运商申请拦截；"
            "如果无法拦截，收到包裹时可以拒收，拒收后我再帮您跟进退款。"
        )
    if status in {"已签收", "已完成", "已退货"}:
        issue = reason or "客户申请退货"
        return (
            f"已为您登记退货申请（订单 {order_id}，原因：{issue}）。"
            "客服会核对订单和商品状态后跟进，请保留商品和相关凭证。"
        )
    return f"订单 {order_id} 当前状态为“{status or '未知'}”，我先为您核对退货条件。"


def _classify_safe(safe_text: str, history: list | None) -> dict | None:
    """调用分类器，LLM/API 出错时返回 None（由路由返回可重试状态）。"""
    try:
        return classifier.classify(safe_text, history)
    except Exception as e:  # noqa: BLE001
        logger.exception("分类器调用失败，返回可重试状态：%s", e)
        return None


def _remember_and_schedule(username: str | None, user_text: str, reply: str,
                           extract_profile: bool = True) -> None:
    """写入短期记忆，并异步投递实体画像提取任务。"""
    memory.append(username, "user", user_text)
    memory.append(username, "assistant", reply)
    if extract_profile:
        profile.schedule_extraction(username, user_text, reply)


def process_ticket(ticket_text: str, ground_truth: str | None = None,
                   username: str | None = None, phone: str | None = None,
                   device: str | None = None) -> dict:
    """处理一张工单，返回完整的处理结果字典（含耗时、分类、情绪、回复等）。

    参数：
      ticket_text  工单原文
      ground_truth 真实标签（仅评测时传入，用于后续算准确率）
      username     当前登录用户名（传给 Agent，用于订单归属校验）
      phone        客户联系电话（若有）
      device       渠道/设备信息（web / wechat）
    """
    started = time.perf_counter()
    trace: list = []  # 路由决策留痕（全链路可追溯）

    # 隐私边界：后续分类 / Agent / 落库 / 日志一律用脱敏后的文本，
    # 明文手机号/身份证/银行卡/邮箱不外发第三方 LLM，也不明文落盘。
    safe_text = privacy.mask_sensitive(ticket_text)
    history = memory.get_history(username)
    cache_key = f"{username or 'anon'}:{safe_text}"

    def mk(**overrides) -> dict:
        """拼出带公共字段的记录（留痕 / 电话 / 设备 / 情绪默认值），分支再覆盖差异。"""
        rec = {
            "ticket_text": safe_text,
            "username": username,
            "phone": phone,
            "device": device,
            "ground_truth": ground_truth,
            "emotion": "中性",
            "emotion_intensity": "",
            "multi_intent": 0,
            "rag_chunks": None,
            "route_trace": json.dumps(trace, ensure_ascii=False),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        rec.update(overrides)
        return rec

    # 纯服务反馈走本地即时回复，不调用分类模型，也不创建人工工单。
    if _is_service_feedback(safe_text):
        record = _service_feedback_record(safe_text, trace, mk)
        _remember_and_schedule(username, safe_text, record["reply"], extract_profile=False)
        ticket_logger.info("服务反馈即时安抚：%s", safe_text)
        return record

    service_feedback = _has_service_feedback(safe_text)

    cancel_skill_reply = _cancel_skill_reply(safe_text, history, username)
    if cancel_skill_reply is not None:
        trace.append("cancel_skill")
        record = mk(
            category="订单操作", confidence=1.0,
            reason="取消订单 Skill 根据订单状态执行取消或提供物流拦截方案",
            status="auto", reply=_with_empathy(cancel_skill_reply, service_feedback),
            reply_source="agent",
        )
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    return_skill_reply = _return_skill_reply(safe_text, history, username)
    if return_skill_reply is not None:
        trace.append("return_skill")
        record = mk(
            category="退货申请", confidence=1.0,
            reason="退货 Skill 根据订单状态办理退货或给出拦截/取消方案",
            status="escalated" if "登记退货申请" in return_skill_reply else "auto",
            reply=_with_empathy(return_skill_reply, service_feedback),
            reply_source="escalate" if "登记退货申请" in return_skill_reply else "agent",
        )
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 退款流程 Skill：按钮选择「已申请退款，查询进度」时先收集订单号，
    # 下一轮只有订单号也要保留退款语义，不能落入通用分类或异常兜底。
    refund_skill_reply = _refund_skill_reply(safe_text, history, username)
    if refund_skill_reply is not None:
        trace.append("refund_skill")
        record = mk(
            category="退款咨询", confidence=1.0,
            reason="退款多轮 Skill 根据订单号查询实时退款状态",
            status="auto", reply=_with_empathy(refund_skill_reply, service_feedback),
            reply_source="agent",
        )
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 0. 日常问答：时间/天气等实时问题交给 Agent（DeepSeek 调 get_time/get_weather 工具回答）
    if daily.is_daily(safe_text):
        trace.append("daily")
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("daily:degraded")
            record = _service_error_record(trace, mk, "抱歉，实时信息服务暂时没有响应，您的问题没有丢失。请稍后重试。")
            _remember_and_schedule(username, safe_text, record["reply"], extract_profile=False)
            return record
        record = mk(
            category="日常问答", confidence=1.0,
            reason="时间/天气等日常问题，Agent 调用实时工具回答",
            status="auto", reply=_with_empathy(reply, service_feedback), reply_source="agent",
        )
        ticket_logger.info("日常问答（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 1. 实时业务查询：正常问题，但每次都查当前订单/物流/退款数据，不能复用旧响应缓存。
    if _is_data_query(safe_text):
        trace.append("data_query")
        reply = _run_data_query_safe(safe_text, username)
        if reply is None:
            trace.append("data_query:degraded")
            record = mk(
                category="自助查询", confidence=0.0,
                reason="实时订单/物流/退款查询暂时不可用，保留当前请求并提示重试",
                status="auto",
                reply="抱歉，实时订单/物流查询暂时没有响应，您的问题没有丢失。请稍后点击重试；如果仍无法查询，再联系人工客服处理。",
                reply_source="service_error",
            )
            ticket_logger.warning("实时查询降级为可重试状态，不创建人工工单：%s", safe_text)
            _remember_and_schedule(username, safe_text, record["reply"])
            return record
        record = mk(
            category="自助查询", confidence=1.0,
            reason="正常的实时业务查询，交 Agent 调工具获取当前数据",
            status="auto", reply=_with_empathy(reply, service_feedback), reply_source="agent",
        )
        ticket_logger.info("实时自助查询（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 2. 响应缓存：完全重复的问题直接命中，跳过分类 + 大模型
    cached = cache.get(cache_key)
    if cached is not None:
        trace.append("cache")
        record = dict(cached)
        record["ticket_text"] = safe_text
        record["username"] = username
        record["phone"] = phone
        record["device"] = device
        record["latency_ms"] = int((time.perf_counter() - started) * 1000)
        record["route_trace"] = json.dumps(trace, ensure_ascii=False)
        ticket_logger.info("缓存命中（%s）：%s", cache_key, safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 3. 闲聊检测：问候、寒暄、自我介绍等，交给 DeepSeek 自然回复
    if is_smalltalk(safe_text):
        trace.append("smalltalk")
        reply = responder.chat_reply(safe_text, history, profile.get_context_for_question(username, safe_text))
        record = mk(
            category="闲聊", confidence=1.0,
            reason="问候/寒暄，直接聊天回复",
            status="auto", reply=reply, reply_source="chat",
        )
        ticket_logger.info("闲聊回复：%s", safe_text)
        _remember_and_schedule(username, safe_text, reply)
        cache.set(cache_key, record)  # 闲聊回复确定性较强，缓存加速
        return record

    # 4. 退款纠纷：升级人工，但保留业务分类，不能泛化成「人工处理工单」。
    if _is_refund_dispute_intent(safe_text):
        trace.append("escalate:category")
        record = mk(
            category="退款纠纷", confidence=1.0,
            reason="商家拒绝或拖延退款，属于退款纠纷，需要人工核实",
            status="escalated", emotion="负面", emotion_intensity="high",
            reply="我理解您着急退款的心情。由于商家拒绝或长时间未处理，这属于退款纠纷，我已为您转接人工客服核实处理，请保留订单和沟通记录。",
            reply_source="escalate",
        )
        ticket_logger.info("升级人工（退款纠纷）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 5. 其它升级人工意图：明确投诉/赔偿/转人工，直接升级（先安抚），绝不查 RAG 或自动作答
    if _is_escalate_intent(safe_text):
        trace.append("escalate_intent")
        record = mk(
            category="人工处理工单", confidence=1.0,
            reason="明确投诉/纠纷/赔偿/转人工意图，直接升级人工（先安抚）",
            status="escalated", emotion="负面", emotion_intensity="extreme",
            reply="非常抱歉给您带来不好的体验，我完全理解您的心情。您的问题我已经帮您转接人工客服，请稍候，专员会尽快为您核实处理。",
            reply_source="escalate",
        )
        ticket_logger.info("升级人工（投诉/纠纷意图）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 6. 信息不足的退款申请先澄清原因，不能直接查询、回答或转人工。
    refund_reply = _refund_reason_reply(safe_text, history)
    if refund_reply is not None:
        trace.append("refund_clarification:reason_selected")
        record = mk(
            category="退款咨询", confidence=1.0,
            reason="退款原因已明确，等待订单号或补充信息",
            status="auto", reply=refund_reply, reply_source="clarification",
        )
        ticket_logger.info("退款澄清下一步：%s", safe_text)
        _remember_and_schedule(username, safe_text, refund_reply)
        return record
    if _is_refund_clarification_request(safe_text):
        trace.append("refund_clarification:ask_reason")
        reply = _with_empathy(
            "可以帮您处理退款。为了给您匹配正确流程，请先告诉我退款原因：",
            service_feedback,
        )
        record = mk(
            category="退款咨询", confidence=1.0,
            reason="退款申请缺少原因，先展示常见退款场景供客户选择",
            status="auto", reply=reply, reply_source="clarification",
        )
        record["quick_replies"] = _REFUND_OPTIONS
        ticket_logger.info("退款澄清：等待客户选择原因")
        _remember_and_schedule(username, safe_text, reply)
        return record

    # 7. RAG 优先：知识库命中就直接返回答案，不调大模型（毫秒级，性能关键）
    rag_hit = rag.best_answer(safe_text)
    if rag_hit is not None:
        trace.append(f"rag(distance={rag_hit['distance']:.3f})")
        record = mk(
            category="知识库命中", confidence=1.0,
            reason=f"RAG 知识库命中（距离 {rag_hit['distance']:.3f}），直接返回，未调用大模型",
            status="auto", reply=_with_empathy(rag_hit["answer"], service_feedback), reply_source="rag",
            rag_chunks=json.dumps([rag_hit], ensure_ascii=False),
        )
        ticket_logger.info("RAG 命中（距离 %.3f）：%s", rag_hit["distance"], safe_text)
        _remember_and_schedule(username, safe_text, rag_hit["answer"])
        return record

    # 8. 上下文承接：短消息且上一轮客服在追问 → 直接交给 Agent（带历史理解上下文），跳过孤立分类
    if _is_followup(safe_text, username):
        trace.append("followup")
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("followup:degraded")
            record = _service_error_record(
                trace, mk,
                "抱歉，我暂时没能接上这次追问，但您的问题没有丢失。请稍后重试，也可以重新描述一下需求。",
            )
            ticket_logger.warning("多轮追问 Agent 故障，返回可重试状态：%s", safe_text)
            _remember_and_schedule(username, safe_text, record["reply"], extract_profile=False)
            return record
        record = mk(
            category="多轮追问", confidence=1.0,
            reason="短承接语，结合对话历史交给 Agent 理解，跳过孤立分类",
            status="auto", reply=_with_empathy(reply, service_feedback), reply_source="agent",
        )
        ticket_logger.info("多轮追问（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 9. 分类 + 情绪识别 + 多诉求判断（同一次 LLM 调用内完成）
    result = _classify_safe(safe_text, history)
    if result is None:
        trace.append("classify:degraded")
        record = _service_error_record(trace, mk, "抱歉，当前服务响应有些慢，您的问题没有丢失。请稍后重试；您也可以先选择下面的常见问题。")
        ticket_logger.warning("分类器故障，返回可重试状态：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"], extract_profile=False)
        return record

    category = result["category"]
    confidence = result["confidence"]
    reason = result["reason"]
    emotion = result["emotion"]
    intensity = result["emotion_intensity"]
    multi_intent = 1 if result["multi_intent"] else 0
    trace.append(
        f"classify(category={category},conf={confidence:.2f},"
        f"emotion={emotion}/{intensity},multi={bool(multi_intent)})"
    )

    # 10. 路由判断：决定「自动处理」还是「人工升级」
    #    优先级：极端负面 > 多诉求混杂 > 低置信 > 纠纷类 > 可自动类
    if intensity == "extreme":
        trace.append("escalate:extreme_emotion")
        status, reply_source = "escalated", "escalate"
        reply = "非常抱歉给您带来不好的体验，我完全理解您的心情。您的情况比较特殊，已经帮您转接人工客服优先处理，请稍候，专员会尽快与您联系。"
    elif multi_intent:
        trace.append("clarify:multi_intent")
        status, reply_source = "auto", "clarification"
        reply = "我看到您提到了多个问题。为了更快处理，请告诉我您想先处理订单物流、退款退货，还是商品售后？"
    elif confidence < config.Config.CONFIDENCE_THRESHOLD:
        trace.append("clarify:low_confidence")
        status, reply_source = "auto", "clarification"
        reply = "抱歉，我还没完全理解您的需求。您是想查询订单物流、办理退款退货，还是处理商品售后？"
    elif category in categories.ESCALATE_CATEGORIES:
        trace.append("escalate:category")
        status, reply_source = "escalated", "escalate"
        reply = f"非常抱歉给您带来不好的体验，我完全理解您的心情。您的问题涉及「{category}」，需要人工客服为您进一步核实，已经帮您转接，请稍候，专员会尽快为您处理。"
    elif category in categories.AUTO_CATEGORIES:
        # 所有可自动处理的类别一律交给 Agent：
        # 大模型（DeepSeek）负责理解问题 + 调用工具（RAG 知识库 / 订单 / 物流 / 退款）
        # + 组织自然语言回答，不落硬编码模板。普通负面情绪由 Agent 规则 7 先安抚再回答。
        trace.append("auto:agent")
        status, reply_source = "auto", "agent"
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("auto:agent_degraded")
            record = _service_error_record(trace, mk)
            ticket_logger.warning("Agent 故障，返回可重试状态：%s", safe_text)
            _remember_and_schedule(username, safe_text, record["reply"], extract_profile=False)
            return record
        reply = _with_empathy(
            reply,
            service_feedback or (emotion == "负面" and intensity == "normal"),
        )
    else:
        # 未识别类别先澄清，不把系统理解不足伪装成客户需要人工。
        trace.append("clarify:unknown_category")
        status, reply_source = "auto", "clarification"
        reply = "抱歉，我还没理解清楚。您可以告诉我这是订单物流、退款退货、商品售后，还是其他问题。"

    latency_ms = int((time.perf_counter() - started) * 1000)

    record = mk(
        category=category,
        confidence=confidence,
        reason=reason,
        status=status,
        reply=reply,
        reply_source=reply_source,
        emotion=emotion,
        emotion_intensity=intensity,
        multi_intent=multi_intent,
    )
    record["latency_ms"] = latency_ms

    ticket_logger.info(
        "工单处理完成：类别=%s，置信度=%.2f，情绪=%s(%s)，多诉求=%s，处理方式=%s，回复来源=%s，耗时=%dms",
        category, confidence, emotion, intensity, bool(multi_intent), status, reply_source, latency_ms,
    )
    # 只缓存确定性的模板回复；Agent/RAG 结果依赖实时订单/物流数据，不缓存
    if reply_source == "template":
        cache.set(cache_key, record)
    _remember_and_schedule(username, safe_text, reply)
    return record


def save_processed(record: dict) -> int:
    """把处理结果存入数据库，返回工单 id。"""
    return db.insert_ticket(**record)
