"""阈值调优脚本：python scripts/tune_threshold.py [数量]

回答「分类置信度阈值 0.6 是怎么来的」——它不是拍脑袋，而是在带真实标签的
评测集上做「阈值扫描」，挑出「误转人工 + 漏转人工」综合错误率最低的值。

做法：
  1. 用 gen_tickets 生成一批带 ground_truth 的工单；
  2. 对每条工单跑分类器，得到 (预测类别, 置信度)；
  3. 对每个候选阈值 T，复现 router.py 的路由逻辑，统计两类错误：
     - 误转人工（false escalate）：本该自动处理的，被转给人工 → 浪费人力
     - 漏转人工（missed escalate）：本该转人工的，被自动处理了 → 风险最高
  4. 打印每个阈值下的自动处理率 + 两类错误率，推荐综合最优阈值。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import categories, classifier
from app.config import setup_logging
from scripts import gen_tickets

# 候选阈值
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

# 真实标签 → 「该自动处理」还是「该转人工」
AUTO_TRUTH = {"物流查询", "退货申请", "商品咨询", "售后维修", "发票问题"}
ESCALATE_TRUTH = {"退款纠纷", "人工处理工单"}


def route(category: str, confidence: float, threshold: float) -> str:
    """复现 router.py 里的路由逻辑（只看置信度阈值这一刀）。"""
    if confidence < threshold:
        return "escalated"
    if category in categories.ESCALATE_CATEGORIES:
        return "escalated"
    return "auto"


def sweep(tickets: list[dict]):
    # 先对每条工单分类一次（缓存结果，避免每个阈值重复调模型）
    results = []
    print(f"正在对 {len(tickets)} 条工单分类...\n")
    for i, t in enumerate(tickets, start=1):
        r = classifier.classify(t["ticket_text"])
        results.append({
            "truth": t["ground_truth"],
            "pred": r["category"],
            "conf": r["confidence"],
        })
        print(f"[{i:>3}/{len(tickets)}] 真实={t['ground_truth']:<6} | "
              f"预测={r['category']:<6} | 置信度={r['confidence']:.2f}")

    print("\n" + "=" * 78)
    print(f"{'阈值':<7}{'自动处理率':>11}{'误转人工率':>11}{'漏转人工率':>11}{'总错误率':>11}")
    best = None
    for T in THRESHOLDS:
        auto_total = fa = miss = 0
        for r in results:
            decision = route(r["pred"], r["conf"], T)
            if decision == "auto":
                auto_total += 1
            # 误转人工：真实是「自动类」，却被转人工
            if r["truth"] in AUTO_TRUTH and decision == "escalated":
                fa += 1
            # 漏转人工：真实是「转人工类」，却被自动处理
            if r["truth"] in ESCALATE_TRUTH and decision == "auto":
                miss += 1
        n = len(results)
        auto_rate, fa_rate, miss_rate = auto_total / n, fa / n, miss / n
        total_err = fa_rate + miss_rate
        print(f"{T:<7.1f}{auto_rate:>10.1%}{fa_rate:>11.1%}{miss_rate:>11.1%}{total_err:>11.1%}")
        if best is None or total_err < best[1]:
            best = (T, total_err, auto_rate)

    print("=" * 78)
    print(f"推荐阈值：{best[0]:.1f}（总错误率最低 {best[1]:.1%}，自动处理率 {best[2]:.1%}）")
    print("说明：漏转人工（投诉/纠纷被自动处理）风险远高于误转人工，")
    print("      若想更保守（宁可多转人工）可选更高阈值；想省人力可选更低阈值。")
    return best


if __name__ == "__main__":
    setup_logging()
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    tickets = gen_tickets.generate(count, seed=42)
    sweep(tickets)
