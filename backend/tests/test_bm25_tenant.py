"""W28 Day4 堵 C5：BM25 路租户隔离测试。

覆盖：
- BM25Index 构建时解析 chunk 租户维度；search(tenant_id) 先按租户过滤再打分
- 双租户语料零交集（同 query 各查各的，互不可见）
- 未知租户 fail-safe 返回空（宁可不返回不跨租户泄露）
- 缓存加载后租户维度保留（save/load 不丢）
- HybridRetriever.retrieve 把 tenant_id 透传给 store.query + bm25.search（双路）

CI 可跑（纯逻辑 + jieba，无 Qdrant/模型）。
"""

import pytest

from app.shared.rag.hybrid_retriever import BM25Index


def _chunks():
    """租户隔离语料：a/b 两租户专属 + 一段公共语料（无 tenant_id）。"""
    return [
        {
            "chunk_id": "a-0",
            "doc_id": "SCM-PUR-101",
            "text": "采购申请需要经过三级审批流程",
            "tenant_id": "tenant_a",
        },
        {
            "chunk_id": "a-1",
            "doc_id": "SCM-PUR-102",
            "text": "采购金额超过一百万必须招标",
            "tenant_id": "tenant_a",
        },
        {
            "chunk_id": "b-0",
            "doc_id": "SCM-INV-201",
            "text": "库存每周盘点一次",
            "tenant_id": "tenant_b",
        },
        {
            "chunk_id": "b-1",
            "doc_id": "SCM-INV-202",
            "text": "库存差异超过百分之五需要上报",
            "tenant_id": "tenant_b",
        },
        {"chunk_id": "pub-0", "doc_id": "SCM-ORG-301", "text": "质量管理不合格品退回流程"},
    ]


def _index(tmp_path, chunks=None):
    idx = BM25Index(
        chunks if chunks is not None else _chunks(), cache_file=tmp_path / "bm25_test.json"
    )
    idx.build()
    return idx


# ==================== 构建时租户维度 ====================


def test_build_indexes_tenant_dimension(tmp_path):
    idx = _index(tmp_path)
    assert idx.chunk_tenant["a-0"] == "tenant_a"
    assert idx.chunk_tenant["b-1"] == "tenant_b"
    assert idx.chunk_tenant["pub-0"] is None  # 无 tenant_id = 公共语料


# ==================== 检索过滤 ====================


def test_search_tenant_filters(tmp_path):
    idx = _index(tmp_path)
    hits = idx.search("采购申请审批", top_k=10, tenant_id="tenant_a")
    cids = {h["chunk_id"] for h in hits}
    assert cids == {"a-0", "a-1"}, f"tenant_a 只应命中自己的语料，实际 {cids}"


def test_search_tenant_b_isolated(tmp_path):
    idx = _index(tmp_path)
    hits = idx.search("库存盘点", top_k=10, tenant_id="tenant_b")
    cids = {h["chunk_id"] for h in hits}
    assert cids == {"b-0", "b-1"}, f"tenant_b 只应命中自己的语料，实际 {cids}"


def test_search_no_tenant_returns_public_full(tmp_path):
    """tenant_id=None（公共检索）→ 全量语料，行为与分片前一致（向后兼容）。"""
    idx = _index(tmp_path)
    hits = idx.search("不合格品退回", top_k=10, tenant_id=None)
    cids = {h["chunk_id"] for h in hits}
    assert "pub-0" in cids


# ==================== 双租户零交集（手册验收） ====================


def test_two_tenants_zero_overlap(tmp_path):
    """同 query 在两个租户语料内检索 → 结果零交集（BM25 路跨租户泄露已堵）。

    注意：rank_bm25 对不匹配 query 的文档返回 score=0（0 分也会进 top-k），
    所以"零交集"才是隔离判据——tenant_b 的候选集永远不含 tenant_a 的 chunk。"""
    idx = _index(tmp_path)
    q = "采购"  # 只出现在 tenant_a 语料
    hits_a = {h["chunk_id"] for h in idx.search(q, top_k=10, tenant_id="tenant_a")}
    hits_b = {h["chunk_id"] for h in idx.search(q, top_k=10, tenant_id="tenant_b")}
    assert not (hits_a & hits_b), f"BM25 双租户必须零交集，实际 a={hits_a} b={hits_b}"
    assert hits_a == {"a-0", "a-1"}
    # tenant_b 的候选集 = 自己的语料（即使 score=0 也在候选内，但绝不包含 a 的 chunk）
    assert hits_b == {"b-0", "b-1"}


