"""用户实体画像记忆：后台提取结构化事实并持久化到 MySQL。"""
from concurrent.futures import ThreadPoolExecutor
import logging
import re
from collections import defaultdict
from threading import Lock

from app import config, db, llm
from prompts import profile as profile_prompt

logger = logging.getLogger("app.profile")

_executor = ThreadPoolExecutor(
    max_workers=max(1, config.Config.PROFILE_MAX_WORKERS),
    thread_name_prefix="profile-memory",
)
_ALLOWED_FACTS = {
    ("person", "name"),
    ("device", "preferred_device"),
    ("preference", "favorite_category"),
    ("preference", "usage_preference"),
    ("after_sale", "issue"),
}
_MAX_VALUE_CHARS = 255
_MAX_EVIDENCE_CHARS = 500
_USER_LOCKS = defaultdict(Lock)
_ORDER_ID_PATTERN = re.compile(r"\b[A-Za-z]\d{3,}\b")

_SUBJECT_KEYS_BY_SCENE = {
    # 订单/物流的权威信息来自订单表，主体只保留称呼，避免偏好和旧售后故障污染查询。
    "order": {("person", "name")},
    "ticket": {
        ("person", "name"), ("device", "preferred_device"),
        ("preference", "usage_preference"), ("after_sale", "issue"),
    },
    "device": {
        ("person", "name"), ("device", "preferred_device"),
        ("preference", "usage_preference"),
    },
    "product": {
        ("person", "name"), ("device", "preferred_device"),
        ("preference", "favorite_category"), ("preference", "usage_preference"),
    },
}


def _subject_context(username: str, question: str | None = None) -> str:
    """返回主体事实；传入问题时只保留当前场景相关的事实。"""
    if question is None:
        return get_context(username)
    return _get_scene_subject_context(username, question)


def _subject_scene(question: str) -> str | None:
    """根据问题粗粒度确定主体画像范围，客体仍由业务表单独召回。"""
    text = (question or "").strip()
    if any(word in text for word in ("订单", "快递", "物流", "运单", "发货", "退款")):
        return "order"
    if any(word in text for word in ("售后", "维修", "故障", "工单", "报修")):
        return "ticket"
    if any(word in text for word in ("设备", "手机", "电脑", "耳机", "平板")):
        return "device"
    if any(word in text for word in ("商品", "产品", "保修", "参数", "喜欢", "偏好")):
        return "product"
    return None


def _get_scene_subject_context(username: str, question: str) -> str:
    """按场景筛选主体事实，保证稳定事实不会遮蔽当前业务对象。"""
    if not username:
        return ""
    try:
        facts = db.list_profile_facts(username)
        scene = _subject_scene(question)
        allowed = _SUBJECT_KEYS_BY_SCENE.get(scene)
        if allowed is not None:
            facts = [
                fact for fact in facts
                if (fact.get("entity_type"), fact.get("fact_key")) in allowed
            ]
        return _format_facts(facts)
    except Exception:  # noqa: BLE001
        logger.exception("按场景读取主体画像失败：%s", username)
        return ""


def _format_order(order: dict) -> str:
    parts = [
        f"订单号：{order.get('order_id', '')}",
        f"商品：{order.get('product', '')}",
        f"状态：{order.get('status', '')}",
    ]
    for label, key in (("运单号", "tracking_no"), ("物流", "logistics"), ("退款", "refund_status")):
        if order.get(key):
            parts.append(f"{label}：{order[key]}")
    return "- " + "；".join(parts)


def _format_ticket(ticket: dict) -> str:
    text = (ticket.get("ticket_text") or "").replace("\n", " ").strip()
    if len(text) > 180:
        text = text[:180] + "..."
    return (
        f"- 工单号：{ticket.get('id', '')}；类型：{ticket.get('category') or '未分类'}；"
        f"状态：{ticket.get('status', '')}；问题：{text}"
    )


