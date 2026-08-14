"""路由模块：系统的「大脑」，决定每张工单走哪条处理路径。

处理流程（核心业务逻辑）：
  1. 先调用分类器，得到 类别 + 置信度；
  2. 判断是否升级人工：
     - 置信度 < 阈值（拿不准） → 升级人工
     - 类别是「投诉」或「其他」    → 升级人工
  3. 否则自动处理：
     - 类别是「FAQ咨询」 → RAG 知识库回复
     - 其他可自动类别     → 模板回复
"""
import logging
import time

from app import agent, cache, categories, classifier, config, daily, db, memory, privacy, rag, responder

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
    """判断是否为闲聊/寒暄（问候、自我介绍、道谢、告别）。"""
    t = text.strip().lower()
    if not t:
        return True
    return any(k in t for k in _SMALLTALK_KEYWORDS)


def process_ticket(ticket_text: str, ground_truth: str | None = None,
                   username: str | None = None) -> dict:
    """处理一张工单，返回完整的处理结果字典（含耗时、分类、回复等）。

    参数：
      ticket_text  工单原文
      ground_truth 真实标签（仅评测时传入，用于后续算准确率）
      username     当前登录用户名（传给 Agent，用于订单归属校验）
    """
    started = time.perf_counter()

    # 隐私边界：后续分类 / Agent / 落库 / 日志一律用脱敏后的文本，
    # 明文手机号/身份证/银行卡/邮箱不外发第三方 LLM，也不明文落盘。
    safe_text = privacy.mask_sensitive(ticket_text)
    history = memory.get_history(username)
    cache_key = f"{username or 'anon'}:{safe_text}"

    # 0. 日常问答：时间/天气等实时问题交给 Agent（DeepSeek 调 get_time/get_weather 工具回答）
    if daily.is_daily(safe_text):
        reply = agent.run_agent(safe_text, username)
        record = {
            "ticket_text": safe_text,
            "username": username,
            "category": "日常问答",
            "ground_truth": ground_truth,
            "confidence": 1.0,
            "reason": "时间/天气等日常问题，Agent 调用实时工具回答",
            "status": "auto",
            "reply": reply,
            "reply_source": "agent",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        ticket_logger.info("日常问答（Agent）：%s", safe_text)
        memory.append(username, "user", safe_text)
        memory.append(username, "assistant", reply)
        return record

    # 1. 响应缓存：完全重复的问题直接命中，跳过分类 + 大模型
    cached = cache.get(cache_key)
    if cached is not None:
        record = dict(cached)
        record["ticket_text"] = safe_text
        record["username"] = username
        record["latency_ms"] = int((time.perf_counter() - started) * 1000)
        ticket_logger.info("缓存命中（%s）：%s", cache_key, safe_text)
        memory.append(username, "user", safe_text)
        memory.append(username, "assistant", record["reply"])
        return record

    # 2. 闲聊检测：问候、寒暄、自我介绍等，交给 DeepSeek 自然回复
    if is_smalltalk(safe_text):
        reply = responder.chat_reply(safe_text, history)
        record = {
            "ticket_text": safe_text,
            "username": username,
            "category": "闲聊",
            "ground_truth": ground_truth,
            "confidence": 1.0,
            "reason": "问候/寒暄，直接聊天回复",
            "status": "auto",
            "reply": reply,
            "reply_source": "chat",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        ticket_logger.info("闲聊回复：%s", safe_text)
        memory.append(username, "user", safe_text)
        memory.append(username, "assistant", reply)
        cache.set(cache_key, record)  # 闲聊回复确定性较强，缓存加速
        return record

    # 3. RAG 优先：知识库命中就直接返回答案，不调大模型（毫秒级，性能关键）
    rag_hit = rag.best_answer(safe_text)
    if rag_hit is not None:
        record = {
            "ticket_text": safe_text,
            "username": username,
            "category": "知识库命中",
            "ground_truth": ground_truth,
            "confidence": 1.0,
            "reason": f"RAG 知识库命中（距离 {rag_hit['distance']:.3f}），直接返回，未调用大模型",
            "status": "auto",
            "reply": rag_hit["answer"],
            "reply_source": "rag",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        ticket_logger.info("RAG 命中（距离 %.3f）：%s", rag_hit["distance"], safe_text)
        memory.append(username, "user", safe_text)
        memory.append(username, "assistant", rag_hit["answer"])
        return record

    # 4. 分类
    result = classifier.classify(safe_text)
    category = result["category"]
    confidence = result["confidence"]
    reason = result["reason"]

    # 5. 路由判断：决定「自动处理」还是「人工升级」
    if confidence < config.Config.CONFIDENCE_THRESHOLD:
        status, reply_source = "escalated", "escalate"
        reply = "抱歉，这个问题我暂时无法准确判断，已经帮您转接人工客服，请稍候。"
    elif category in categories.ESCALATE_CATEGORIES:
        status, reply_source = "escalated", "escalate"
        reply = f"您的问题涉及「{category}」，需要人工客服为您进一步核实处理，已经帮您转接，请稍候。"
    elif category in categories.AUTO_CATEGORIES:
        # 所有可自动处理的类别一律交给 Agent：
        # 大模型（DeepSeek）负责理解问题 + 调用工具（RAG 知识库 / 订单 / 物流 / 退款）
        # + 组织自然语言回答，不落硬编码模板。
        status, reply_source = "auto", "agent"
        reply = agent.run_agent(safe_text, username)
    else:
        # 兜底：未识别的类别一律转人工，绝不擅自作答
        status, reply_source = "escalated", "escalate"
        reply = "抱歉，这个问题我需要人工客服为您进一步核实，已经帮您转接，请稍候。"

    latency_ms = int((time.perf_counter() - started) * 1000)

    record = {
        "ticket_text": safe_text,
        "username": username,
        "category": category,
        "ground_truth": ground_truth,
        "confidence": confidence,
        "reason": reason,
        "status": status,
        "reply": reply,
        "reply_source": reply_source,
        "latency_ms": latency_ms,
    }

    ticket_logger.info(
        "工单处理完成：类别=%s，置信度=%.2f，处理方式=%s，回复来源=%s，耗时=%dms",
        category, confidence, status, reply_source, latency_ms,
    )
    # 只缓存确定性的模板回复；Agent/RAG 结果依赖实时订单/物流数据，不缓存
    if reply_source == "template":
        cache.set(cache_key, record)
    memory.append(username, "user", safe_text)
    memory.append(username, "assistant", reply)
    return record


def save_processed(record: dict) -> int:
    """把处理结果存入数据库，返回工单 id。"""
    return db.insert_ticket(**record)