def test_search_unknown_tenant_fail_safe(tmp_path):
    """未知租户（无语料）→ 返回空，不退回公共语料（fail-safe：宁缺毋滥）。"""
    idx = _index(tmp_path)
    assert idx.search("采购", top_k=10, tenant_id="ghost_tenant") == []


# ==================== 缓存加载保留租户维度 ====================


def test_load_preserves_tenant_dimension(tmp_path):
    idx = _index(tmp_path)
    idx.save()
    idx2 = BM25Index(cache_file=tmp_path / "bm25_test.json")
    assert idx2.load() is True
    hits = idx2.search("采购申请", top_k=10, tenant_id="tenant_a")
    assert {h["chunk_id"] for h in hits} == {"a-0", "a-1"}


# ==================== HybridRetriever 透传（双路） ====================


class _FakeStore:
    def __init__(self):
        self.collection = "scm_kb_v1"  # 默认 collection（分片前行为）
        self.calls = []

    def query(self, qv, top_k=5, topic=None, tenant_id=None, retries=None, collection=None, **kw):
        self.calls.append({"tenant_id": tenant_id, "collection": collection, "retries": retries})
        return [
            {
                "chunk_id": f"vec-{i}",
                "doc_id": f"SCM-ORG-00{i}",
                "text": f"t{i}",
                "score": 0.9 - i * 0.1,
            }
            for i in range(min(top_k, 2))
        ]


class _FakeBM25:
    def __init__(self):
        self.calls = []

    def search(self, query, top_k=10, tenant_id=None):
        self.calls.append({"tenant_id": tenant_id})
        return [{"chunk_id": "bm-1", "score": 5.0}]


class _FakeEmbedder:
    def embed_query(self, query):
        import numpy as np

        return np.array([0.1] * 8)


@pytest.fixture
def hybrid():
    from app.shared.rag.hybrid_retriever import HybridRetriever

    r = HybridRetriever.__new__(HybridRetriever)
    r.embedder = _FakeEmbedder()
    r.reranker = None
    r.fusion = "rrf"
    r.alpha = 0.5
    r.top_candidates = 20
    r.store = _FakeStore()
    r.bm25 = _FakeBM25()
    r.chunk_meta = {
        "bm-1": {
            "chunk_id": "bm-1",
            "doc_id": "SCM-PUR-001",
            "section_path": "s",
            "text": "采购条款一",
        },
        "vec-0": {
            "chunk_id": "vec-0",
            "doc_id": "SCM-ORG-001",
            "section_path": "s",
            "text": "向量条款零",
        },
        "vec-1": {
            "chunk_id": "vec-1",
            "doc_id": "SCM-ORG-002",
            "section_path": "s",
            "text": "向量条款一",
        },
    }
    return r


def test_hybrid_retrieve_passes_tenant_to_both_paths(hybrid, monkeypatch):
    """retrieve(tenant_id=...) 必须同时传给向量路（collection 路由 + payload）与 BM25 路。"""
    monkeypatch.setattr(
        "app.shared.rag.hybrid_retriever.sharding.collection_for", lambda t, **kw: "scm_kb_v1_3"
    )
    hybrid.retrieve("采购", top_k=3, tenant_id="tenant_a")
    assert hybrid.store.calls[0]["tenant_id"] == "tenant_a"
    assert hybrid.store.calls[0]["collection"] == "scm_kb_v1_3"  # 分片路由
    assert hybrid.bm25.calls[0]["tenant_id"] == "tenant_a"


def test_hybrid_retrieve_without_tenant_keeps_default(hybrid):
    """tenant_id 为空（公共检索）→ 用 store 自身 collection + BM25 全量（向后兼容）。"""
    hybrid.retrieve("采购", top_k=3)
    assert hybrid.store.calls[0]["tenant_id"] is None
    assert hybrid.store.calls[0]["collection"] == hybrid.store.collection  # 默认 collection
    assert hybrid.bm25.calls[0]["tenant_id"] is None
