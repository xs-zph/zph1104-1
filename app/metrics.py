"""指标统计模块：把数据库里的工单记录汇总成可视化指标。

对应简历里的几个量化指标：
  - 自动处理率  = 自动处理工单数 / 工单总数
  - 分类准确率  = 分类正确数 / 有真实标签的工单数
  - 平均处理耗时 = 平均每张工单耗时
"""
from collections import Counter, defaultdict

from app import db


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
