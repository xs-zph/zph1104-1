"""RAG 知识库模块：用 Qdrant（Chroma 降级）存储 FAQ 文档并做语义检索。

流程：
  1. ingest()   把 data/faq.md 里的问答对切成一条条，向量化后存入 ChromaDB；
  2. retrieve() 把用户问题也向量化，检索出语义最接近的几条 FAQ。

关于向量化模型（这是「语义检索」的核心）：
  默认使用中文语义模型 BAAI/bge-small-zh-v1.5（sentence-transformers），
  能把「字面不同但语义相同」的问题映射到相近的向量——例如「手机怎么还没到」
  和「物流查询」、「这个能退吗」和「退货政策」都能互相检索到（同义改写命中）。

  如果模型加载失败（未安装 sentence-transformers / 网络不通），自动回退到
  本地「字符 n-gram + 哈希」向量化（HashEmbeddingFunction）保证系统可用，
  但此时只对字面相近的查询有效。
"""
import hashlib
import logging
import os
import re
import threading
import uuid
from datetime import datetime

import chromadb
import numpy as np
from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from chromadb.utils import embedding_functions

from app import config, db, llm
from app.vector_store import create_store

logger = logging.getLogger("app.rag")

# 国内下载 huggingface 模型走镜像，避免连不上 huggingface.co
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


class HashEmbeddingFunction(EmbeddingFunction):
    """轻量本地向量化：字符 n-gram + 哈希，生成固定长度向量。

    原理：把文本切成长度为 n 的连续字符片段（n-gram），
    每个片段哈希到向量某个位置并累加，最后做 L2 归一化。
    相似文本会有相似的字符片段，向量也更接近。
    """

    def __init__(self, dim: int = 384, n: int = 2):
        self.dim = dim
        self.n = n

    def _embed(self, text: str) -> np.ndarray:
        text = text.lower()
        grams = [text[i:i + self.n] for i in range(max(len(text) - self.n + 1, 1))]
        vec = np.zeros(self.dim, dtype=np.float32)
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16) % self.dim
            vec[h] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    def __call__(self, input: Documents) -> Embeddings:
        return [self._embed(t) for t in input]


