"""工单批量统计分析脚本：python scripts/analyze.py

读取数据库里的全部工单，调用大模型生成量化统计报告
（各类别占比、自动处理率、高频诉求等）。
"""
import sys
from pathlib import Path

# 把项目根目录加入模块搜索路径，方便直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, llm
from app.config import setup_logging
from prompts import analysis


if __name__ == "__main__":
    setup_logging()
    tickets = db.list_all_tickets()
    if not tickets:
        print("暂无工单数据，请先运行 python scripts/run_demo.py 生成工单。")
        sys.exit(0)

    # 拼出精简的工单列表文本
    lines = [f"{i + 1}. [{t['category']}] {t['ticket_text']}" for i, t in enumerate(tickets)]
    report = llm.complete(
        system=analysis.SYSTEM_PROMPT,
        user=analysis.build_user_prompt("\n".join(lines)),
        max_tokens=1024,
    )
    print(report)
