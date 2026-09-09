"""指标统计模块：把数据库里的工单记录汇总成可视化指标。

对应简历里的几个量化指标：
  - 自动处理率  = 自动处理工单数 / 工单总数
  - 分类准确率  = 分类正确数 / 有真实标签的工单数
  - 平均处理耗时 = 平均每张工单耗时
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app import db
from app import rag
from app import rag_eval


def overview() -> dict:
    """返回系统的核心指标概览。"""
    tickets = db.list_all_tickets()
    total = len(tickets)

    if total == 0:
        return {"total": 0}

    auto_count = sum(1 for t in tickets if t["status"] == "auto")
    escalated_count = total - auto_count

    # 平均耗时（毫秒）
    latencies = [t["latency_ms"] for t in tickets if t.get("latency_ms")]
    avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0

    # 分类准确率（只统计有真实标签的工单）
    labeled = [t for t in tickets if t.get("ground_truth")]
    correct = sum(1 for t in labeled if t["category"] == t["ground_truth"])
    accuracy = correct / len(labeled) if labeled else None

    # 各类别数量
    category_dist = dict(Counter(t["category"] for t in tickets if t["category"]))

    # 情绪分布（负面 / 中性 / 正面）
    emotion_dist = dict(Counter(t["emotion"] for t in tickets if t.get("emotion")))
    negative_count = emotion_dist.get("负面", 0)
    negative_ratio = round(negative_count / total, 4) if total else 0.0

    # 满意度（👍/👎）
    feedback_up = sum(1 for t in tickets if t.get("feedback") == "up")
    feedback_down = sum(1 for t in tickets if t.get("feedback") == "down")

    return {
        "total": total,
        "auto_count": auto_count,
        "escalated_count": escalated_count,
        "auto_rate": round(auto_count / total, 4),             # 自动处理率
        "accuracy": round(accuracy, 4) if accuracy is not None else None,  # 分类准确率
        "avg_latency_ms": round(avg_latency_ms, 1),             # 平均耗时
        "category_distribution": category_dist,
        "emotion_distribution": emotion_dist,                   # 情绪分布
        "negative_ratio": negative_ratio,                       # 负面情绪占比
        "feedback_up": feedback_up,
        "feedback_down": feedback_down,
        "satisfaction_rate": round(feedback_up / (feedback_up + feedback_down), 4)
        if (feedback_up + feedback_down) else None,            # 满意度
    }


def category_accuracy() -> dict:
    """按类别统计分类准确率（用于定位哪个类别容易分错）。"""
    tickets = [t for t in db.list_all_tickets() if t.get("ground_truth")]
    stat = defaultdict(lambda: {"total": 0, "correct": 0})
    for t in tickets:
        gt = t["ground_truth"]
        stat[gt]["total"] += 1
        if t["category"] == gt:
            stat[gt]["correct"] += 1
    return {
        k: {"total": v["total"], "correct": v["correct"],
            "accuracy": round(v["correct"] / v["total"], 4)}
        for k, v in stat.items()
    }


def top_questions(n: int = 10) -> list[dict]:
    """TOP 问题：出现次数最多的问题文本（发现高频咨询，指导补充知识库）。"""
    counter = Counter(t["ticket_text"] for t in db.list_all_tickets() if t.get("ticket_text"))
    return [{"question": q, "count": c} for q, c in counter.most_common(n)]


def emotion_trend(days: int = 7) -> list[dict]:
    """按天统计情绪分布（负面/中性/正面），用于观察客户情绪趋势。"""
    per_day = defaultdict(lambda: {"负面": 0, "中性": 0, "正面": 0})
    for t in db.list_all_tickets():
        created = t.get("created_at")
        if not created:
            continue
        day = str(created)[:10]  # YYYY-MM-DD
        emo = t.get("emotion") or "中性"
        if emo in per_day[day]:
            per_day[day][emo] += 1

    today = datetime.now().date()
    result = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": day, **per_day.get(day, {"负面": 0, "中性": 0, "正面": 0})})
    return result


def manager_dashboard(days: int = 7) -> dict:
    """经理视角的人工处理效果与客服负载数据。"""
    tickets = db.list_all_tickets()
    overview_data = overview()
    human_statuses = {"escalated", "in_progress", "waiting_customer", "resolved", "closed"}
    active_statuses = {"escalated", "in_progress", "waiting_customer"}
    human_tickets = [ticket for ticket in tickets if ticket.get("status") in human_statuses]
    resolved_tickets = [ticket for ticket in human_tickets if ticket.get("status") == "resolved"]
    active_tickets = [ticket for ticket in human_tickets if ticket.get("status") in active_statuses]
    breached = [ticket for ticket in human_tickets if ticket.get("sla_breached")]

    by_agent: dict[str, dict] = {}
    for ticket in human_tickets:
        agent = ticket.get("assigned_to") or "未分配"
        item = by_agent.setdefault(agent, {"agent": agent, "handled": 0, "resolved": 0, "open": 0})
        item["handled"] += 1
        if ticket.get("status") == "resolved":
            item["resolved"] += 1
        if ticket.get("status") in active_statuses:
            item["open"] += 1

    per_day = defaultdict(lambda: {"total": 0, "handoff": 0, "resolved": 0})
    for ticket in tickets:
        created = ticket.get("created_at")
        if not created:
            continue
        day = str(created)[:10]
        per_day[day]["total"] += 1
        if ticket.get("status") in human_statuses:
            per_day[day]["handoff"] += 1
        if ticket.get("status") == "resolved":
            per_day[day]["resolved"] += 1

    today = datetime.now().date()
    trend = []
    for index in range(days - 1, -1, -1):
        day = (today - timedelta(days=index)).strftime("%Y-%m-%d")
        trend.append({"date": day, **per_day[day]})

    return {
        **overview_data,
        "human_total": len(human_tickets),
        "human_resolved": len(resolved_tickets),
        "human_open": len(active_tickets),
        "human_resolution_rate": round(len(resolved_tickets) / len(human_tickets), 4)
        if human_tickets else None,
        "sla_breached": len(breached),
        "agent_performance": sorted(by_agent.values(), key=lambda item: (-item["handled"], item["agent"])),
        "daily_workload": trend,
        "rag_quality": rag_quality(),
    }


def rag_quality() -> dict:
    """返回离线检索质量和线上缓存节省情况。"""
    try:
        evaluation = rag_eval.evaluate()
    except Exception:
        evaluation = {}
    return {
        "recall_at_5": evaluation.get("recall_at_k"),
        "precision_at_3": evaluation.get("precision_at_k"),
        "hit_rate": evaluation.get("hit_rate"),
        "case_count": evaluation.get("case_count", 0),
        "guard_pass_rate": evaluation.get("guard_pass_rate"),
        "cache": rag.cache_stats(),
    }