class SentenceEmbeddingFunction(EmbeddingFunction):
    """真正的语义向量化：基于中文 embedding 模型。

    用户问「手机怎么还没到」和库里存的「物流查询」虽然字面不同，
    但语义接近，会被映射到相近的向量，从而被检索出来（同义改写也能命中）。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        from sentence_transformers import SentenceTransformer
        self._model_name = model_name
        # 优先本地缓存加载（local_files_only=True 跳过 hf-mirror 逐文件 HEAD 校验，
        # 重启/首次请求能从 ~90 秒降到几秒）；缓存不存在时再回退联网下载。
        try:
            self._model = SentenceTransformer(model_name, local_files_only=True)
            logger.info("已从本地缓存加载向量模型：%s", model_name)
        except Exception:
            logger.info("本地缓存未命中，改为联网加载向量模型：%s", model_name)
            self._model = SentenceTransformer(model_name)

    def __call__(self, input: Documents) -> Embeddings:
        vectors = self._model.encode(
            list(input), normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vectors]


_embedding_fn = None
_embedding_lock = threading.Lock()
_faq_collection = None
_docs_collection = None
_collection_lock = threading.Lock()


def _get_embedding_fn():
    """获取（或创建）向量化函数。

    优先用中文语义模型（支持同义改写检索）；不可用时回退本地哈希。
    用双重检查锁防止冷启动时（模型加载约几十秒）多个线程并发重复加载。
    """
    global _embedding_fn
    if _embedding_fn is None:
        with _embedding_lock:
            if _embedding_fn is None:
                _embedding_fn = _create_embedding_fn()
    return _embedding_fn


def warmup() -> None:
    """预加载向量化模型（供启动时后台调用），避免首个检索请求等待模型冷加载。"""
    try:
        _get_embedding_fn()
        logger.info("RAG 向量化模型预热完成")
    except Exception as e:  # noqa: BLE001
        logger.warning("RAG 预热失败：%s", e)


def _create_embedding_fn():
    """优先用中文语义向量模型；失败则回退本地哈希（离线兜底，保证可用）。"""
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    try:
        fn = SentenceEmbeddingFunction(model_name)
        fn(["预热"])  # 触发真实加载，加载失败会抛异常
        logger.info("已启用中文语义向量模型：%s", model_name)
        return fn
    except Exception as e:  # noqa: BLE001
        logger.warning("语义模型不可用，回退本地哈希向量化：%s", e)
        return HashEmbeddingFunction()


def get_collection():
    """获取 FAQ 向量集合；Qdrant 不可用时回退到 Chroma。"""
    global _faq_collection
    if _faq_collection is not None:
        return _faq_collection
    config.Config.ensure_dirs()
    with _collection_lock:
        if _faq_collection is None:
            _faq_collection = create_store(
                "faq", _get_embedding_fn(), config.Config, config.Config.CHROMA_DIR
            )
    return _faq_collection


def parse_faq(faq_path) -> list[dict]:
    """解析 data/faq.md 文件，返回问答对列表。

    文件格式约定：
        ## Q: 问题内容
        A: 答案内容

    逐行解析（只认行首的 ## Q: / A:），标题(#)、说明(>)、空行等其它行自动忽略，
    避免头部说明里字面出现的「## Q: / A:」被误当成一条 FAQ。
    """
    pairs = []
    current_q = None
    for raw in faq_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## Q:"):
            current_q = line[len("## Q:"):].strip()
        elif line.startswith("A:") and current_q is not None:
            pairs.append({"question": current_q, "answer": line[len("A:"):].strip()})
            current_q = None
    logger.info("解析 FAQ 完成，共 %d 条问答", len(pairs))
    return pairs


def sync_faq_to_chroma() -> int:
    """把 MySQL 里「启用中」的知识库条目同步到 ChromaDB（增删改后调用，热更新）。

    ChromaDB 的 id 用 "faq-{db主键}"，这样编辑 / 软删都能精确定位，
    不需要重启服务即可生效。软删除的条目会从向量库里移除。
    """
    entries = db.list_faq_entries(enabled_only=True)
    collection = get_collection()

    if entries:
        collection.upsert(
            ids=[f"faq-{e['id']}" for e in entries],
            documents=[e["question"] for e in entries],
            metadatas=[{"answer": e["answer"], "faq_id": e["id"]} for e in entries],
        )

    # 删除向量库里「已不在启用列表」的旧条目（含软删 / 被删除的）
    enabled_ids = {f"faq-{e['id']}" for e in entries}
    existing = collection.get()
    stale = [i for i in existing.get("ids", []) if i not in enabled_ids]
    if stale:
        collection.delete(ids=stale)

    logger.info("知识库已同步到向量库：启用 %d 条，清理 %d 条", len(entries), len(stale))
    return len(entries)


def ensure_faq_seeded() -> None:
    """启动时确保知识库已从 faq.md 导入 MySQL（首次运行 / 老库迁移时补种）。

    幂等：表里已有数据就直接同步（保证 ChromaDB 与 MySQL 一致）。
    """
    if db.count_faq_entries() == 0:
        logger.info("知识库表为空，从 faq.md 导入种子数据")
        ingest(reset=False)
    else:
        sync_faq_to_chroma()


def ingest(faq_path=None, reset: bool = True) -> int:
    """把 data/faq.md 的问答导入 MySQL 知识库，并同步到向量库。

    参数：
      reset=True 时先清空 MySQL 里的知识库条目再导入（保证可重复执行）。
    """
    faq_path = faq_path or config.Config.FAQ_PATH
    pairs = parse_faq(faq_path)

    if reset:
        db.clear_faq_entries()
    for p in pairs:
        db.insert_faq_entry(p["question"], p["answer"])

    count = sync_faq_to_chroma()
    logger.info("知识库导入完成，启用 %d 条", count)
    return count


def retrieve(query: str, top_k: int = None) -> list[dict]:
    """检索与问题最相关的 FAQ 条目。

    返回：[{"question": str, "answer": str, "distance": float}, ...]
    """
    top_k = top_k or config.Config.RAG_TOP_K
    collection = get_collection()
    result = collection.query(query_texts=[query], n_results=top_k)

    chunks = []
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]

    for doc, meta, dist in zip(docs, metas, distances):
        chunks.append({
            "question": doc,
            "answer": (meta or {}).get("answer", ""),
            "distance": dist,
        })
    return chunks


def best_answer(question: str, top_k: int = None) -> dict | None:
    """RAG 优先检索：知识库 top-1 命中（距离达标）时返回其问答，否则返回 None。

    这是「先查 RAG、查不到再上大模型、实在不行转人工」三级链路的第一级：
      - 命中：直接返回知识库答案（不调用大模型，毫秒级）；
      - 未命中：返回 None，由调用方降级到 LLM / Agent。
    """
    hits = rerank(question, retrieve(question, top_k=top_k), top_n=3)
    if not hits:
        return None
    best = hits[0]
    distance = best.get("distance")
    if distance is None or distance > config.Config.RAG_MAX_DISTANCE:
        return None
    if not (best.get("answer") or "").strip():
        return None
    return best


def list_entries(include_disabled: bool = False) -> list[dict]:
    """返回知识库条目（默认只返回启用中；include_disabled=True 含软删除）。"""
    return db.list_faq_entries(enabled_only=not include_disabled)


def add_entry(question: str, answer: str, operator: str = "system") -> dict:
    """实时新增一条知识：写入 MySQL + 同步向量库，无需重启即可被检索。"""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question or not answer:
        raise ValueError("问题和答案都不能为空")

    faq_id = db.insert_faq_entry(question, answer)
    entry = db.get_faq_entry(faq_id) or {
        "id": faq_id, "question": question, "answer": answer, "enabled": 1,
    }
    db.insert_faq_audit(faq_id, "created", question, answer, True, operator)
    sync_faq_to_chroma()
    logger.info("实时新增知识成功：#%d %s", faq_id, question)
    return entry


def update_entry(faq_id: int, question: str | None = None,
                 answer: str | None = None, operator: str = "system") -> dict:
    """编辑一条知识库条目（问题/答案可部分更新），并同步向量库。"""
    entry = db.update_faq_entry(faq_id, question=question, answer=answer)
    if entry is None:
        raise ValueError(f"知识条目 #{faq_id} 不存在")
    db.insert_faq_audit(
        faq_id, "updated", entry["question"], entry["answer"], bool(entry.get("enabled", 1)), operator,
    )
    sync_faq_to_chroma()
    logger.info("知识条目 #%d 已更新", faq_id)
    return entry


def disable_entry(faq_id: int, operator: str = "system") -> dict:
    """软删除一条知识库条目（enabled 置 0，从向量库移除，但保留数据可恢复）。"""
    entry = db.set_faq_enabled(faq_id, False)
    if entry is None:
        raise ValueError(f"知识条目 #{faq_id} 不存在")
    db.insert_faq_audit(
        faq_id, "disabled", entry["question"], entry["answer"], False, operator,
    )
    sync_faq_to_chroma()
    logger.info("知识条目 #%d 已软删除", faq_id)
    return entry


def enable_entry(faq_id: int, operator: str = "system") -> dict:
    """重新启用一条被软删除的知识库条目。"""
    entry = db.set_faq_enabled(faq_id, True)
    if entry is None:
        raise ValueError(f"知识条目 #{faq_id} 不存在")
    db.insert_faq_audit(
        faq_id, "restored", entry["question"], entry["answer"], True, operator,
    )
    sync_faq_to_chroma()
    logger.info("知识条目 #%d 已重新启用", faq_id)
    return entry


# ---------------------------------------------------------------------------
# 向量召回 Top5 + LLM 重排 Top3
# ---------------------------------------------------------------------------

_reranker = None
_reranker_lock = threading.Lock()


def _get_reranker():
    """懒加载本地 CrossEncoder；模型不可用时由调用方按向量距离降级。"""
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                from sentence_transformers import CrossEncoder

                model_name = config.Config.RERANKER_MODEL
                try:
                    _reranker = CrossEncoder(
                        model_name,
                        automodel_args={"local_files_only": True},
                    )
                except Exception:
                    if not config.Config.RERANKER_ALLOW_DOWNLOAD:
                        raise
                    _reranker = CrossEncoder(model_name)
                logger.info("已启用本地 reranker：%s", model_name)
    return _reranker


def _chunk_pair(query: str, chunk: dict) -> list[str]:
    question = chunk.get("question") or chunk.get("text") or ""
    answer = chunk.get("answer") or ""
    return [query, f"{question}\n{answer}".strip()]


def rerank(query: str, chunks: list[dict], top_n: int = 3) -> list[dict]:
    """对向量召回候选做 CrossEncoder 精排，失败时按距离排序。"""
    if not chunks:
        return []
    if len(chunks) <= 1 or not config.Config.RERANKER_ENABLED:
        return chunks[:top_n]
    try:
        model = _get_reranker()
        scores = model.predict([_chunk_pair(query, chunk) for chunk in chunks])
        ranked = sorted(
            zip(chunks, scores), key=lambda item: float(item[1]), reverse=True
        )[:top_n]
        return [{**chunk, "rerank_score": float(score)} for chunk, score in ranked]
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地 reranker 不可用，降级为向量距离排序：%s", exc)
        return sorted(chunks, key=lambda c: c.get("distance") if c.get("distance") is not None else 999.0)[:top_n]


def rerank_documents(query: str, chunks: list[dict], top_n: int = 3) -> list[dict]:
    """文档块使用同一 CrossEncoder 精排入口，保留独立名称方便调用方表达数据类型。"""
    return rerank(query, chunks, top_n=top_n)


# ---------------------------------------------------------------------------
# 文档切块 + 向量化（RAG 的另一种数据源：产品手册 / 政策文档）
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """把一篇长文档切成带重叠的文本块（chunk）。

    切块策略：
      1. 先按「空行 / 换行」拆成段落，尽量不在句子中间生硬切断；
      2. 相邻段落贪心合并，接近 chunk_size 字就收成一块；
      3. 相邻块之间保留 overlap 字的重叠，避免关键信息被切散到两个块里。

    这是「文档切块 → 向量化 → 相似度检索」链路的第一步。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 先按空行/换行拆成段落，去掉空白
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]

    chunks = []
    current = ""
    for para in paragraphs:
        # 单段就超过 chunk_size：按固定窗口硬切（保留 overlap）
        if len(para) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            step = chunk_size - overlap
            for i in range(0, len(para), step if step > 0 else chunk_size):
                chunks.append(para[i:i + chunk_size])
            continue

        # 当前块再塞这段会不会超？超了就收块，并保留上一段尾部做重叠衔接
        if current and len(current) + len(para) + 1 > chunk_size:
            chunks.append(current)
            current = current[-overlap:] if overlap > 0 else ""

        current = (current + "\n" + para).strip() if current else para

    if current:
        chunks.append(current)
    return chunks


