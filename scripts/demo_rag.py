"""RAG 全链路演示脚本：python scripts/demo_rag.py

完整展示「文档切块 → 向量化 → Qdrant（Chroma 降级）→ 相似度检索」这条链路，
用中文语义模型把「字面不同但语义相同」的问题映射到相近向量。

运行时会在控制台打印：
  1. 当前激活的向量化模型（语义模型 or 哈希兑底）
  2. 每篇文档被切成几个块
  3. 用「换种说法」的问题做相似度检索，看命中的块和距离
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import rag
from app.config import Config, setup_logging


def show_model():
    """打印当前激活的向量化模型。"""
    fn = rag._get_embedding_fn()
    if isinstance(fn, rag.SentenceEmbeddingFunction):
        print(f"[模型] 中文语义向量模型：{fn._model_name}")
    else:
        print("[模型] 本地哈希向量化（语义模型未启用，仅字面匹配）")


def show_chunks():
    """展示每篇文档的切块结果。"""
    for fp in sorted(Config.DOCS_DIR.glob("*.md")) + sorted(Config.DOCS_DIR.glob("*.txt")):
        text = fp.read_text(encoding="utf-8")
        chunks = rag.chunk_text(text)
        print(f"[切块] {fp.name}：全文 {len(text)} 字 → {len(chunks)} 块")
        for i, c in enumerate(chunks[:2]):
            print(f"     块{i}（{len(c)} 字）：{c[:40]}...")


def demo_retrieve():
    """用「换种说法」的问题做相似度检索，验证语义命中。"""
    queries = [
        "我买的这个东西不想要了能不能退掉",
        "手机坏了你们管修吗",
        "退款啥时候能到我卡里",
    ]
    print(f"\n[向量化] 正在把 {Config.DOCS_DIR} 下的文档切块并写入 Qdrant ...")
    total = rag.ingest_documents(reset=True)
    print(f"[向量化] 完成，共写入 {total} 个文本块到集合 docs")

    print("\n[相似度检索]")
    for q in queries:
        print(f"  用户问：{q}")
        hits = rag.retrieve_docs(q, top_k=2)
        for h in hits:
            print(f"    ↳ 命中《{h['source']}》块{h['chunk_index']}（距离 {h['distance']:.3f}）")
            print(f"      内容：{h['text'][:60]}...")
        print()


if __name__ == "__main__":
    setup_logging()
    Config.ensure_dirs()
    show_model()
    print()
    show_chunks()
    demo_retrieve()
    print("[完成] RAG 文档切块 + 向量化 + 相似度检索 全链路演示结束。")
