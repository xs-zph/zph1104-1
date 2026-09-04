"""场景化 Skill 注册与召回。

Skill 负责描述“什么时候应该候选这个工具”，真正的参数校验和权限校验
仍由 Agent 工具执行层负责。召回采用轻量规则评分，避免每轮再增加一次
LLM 调用；后续可以在 ``retrieve`` 内替换为向量 + reranker。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SkillSpec:
    """一个可被 Agent 调用的 Skill 元数据。"""

    name: str
    scene: str
    triggers: tuple[str, ...]
    required_entities: tuple[str, ...] = ()
    risk: str = "low"
    base_score: int = 0


SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        "search_faq", "knowledge",
        ("政策", "流程", "规则", "保修", "退货", "发票", "怎么用", "使用", "优惠", "活动"),
        risk="low", base_score=2,
    ),
    SkillSpec(
        "list_my_orders", "order",
        ("我的订单", "查订单", "订单", "我买的", "我的耳机", "我的商品"),
        risk="low", base_score=3,
    ),
    SkillSpec(
        "query_order", "order",
        ("订单号", "订单状态", "订单进度"),
        required_entities=("order_id",), risk="low", base_score=2,
    ),
    SkillSpec(
        "query_logistics", "logistics",
        ("物流", "快递", "运单", "配送", "什么时候到", "到哪了"),
        required_entities=("tracking_no",), risk="low", base_score=3,
    ),
    SkillSpec(
        "check_refund", "refund",
        ("退款", "退钱", "钱到账", "退款进度"),
        required_entities=("order_id",), risk="medium", base_score=3,
    ),
    SkillSpec(
        "cancel_order", "order_action",
        ("取消订单", "不要了", "撤销订单"),
        required_entities=("order_id",), risk="high", base_score=4,
    ),
    SkillSpec(
        "check_my_tickets", "ticket",
        ("我的工单", "工单进度", "处理进度", "人工回复"),
        risk="low", base_score=3,
    ),
    SkillSpec(
        "apply_after_sale", "after_sale",
        ("申请售后", "售后维修", "维修", "坏了", "故障", "无法开机", "有杂音"),
        required_entities=("order_id", "issue"), risk="high", base_score=4,
    ),
    SkillSpec(
        "get_time", "realtime",
        ("几点", "几号", "星期几", "日期", "时间"),
        risk="low", base_score=5,
    ),
    SkillSpec(
        "get_weather", "realtime",
        ("天气", "下雨", "温度", "冷不冷", "热不热"),
        risk="low", base_score=5,
    ),
)

_SCENE_TERMS = {
    "order": ("订单", "我买的", "我的商品"),
    "order_action": ("取消订单", "撤销订单", "不要了"),
    "logistics": ("物流", "快递", "运单", "配送", "到哪", "什么时候到"),
    "refund": ("退款", "退钱", "钱到账"),
    "after_sale": ("售后", "维修", "故障", "坏了", "无法开机"),
    "knowledge": ("政策", "流程", "保修", "退货", "发票", "怎么用", "优惠"),
    "ticket": ("工单", "人工回复", "处理进度"),
    "realtime": ("几点", "几号", "星期", "天气", "下雨", "温度"),
}


def _score(spec: SkillSpec, question: str, scenes: set[str]) -> int:
    score = spec.base_score
    score += sum(3 for term in spec.triggers if term in question)
    if spec.scene in scenes:
        score += 4
    return score


def retrieve(question: str, top_k: int = 3) -> tuple[SkillSpec, ...]:
    """召回当前问题的 Top-K Skill，至少保留一个通用知识查询能力。"""
    text = (question or "").strip().lower()
    scenes = {
        scene for scene, terms in _SCENE_TERMS.items()
        if any(term.lower() in text for term in terms)
    }
    ranked = sorted(
        ((spec, _score(spec, text, scenes)) for spec in SKILLS),
        key=lambda item: (-item[1], item[0].name),
    )
    selected = [spec for spec, score in ranked if score > spec.base_score]
    if not selected:
        selected = [next(spec for spec in SKILLS if spec.name == "search_faq")]
    return tuple(selected[:max(1, top_k)])


def select_tools(question: str, tools: list[dict], top_k: int = 3) -> list[dict]:
    """按 Skill 名称从 Agent 工具定义中筛选候选工具。"""
    selected_names = {spec.name for spec in retrieve(question, top_k=top_k)}
    return [
        tool for tool in tools
        if tool.get("function", {}).get("name") in selected_names
    ]


def context(question: str, top_k: int = 3) -> str:
    """生成注入 Agent 的本轮 Skill 候选说明。"""
    selected = retrieve(question, top_k=top_k)
    lines = [
        "【本轮 Skill 候选范围】",
        "只使用本轮传入的候选工具；若候选工具无法解决问题，说明原因并转人工，不要臆造工具。",
    ]
    lines.extend(
        f"- {spec.name}（场景：{spec.scene}，风险：{spec.risk}，需要实体：{','.join(spec.required_entities) or '无'}）"
        for spec in selected
    )
    return "\n".join(lines)