def get_docs_collection():
    """获取文档向量集合；Qdrant 不可用时回退到 Chroma。"""
    global _docs_collection
    if _docs_collection is not None:
        return _docs_collection
    config.Config.ensure_dirs()
    with _collection_lock:
        if _docs_collection is None:
            _docs_collection = create_store(
                "docs", _get_embedding_fn(), config.Config, config.Config.CHROMA_DIR
            )
    return _docs_collection


def ingest_document_text(text: str, source_name: str) -> dict:
    """将一份已提取的文档按来源替换写入文档向量集合。"""
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("文档没有可索引的文本内容")
    source_name = (source_name or "未命名文档").strip()[:255]
    collection = get_docs_collection()
    try:
        old = collection.get(where={"source": source_name})
        old_ids = old.get("ids", [])
        if old_ids:
            collection.delete(ids=old_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理旧文档块失败，将继续写入新版本：%s", exc)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ids = [f"upload-{digest}-{index}" for index in range(len(chunks))]
    metadatas = [
        {"source": source_name, "chunk_index": index, "imported_at": imported_at}
        for index in range(len(chunks))
    ]
    collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
    logger.info("上传文档已索引：%s，共 %d 个文本块", source_name, len(chunks))
    return {"source": source_name, "chunks": len(chunks), "characters": len(text)}


def list_document_sources() -> list[dict]:
    """列出已索引文档来源和文本块数量，不返回文档正文。"""
    collection = get_docs_collection()
    if collection.count() == 0:
        return []
    result = collection.get(include=["metadatas"])
    grouped = {}
    for meta in result.get("metadatas", []) or []:
        meta = meta or {}
        source = meta.get("source") or "未命名文档"
        item = grouped.setdefault(source, {"source": source, "chunks": 0, "imported_at": ""})
        item["chunks"] += 1
        item["imported_at"] = max(item["imported_at"], str(meta.get("imported_at") or ""))
    return sorted(grouped.values(), key=lambda item: item["source"])


def ingest_documents(docs_dir=None, reset: bool = True) -> int:
    """把 data/docs/ 里的产品手册/政策文档切块、向量化后写入向量库。

    与 faq.md 的「问答对」不同，这里直接读整篇文档，切块后每一块
    作为一个独立检索单元；返回本次向量化的块总数。
    """
    docs_dir = docs_dir or config.Config.DOCS_DIR
    if not docs_dir.exists():
        logger.warning("文档目录不存在：%s", docs_dir)
        return 0

    files = sorted(list(docs_dir.glob("*.md")) + list(docs_dir.glob("*.txt")))
    if not files:
        logger.warning("文档目录里没有 .md/.txt 文件：%s", docs_dir)
        return 0

    if reset:
        collection = get_docs_collection()
        existing = collection.get().get("ids", [])
        if existing:
            collection.delete(ids=existing)
    else:
        collection = get_docs_collection()

    total = 0
    for fp in files:
        text = fp.read_text(encoding="utf-8")
        chunks = chunk_text(text)
        if not chunks:
            continue
        ids = [f"{fp.stem}-{i}" for i in range(len(chunks))]
        metadatas = [{"source": fp.name, "chunk_index": i} for i in range(len(chunks))]
        collection.add(ids=ids, documents=chunks, metadatas=metadatas)
        total += len(chunks)
        logger.info("文档 %s 已切 %d 块并向量化", fp.name, len(chunks))

    logger.info("文档向量化完成，共 %d 个文本块", total)
    return total


def retrieve_docs(query: str, top_k: int = None) -> list[dict]:
    """在「文档块」向量库里做相似度检索（与 faq 问答对检索并列）。

    返回：[{"text", "source", "chunk_index", "distance"}, ...]
    距离越小越相关。
    """
    top_k = top_k or config.Config.RAG_TOP_K
    collection = get_docs_collection()
    if collection.count() == 0:
        return []
    result = collection.query(query_texts=[query], n_results=top_k)

    chunks = []
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, distances):
        chunks.append({
            "text": doc,
            "source": (meta or {}).get("source", ""),
            "chunk_index": (meta or {}).get("chunk_index", 0),
            "distance": dist,
        })
    return chunks
