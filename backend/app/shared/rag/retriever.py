"""检索器：Embedder + SCMStore 组合，供评测/问答/demo 统一使用。

接口：
    retriever.retrieve(query, top_k=5, topic=None) -> list[dict]
        [{chunk_id, doc_id, section_path, topic, text, score}]
    retriever.retrieve_top_docs(query, top_k=5) -> list[str]   # 去重后的 doc_id 列表（评测用）

Day5 的检索问答 demo 与 Day6 评测脚本都通过本类访问 Qdrant。
"""

from app.shared import config
from app.shared.rag import sharding
from app.shared.rag.embedder import Embedder
from app.shared.rag.store import SCMStore


class Retriever:
    def __init__(self, collection: str | None = None):
        self.embedder = Embedder()
        self.store = SCMStore(collection=collection or config.SCM_COLLECTION)

    def retrieve(
        self, query: str, top_k: int = 5, topic: str | None = None, tenant_id: str | None = None
    ) -> list[dict]:
        """★ W28 Day4（分片 C4）：tenant_id 非空 → 路由到租户分片 collection +
        payload filter（双保险）；为空 = 公共语料检索（行为与分片前一致）。"""
        qv = self.embedder.embed_query(query)
        coll = sharding.collection_for(tenant_id) if tenant_id else self.store.collection
        return self.store.query(
            qv.tolist(), top_k=top_k, topic=topic, tenant_id=tenant_id, collection=coll
        )

    def retrieve_top_docs(self, query: str, top_k: int = 5) -> list[str]:
        """Top-K 结果去重后的 doc_id 列表（保持召回顺序）。评测 Hit@1/Recall@5 用。"""
        hits = self.retrieve(query, top_k=top_k)
        seen, docs = set(), []
        for h in hits:
            if h["doc_id"] not in seen:
                seen.add(h["doc_id"])
                docs.append(h["doc_id"])
        return docs
