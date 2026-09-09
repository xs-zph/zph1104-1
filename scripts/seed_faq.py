"""知识库初始化脚本：python scripts/seed_faq.py

把 data/faq.md 里的问答写入 Qdrant（不可用时自动写入 Chroma 降级库）。
首次运行会自动下载向量化模型 all-MiniLM-L6-v2，需要联网。
"""
import sys
from pathlib import Path

# 把项目根目录加入模块搜索路径，方便直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, rag
from app.config import setup_logging


if __name__ == "__main__":
    setup_logging()
    db.init_db()          # 确保 faq_entries 等表已创建
    count = rag.ingest()
    print(f"[完成] 知识库已写入 {count} 条 FAQ，可用于 RAG 检索。")
