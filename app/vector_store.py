"""向量库适配层。

上层 RAG 仍使用 Chroma 兼容的 collection 方法，生产环境优先使用 Qdrant；
Qdrant 不可用时自动回退到本地 Chroma，避免知识库故障阻断客服主流程。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import chromadb

logger = logging.getLogger("app.vector_store")


def _point_id(collection: str, item_id: str) -> str:
    """把业务字符串 ID 映射成 Qdrant 支持的稳定 UUID。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-ticket:{collection}:{item_id}"))


class ChromaStore:
    """对现有 Chroma collection 的薄封装，便于统一注入和测试。"""

    def __init__(self, path, collection_name: str, embedding_function):
        self.client = chromadb.PersistentClient(path=str(path))
        self.collection = self.client.get_or_create_collection(
            name=collection_name, embedding_function=embedding_function
        )

    def __getattr__(self, name):
        return getattr(self.collection, name)


class QdrantStore:
    """Qdrant collection 的 Chroma 兼容实现。"""

    def __init__(self, collection_name: str, embedding_function, url: str, api_key: str = "", timeout: float = 2):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.collection_name = collection_name
        self.embedding_function = embedding_function
        self.client = QdrantClient(url=url, api_key=api_key or None, timeout=timeout)
        vector = embedding_function(["向量库初始化"])[0]
        collections = {item.name for item in self.client.get_collections().collections}
        if collection_name not in collections:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=len(vector), distance=Distance.COSINE),
            )
        logger.info("Qdrant collection ready: %s", collection_name)

    def count(self) -> int:
        return int(self.client.count(collection_name=self.collection_name, exact=True).count)

    def upsert(self, ids, documents, metadatas=None):
        from qdrant_client.models import PointStruct

        metadatas = metadatas or [{} for _ in documents]
        vectors = self.embedding_function(list(documents))
        points = []
        for item_id, document, metadata, vector in zip(ids, documents, metadatas, vectors):
            payload = {"_id": str(item_id), "document": document, **(metadata or {})}
            points.append(PointStruct(id=_point_id(self.collection_name, str(item_id)), vector=vector, payload=payload))
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points, wait=True)

    def add(self, ids, documents, metadatas=None):
        return self.upsert(ids, documents, metadatas)

    def delete(self, ids=None, where=None):
        from qdrant_client.models import Filter, FieldCondition, MatchValue, PointIdsList

        if ids:
            point_ids = [_point_id(self.collection_name, str(item)) for item in ids]
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=PointIdsList(points=point_ids),
                wait=True,
            )
            return
        if where:
            conditions = [
                FieldCondition(key=key, match=MatchValue(value=value))
                for key, value in where.items()
            ]
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=conditions),
                wait=True,
            )

    def get(self, ids=None, where=None, include=None):
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if where:
            query_filter = Filter(
                must=[
                    FieldCondition(key=key, match=MatchValue(value=value))
                    for key, value in where.items()
                ]
            )
        records = []
        offset = None
        while True:
            batch, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            records.extend(batch)
            if offset is None:
                break
        if ids is not None:
            wanted = {str(item) for item in ids}
            records = [item for item in records if str((item.payload or {}).get("_id")) in wanted]
        return self._records_to_dict(records)

    def _records_to_dict(self, records) -> dict[str, list[Any]]:
        ids, documents, metadatas = [], [], []
        for record in records:
            payload = dict(record.payload or {})
            ids.append(str(payload.pop("_id", record.id)))
            documents.append(payload.pop("document", ""))
            metadatas.append(payload)
        return {"ids": ids, "documents": documents, "metadatas": metadatas}

    def query(self, query_texts, n_results: int = 5):
        query_vector = self.embedding_function(list(query_texts))[0]
        # search() is available in current Qdrant clients and keeps compatibility with older releases.
        if hasattr(self.client, "search"):
            hits = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=n_results,
                with_payload=True,
            )
        else:
            hits = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=n_results,
                with_payload=True,
            ).points
        documents, metadatas, distances = [], [], []
        for hit in hits:
            payload = dict(hit.payload or {})
            documents.append(payload.pop("document", ""))
            payload.pop("_id", None)
            metadatas.append(payload)
            # Existing RAG thresholds use distance semantics: smaller is better.
            distances.append(1.0 - float(hit.score))
        return {"documents": [documents], "metadatas": [metadatas], "distances": [distances]}


class ResilientStore:
    """主库失败时按调用粒度回退，避免一次 Qdrant 故障拖垮客服请求。"""

    def __init__(self, primary, fallback, name: str):
        self.primary = primary
        self.fallback = fallback
        self.name = name

    def _call(self, method: str, *args, **kwargs):
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant 操作失败，集合 %s 回退 Chroma：%s", self.name, exc)
            return getattr(self.fallback, method)(*args, **kwargs)

    def _mirror(self, method: str, *args, **kwargs):
        """主库成功后同步降级库；降级库故障不影响主流程。"""
        try:
            return getattr(self.fallback, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chroma 降级库同步失败，集合 %s：%s", self.name, exc)
            return None

    def upsert(self, *args, **kwargs):
        try:
            result = self.primary.upsert(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant 操作失败，集合 %s 回退 Chroma：%s", self.name, exc)
            return self.fallback.upsert(*args, **kwargs)
        self._mirror("upsert", *args, **kwargs)
        return result

    def add(self, *args, **kwargs):
        # Qdrant 的 upsert 具备幂等性，统一使用它避免重复导入时失败。
        return self.upsert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        try:
            result = self.primary.delete(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant 操作失败，集合 %s 回退 Chroma：%s", self.name, exc)
            return self.fallback.delete(*args, **kwargs)
        self._mirror("delete", *args, **kwargs)
        return result

    def query(self, *args, **kwargs):
        result = self._call("query", *args, **kwargs)
        if result.get("documents", [[]])[0]:
            return result
        try:
            if self.fallback.count() > 0:
                return self.fallback.query(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chroma 回退查询失败：%s", exc)
        return result

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)


def create_store(collection_name: str, embedding_function, config, chroma_path):
    """创建带降级能力的 collection 适配器。"""
    fallback = ChromaStore(chroma_path, collection_name, embedding_function)
    if not config.QDRANT_ENABLED:
        return fallback
    try:
        primary = QdrantStore(
            collection_name,
            embedding_function,
            config.QDRANT_URL,
            config.QDRANT_API_KEY,
            config.QDRANT_TIMEOUT_SECONDS,
        )
        return ResilientStore(primary, fallback, collection_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant 不可用，集合 %s 使用 Chroma：%s", collection_name, exc)
        return fallback
