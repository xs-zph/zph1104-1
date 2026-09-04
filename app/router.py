"""路由模块：系统的「大脑」，决定每张工单走哪条处理路径。

处理流程（按优先级从高到低）：
  0. 日常问答（时间/天气）         → Agent 调实时工具
  1. 响应缓存命中                  → 直接返回
  2. 闲聊（问候/寒暄）             → 聊天回复
  3. 明确投诉/纠纷/赔偿/转人工     → 直接升级人工（先安抚）
  4. RAG 知识库命中                → 直接返回知识库答案（毫秒级）
  5. 多轮追问承接                  → 交 Agent（带历史）
  6. 自助查询（订单/工单/物流/退款）→ 交 Agent 调工具
  7. 分类 → 低置信或投诉/纠纷类 → 升级人工；可自动类 → Agent
  8. 兜底 → 转人工
"""
import json
import logging
import time

from app import cache, categories, classifier, config, daily, db, memory, multi_agent, privacy, profile, rag, responder

logger = logging.getLogger("app.router")
ticket_logger = logging.getLogger("tickets")

# 常见问候/寒暄关键词：命中则走「闲聊」通道，不进入工单分类
_SMALLTALK_KEYWORDS = (
    "你好", "您好", "在吗", "在不在", "hi", "hello", "hey", "嗨", "哈喽",
    "早上好", "中午好", "下午好", "晚上好", "早安", "晚安",
    "你是谁", "你叫什么", "介绍一下", "你是机器人", "你是真人", "你是ai", "你是AI", "你是智能",
    "你能做什么", "你能干什么", "你能帮我什么", "有什么功能", "你会什么", "你可以做什么",
    "谢谢", "感谢", "辛苦了", "再见", "拜拜",
)


def is_smalltalk(text: str) -> bool:
    """判断是否为「纯闲聊」——只有问候/寒暄/道谢，不含实质诉求。

    「你好，我买的耳机坏了」这类「问候 + 真实问题」不算闲聊，
    要交给后面的流程（售后/查询）处理，不能被一个「你好」带偏。
    """
    t = text.strip().lower()
    if not t:
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
    "退款进度", "退款到账", "退款状态", "退款到哪", "钱什么时候到",
)

# 含投诉/纠纷/找人工等负面诉求时，不按自助查询处理，仍走分类器正常判定转人工
_COMPLAINT_KEYWORDS = ("投诉", "赔偿", "纠纷", "扯皮", "找人工", "转人工", "人工客服", "人工处理")


def _is_data_query(text: str) -> bool:
    """判断是否为「查询我自己的订单/工单/物流/退款」这类自助查询意图。"""
    t = text or ""
    if any(k in t for k in _COMPLAINT_KEYWORDS):
        return False
    return any(k in t for k in _DATA_QUERY_KEYWORDS)


# 明确要求投诉 / 赔偿 / 纠纷 / 转人工的信号：直接升级人工，绝不查 RAG、不自动作答
_ESCALATE_INTENT_KEYWORDS = ("投诉", "赔偿", "纠纷", "扯皮", "转人工", "找人工")


def _is_escalate_intent(text: str) -> bool:
    """判断是否为「明确要求升级人工」的意图（投诉/纠纷/赔偿/转人工）。"""
    return any(k in (text or "") for k in _ESCALATE_INTENT_KEYWORDS)


def _run_agent_safe(question: str, username: str | None) -> str | None:
    """调用 Agent（大模型 + 工具），LLM/API 出错时返回 None，由调用方降级转人工。

    这是「接口降级容错」：DeepSeek 超时/报错/未配密钥时，绝不 500 或吞单，
    而是走「转人工」兜底，保证客户问题不丢。
    """
    try:
        # 中心 Agent 负责选择专业子 Agent，并统一记录协议和黑板轨迹。
        return multi_agent.run(question, username)
    except Exception as e:  # noqa: BLE001
        logger.exception("Agent 调用失败，降级转人工：%s", e)
        return None


def _classify_safe(safe_text: str, history: list | None) -> dict | None:
    """调用分类器，LLM/API 出错时返回 None（降级转人工）。"""
    try:
        return classifier.classify(safe_text, history)
    except Exception as e:  # noqa: BLE001
        logger.exception("分类器调用失败，降级转人工：%s", e)
        return None


