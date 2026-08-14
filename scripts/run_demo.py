"""端到端演示脚本：python scripts/run_demo.py

完整跑一遍「工单 → 分类 → 路由 → 回复 → 落库 → 统计」流程，
最后打印核心指标（分类准确率、自动处理率、平均耗时）。
"""
import sys
import time
from pathlib import Path

# 把项目根目录加入模块搜索路径，方便直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, metrics, router
from app.config import setup_logging
from scripts import gen_tickets


def run(count: int = 20, seed: int = 42):
    """跑 count 张模拟工单，返回指标。"""
    setup_logging()
    db.init_db()

    # 生成模拟工单（带真实标签，用于算分类准确率）
    tickets = gen_tickets.generate(count, seed=seed)
    print(f"开始处理 {count} 张模拟工单...\n")

    for i, t in enumerate(tickets, start=1):
        record = router.process_ticket(t["ticket_text"], ground_truth=t["ground_truth"])
        router.save_processed(record)
        # 简单进度提示
        print(
            f"[{i:>3}/{count}] 真实={t['ground_truth']:<4} | "
            f"分类={record['category']:<4} | 置信度={record['confidence']:.2f} | "
            f"处理={record['status']}"
        )

    print("\n" + "=" * 50)
    overview = metrics.overview()
    print("【核心指标】")
    print(f"  工单总数     : {overview.get('total')}")
    print(f"  自动处理     : {overview.get('auto_count')} 张")
    print(f"  人工升级     : {overview.get('escalated_count')} 张")
    print(f"  自动处理率   : {overview.get('auto_rate', 0) * 100:.1f}%")
    if overview.get("accuracy") is not None:
        print(f"  分类准确率   : {overview['accuracy'] * 100:.1f}%")
    print(f"  平均耗时     : {overview.get('avg_latency_ms')} ms")
    print(f"  类别分布     : {overview.get('category_distribution')}")

    print("\n【分类别准确率】")
    for cat, s in metrics.category_accuracy().items():
        print(f"  {cat:<4} : {s['correct']}/{s['total']} ({s['accuracy'] * 100:.0f}%)")

    return overview


if __name__ == "__main__":
    run(count=20)