def get_context_for_question(username: str | None, question: str) -> str:
    """按当前问题召回主体事实和相关客体，避免把全部业务数据塞给模型。"""
    if not username:
        return ""
    try:
        sections = []
        text = (question or "").strip()
        subject = _subject_context(username, text)
        if subject:
            sections.append("【主体事实】\n" + subject)

        explicit_ids = {match.upper() for match in _ORDER_ID_PATTERN.findall(text)}
        asks_order = any(word in text for word in ("订单", "快递", "物流", "运单", "发货", "退款"))
        asks_ticket = any(word in text for word in ("售后", "维修", "故障", "工单", "报修"))
        asks_order_list = any(word in text for word in ("所有订单", "我的订单", "订单列表"))
        asks_product = any(word in text for word in ("商品", "产品", "保修", "参数"))
        if asks_order or asks_product:
            orders = db.list_orders_for_user(username)
            if explicit_ids:
                orders = [order for order in orders if str(order.get("order_id", "")).upper() in explicit_ids]
            elif not asks_order_list and asks_product:
                orders = [order for order in orders if order.get("product") and order["product"] in text]
            if orders:
                sections.append("【当前相关客体：订单】\n" + "\n".join(_format_order(order) for order in orders[:5]))

        if asks_ticket:
            tickets = db.list_tickets_for_user(username, limit=5)
            if tickets:
                sections.append("【当前相关客体：售后工单】\n" + "\n".join(_format_ticket(ticket) for ticket in tickets))
        return "\n\n".join(sections)
    except Exception:  # noqa: BLE001
        logger.exception("按场景读取用户实体画像失败：%s", username)
        return _subject_context(username)


def _format_facts(facts: list[dict]) -> str:
    """把画像事实格式化为稳定、短小的上下文卡片。"""
    lines = []
    for fact in facts:
        lines.append(
            f"- {fact['entity_type']}.{fact['fact_key']} = {fact['fact_value']}"
        )
    return "\n".join(lines)


def get_context(username: str | None) -> str:
    """返回可注入 Agent 的精确事实卡片。"""
    if not username:
        return ""
    try:
        return _format_facts(db.list_profile_facts(username))
    except Exception:  # noqa: BLE001
        logger.exception("读取用户画像失败，跳过画像上下文：%s", username)
        return ""


def list_facts(username: str) -> list[dict]:
    """返回当前用户的画像事实，供 API 使用。"""
    return db.list_profile_facts(username, include_unconfirmed=True)


def _extract_and_save(username: str, user_text: str, assistant_text: str) -> None:
    """后台任务：调用模型提取事实，并过滤后写库。"""
    with _USER_LOCKS[username]:
        try:
            existing = get_context(username)
            result = llm.complete(
                system=profile_prompt.SYSTEM_PROMPT,
                user=profile_prompt.build_user_prompt(user_text, assistant_text, existing),
                json_mode=True,
                max_tokens=400,
                temperature=0.1,
            )
            facts = result.get("facts", []) if isinstance(result, dict) else []
            if not isinstance(facts, list):
                return

            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                entity_type = str(fact.get("entity_type", "")).strip()
                fact_key = str(fact.get("fact_key", "")).strip()
                value = str(fact.get("fact_value", "")).strip()
                if (entity_type, fact_key) not in _ALLOWED_FACTS or not value:
                    continue
                try:
                    confidence = max(0.0, min(1.0, float(fact.get("confidence", 0))))
                except (TypeError, ValueError):
                    confidence = 0.0
                confirmed = fact.get("confirmed", False) is True
                if confidence < 0.8 and not confirmed:
                    continue
                previous = db.get_profile_fact(username, entity_type, fact_key)
                if previous and previous.get("confirmed") and not confirmed:
                    continue
                db.upsert_profile_fact(
                    username=username,
                    entity_type=entity_type,
                    fact_key=fact_key,
                    fact_value=value[:_MAX_VALUE_CHARS],
                    source="conversation",
                    evidence=user_text[:_MAX_EVIDENCE_CHARS],
                    confidence=confidence,
                    confirmed=confirmed,
                )
            logger.info("用户画像异步提取完成：%s", username)
        except Exception:  # noqa: BLE001
            logger.exception("用户画像异步提取失败：%s", username)


def schedule_extraction(username: str | None, user_text: str, assistant_text: str) -> None:
    """投递画像提取任务；没有登录身份时不建立画像。"""
    if not username or not user_text or not assistant_text:
        return
    try:
        _executor.submit(_extract_and_save, username, user_text, assistant_text)
    except RuntimeError:
        logger.warning("用户画像线程池已关闭，跳过本轮提取：%s", username)


def delete_fact(username: str, fact_id: int) -> bool:
    """删除当前用户的一条画像事实。"""
    return db.delete_profile_fact(username, fact_id)


def update_fact(username: str, fact_id: int, value: str, confirmed: bool = True) -> dict | None:
    """手工修正当前用户的一条画像事实。"""
    value = (value or "").strip()
    if not value:
        raise ValueError("画像事实不能为空")
    return db.update_profile_fact(
        username, fact_id, value[:_MAX_VALUE_CHARS], confirmed=confirmed
    )