def _remember_and_schedule(username: str | None, user_text: str, reply: str) -> None:
    """写入短期记忆，并异步投递实体画像提取任务。"""
    memory.append(username, "user", user_text)
    memory.append(username, "assistant", reply)
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

    # 0. 日常问答：时间/天气等实时问题交给 Agent（DeepSeek 调 get_time/get_weather 工具回答）
    if daily.is_daily(safe_text):
        trace.append("daily")
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("daily:degraded")
            reply = "非常抱歉，实时信息查询暂时不可用，请稍后再试。"
        record = mk(
            category="日常问答", confidence=1.0,
            reason="时间/天气等日常问题，Agent 调用实时工具回答",
            status="auto", reply=reply, reply_source="agent",
        )
        ticket_logger.info("日常问答（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, reply)
        return record

    # 1. 响应缓存：完全重复的问题直接命中，跳过分类 + 大模型
    cached = cache.get(cache_key)
    if cached is not None:
        trace.append("cache")
        record = dict(cached)
        record["ticket_text"] = safe_text
        record["username"] = username
        record["phone"] = phone
        record["device"] = device
        record["latency_ms"] = int((time.perf_counter() - started) * 1000)
        ticket_logger.info("缓存命中（%s）：%s", cache_key, safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
        return record

    # 2. 闲聊检测：问候、寒暄、自我介绍等，交给 DeepSeek 自然回复
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

    # 3. 升级人工意图：明确投诉/纠纷/赔偿/转人工，直接升级（先安抚），绝不查 RAG 或自动作答
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

    # 4. RAG 优先：知识库命中就直接返回答案，不调大模型（毫秒级，性能关键）
    rag_hit = rag.best_answer(safe_text)
    if rag_hit is not None:
        trace.append(f"rag(distance={rag_hit['distance']:.3f})")
        record = mk(
            category="知识库命中", confidence=1.0,
            reason=f"RAG 知识库命中（距离 {rag_hit['distance']:.3f}），直接返回，未调用大模型",
            status="auto", reply=rag_hit["answer"], reply_source="rag",
            rag_chunks=json.dumps([rag_hit], ensure_ascii=False),
        )
        ticket_logger.info("RAG 命中（距离 %.3f）：%s", rag_hit["distance"], safe_text)
        _remember_and_schedule(username, safe_text, rag_hit["answer"])
        return record

    # 5. 上下文承接：短消息且上一轮客服在追问 → 直接交给 Agent（带历史理解上下文），跳过孤立分类
    if _is_followup(safe_text, username):
        trace.append("followup")
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("followup:degraded")
            record = mk(
                category="人工处理工单", confidence=0.0,
                reason="多轮追问 Agent 调用失败，降级转人工",
                status="escalated",
                reply="非常抱歉，系统暂时无法理解您的追问。为了不耽误您，已经帮您转接人工客服，请稍候。",
                reply_source="escalate",
            )
            ticket_logger.info("升级人工（多轮追问降级）：%s", safe_text)
            _remember_and_schedule(username, safe_text, record["reply"])
            return record
        record = mk(
            category="多轮追问", confidence=1.0,
            reason="短承接语，结合对话历史交给 Agent 理解，跳过孤立分类",
            status="auto", reply=reply, reply_source="agent",
        )
        ticket_logger.info("多轮追问（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, reply)
        return record

    # 6. 自助查询：查「我的订单/工单/物流/退款进度」直接交 Agent 调工具，
    #    跳过分类器——这类个人数据查询不是投诉/纠纷，不该因「拿不准」升级人工。
    if _is_data_query(safe_text):
        trace.append("data_query")
        reply = _run_agent_safe(safe_text, username)
        if reply is None:
            trace.append("data_query:degraded")
            record = mk(
                category="人工处理工单", confidence=0.0,
                reason="自助查询 Agent 调用失败，降级转人工",
                status="escalated",
                reply="非常抱歉，订单/工单查询暂时不可用。为了不耽误您，已经帮您转接人工客服，请稍候。",
                reply_source="escalate",
            )
            ticket_logger.info("升级人工（自助查询降级）：%s", safe_text)
            _remember_and_schedule(username, safe_text, record["reply"])
            return record
        record = mk(
            category="自助查询", confidence=1.0,
            reason="个人数据查询（订单/工单/物流/退款），直接交 Agent 调工具查询",
            status="auto", reply=reply, reply_source="agent",
        )
        ticket_logger.info("自助查询（Agent）：%s", safe_text)
        _remember_and_schedule(username, safe_text, reply)
        return record

    # 7. 分类 + 情绪识别 + 多诉求判断（同一次 LLM 调用内完成）
    result = _classify_safe(safe_text, history)
    if result is None:
        trace.append("classify:degraded")
        record = mk(
            category="人工处理工单", confidence=0.0,
            reason="分类器调用失败，降级转人工",
            status="escalated", emotion="负面",
            reply="非常抱歉，系统正在升级维护。为了不耽误您，已经帮您转接人工客服，请稍候。",
            reply_source="escalate",
        )
        ticket_logger.info("升级人工（分类器降级）：%s", safe_text)
        _remember_and_schedule(username, safe_text, record["reply"])
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

    # 8. 路由判断：决定「自动处理」还是「人工升级」
    #    优先级：极端负面 > 多诉求混杂 > 低置信 > 纠纷类 > 可自动类
    if intensity == "extreme":
        trace.append("escalate:extreme_emotion")
        status, reply_source = "escalated", "escalate"
        reply = "非常抱歉给您带来不好的体验，我完全理解您的心情。您的情况比较特殊，已经帮您转接人工客服优先处理，请稍候，专员会尽快与您联系。"
    elif multi_intent:
        trace.append("escalate:multi_intent")
        status, reply_source = "escalated", "escalate"
        reply = "您一次提到了多个问题，为了准确高效地帮您处理，已经帮您转接人工客服统一跟进，请稍候，专员会尽快为您服务。"
    elif confidence < config.Config.CONFIDENCE_THRESHOLD:
        trace.append("escalate:low_confidence")
        status, reply_source = "escalated", "escalate"
        reply = "非常抱歉，我一时没能准确理解您的意思，让您久等了。为了不耽误您，已经帮您转接人工客服，请稍候，专员会尽快为您处理。"
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
            status, reply_source = "escalated", "escalate"
            reply = "非常抱歉，系统正在升级维护。为了不耽误您，已经帮您转接人工客服，请稍候。"
    else:
        # 兜底：未识别的类别一律转人工，绝不擅自作答
        trace.append("escalate:unknown_category")
        status, reply_source = "escalated", "escalate"
        reply = "非常抱歉没能马上帮您解决，让您久等了。已经帮您转接人工客服，请稍候，专员会尽快为您核实处理。"

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
