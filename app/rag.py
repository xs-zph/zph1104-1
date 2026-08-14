"""RAG 知识库模块：用 ChromaDB 存储 FAQ 文档并做语义检索。

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

import chromadb
import numpy as np
from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from chromadb.utils import embedding_functions

from app import config

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
    """获取（或创建）名为 faq 的向量集合。"""
    config.Config.ensure_dirs()
    client = chromadb.PersistentClient(path=str(config.Config.CHROMA_DIR))
    return client.get_or_create_collection(
        name="faq", embedding_function=_get_embedding_fn()
    )


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


def ingest(faq_path=None, reset: bool = True):
    """把 FAQ 文档写入向量库。

    参数：
      reset=True 时先清空旧数据再写入，保证可重复执行。
    """
    faq_path = faq_path or config.Config.FAQ_PATH
    pairs = parse_faq(faq_path)

    if reset:
        # 清空旧数据（ChromaDB 通过删除集合来重置）
        client = chromadb.PersistentClient(path=str(config.Config.CHROMA_DIR))
        try:
            client.delete_collection("faq")
        except Exception:
            pass
        collection = client.create_collection(
            name="faq", embedding_function=_get_embedding_fn()
        )
    else:
        collection = get_collection()

    ids = [str(i) for i in range(len(pairs))]
    documents = [p["question"] for p in pairs]
    metadatas = [{"answer": p["answer"]} for p in pairs]

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("知识库写入完成，共 %d 条", len(pairs))
    return len(pairs)


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
    hits = retrieve(question, top_k=top_k)
    if not hits:
        return None
    best = hits[0]
    distance = best.get("distance")
    if distance is None or distance > config.Config.RAG_MAX_DISTANCE:
        return None
    if not (best.get("answer") or "").strip():
        return None
    return best


def list_entries() -> list[dict]:
    """返回知识库当前所有问答对（供后台展示）。"""
    return parse_faq(config.Config.FAQ_PATH)


def add_entry(question: str, answer: str) -> dict:
    """实时新增一条 FAQ：写入向量库 + 追加到 faq.md（持久化），无需重启。

    新增后立刻能被 retrieve() 检索到，实现知识库的「实时更新」。
    """
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question or not answer:
        raise ValueError("问题和答案都不能为空")

    collection = get_collection()
    faq_id = f"q{uuid.uuid4().hex}"
    collection.add(ids=[faq_id], documents=[question], metadatas=[{"answer": answer}])

    # 追加到 faq.md，保证下次重新 seed 也不会丢
    entry = f"\n## Q: {question}\nA: {answer}\n"
    with open(config.Config.FAQ_PATH, "a", encoding="utf-8") as f:
        f.write(entry)

    logger.info("实时新增 FAQ 成功：%s", question)
    return {"id": faq_id, "question": question, "answer": answer}


# ---------------------------------------------------------------------------
# 文档切块 + 向量化（RAG 的另一种数据源：产品手册 / 政策文档）
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 200, overlap: int = 40) -> list[str]:
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
    """获取（或创建）名为 docs 的向量集合，用于存放切块后的文档。"""
    config.Config.ensure_dirs()
    client = chromadb.PersistentClient(path=str(config.Config.CHROMA_DIR))
    return client.get_or_create_collection(
        name="docs", embedding_function=_get_embedding_fn()
    )


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

    client = chromadb.PersistentClient(path=str(config.Config.CHROMA_DIR))
    if reset:
        try:
            client.delete_collection("docs")
        except Exception:
            pass
        collection = client.create_collection(
            name="docs", embedding_function=_get_embedding_fn()
        )
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
