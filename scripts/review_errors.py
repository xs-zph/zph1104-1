"""异常工单复盘脚本：python scripts/review_errors.py

找出分类错误的工单（预测分类 != 真实标签），调用大模型复盘，
输出故障定位 + 知识库缺失 + 优化方案，用于持续迭代优化系统。
"""
import sys
from pathlib import Path

# 把项目根目录加入模块搜索路径，方便直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, llm
from app.config import setup_logging
from prompts import review


if __name__ == "__main__":
    setup_logging()
    tickets = db.list_all_tickets()

    # 只挑「有真实标签且分类错误」的工单
    errors = [t for t in tickets
              if t.get("ground_truth") and t["category"] != t["ground_truth"]]

    if not errors:
        print("没有分类错误的工单，系统表现良好。")
        sys.exit(0)

    print(f"发现 {len(errors)} 条分类错误工单，逐条复盘：\n")
    for i, t in enumerate(errors[:5], start=1):  # 最多复盘 5 条，节省 token
        print("=" * 60)
        print(f"【第 {i} 条】工单：{t['ticket_text']}")
        report = llm.complete(
            system=review.SYSTEM_PROMPT,
            user=review.build_user_prompt(
                t["ticket_text"], t["category"], t.get("reply", "")
            ),
            max_tokens=1024,
        )
        print(report)
