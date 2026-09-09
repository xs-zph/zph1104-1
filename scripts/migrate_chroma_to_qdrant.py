"""把现有 Chroma FAQ / 文档集合幂等迁移到 Qdrant。

用法：python scripts/migrate_chroma_to_qdrant.py
要求 QDRANT_URL 指向可写的 Qdrant 实例；原 Chroma 数据不会被删除。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb

from app import rag
from app.config import Config, setup_logging
from app.vector_store import QdrantStore


def migrate_collection(name: str) -> int:
    client = chromadb.PersistentClient(path=str(Config.CHROMA_DIR))
    source = client.get_collection(name=name)
    data = source.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    if not ids:
        return 0
    target = QdrantStore(
        name,
        rag._get_embedding_fn(),
        Config.QDRANT_URL,
        Config.QDRANT_API_KEY,
        Config.QDRANT_TIMEOUT_SECONDS,
    )
    target.upsert(ids=ids, documents=data.get("documents", []), metadatas=data.get("metadatas", []))
    return len(ids)


if __name__ == "__main__":
    setup_logging()
    Config.ensure_dirs()
    total = sum(migrate_collection(name) for name in ("faq", "docs"))
    print(f"[完成] 已迁移 {total} 个向量到 {Config.QDRANT_URL}")
