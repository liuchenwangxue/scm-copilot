"""W26 Day2 演练三修复的回归测试：Qdrant 挂 → HybridRetriever 降级 BM25-only。

覆盖：
1. store.query retries 参数：retries=0 快速失败（不无限重试拖垮响应）
2. HybridRetriever.retrieve 向量路异常 → BM25-only 降级（degraded 标记进结果）
3. 恢复后混合检索自动回（source 含 vec/both）

★ CI 可跑（不连真实 Qdrant/模型）：monkeypatch store.query 抛异常模拟故障。
"""
import pytest


class _FakeStore:
    """模拟 SCMStore：retries 透传 + 可选抛异常。"""

    def __init__(self, fail: bool = False, seen: list[int] | None = None):
        self.collection = "scm_kb_v1"  # W28-D4：retrieve 无 tenant 时读 store.collection
        self.fail = fail
        self.seen = seen if seen is not None else []
        self.calls: list[dict] = []

    def query(self, qv, top_k=5, topic=None, tenant_id=None, retries=None, **kw):
        self.calls.append({"retries": retries})
        if retries is not None:
            self.seen.append(retries)
        if self.fail:
            raise ConnectionError("Qdrant connection refused (chaos drill)")
        # 只返回 meta 中存在的 chunk（vec-0/vec-1），避免融合查 meta 时 KeyError
        out = []
        for i in range(min(top_k, 2)):
            out.append({"chunk_id": f"vec-{i}", "doc_id": f"SCM-ORG-00{i}",
                        "text": f"vec chunk {i}", "score": 0.9 - i * 0.1})
        return out


class _FakeBM25:
    """模拟 BM25Index：固定返回两条。"""

    def search(self, query, top_k=10, tenant_id=None):  # W28-D4：透传 tenant 过滤
        return [
            {"chunk_id": "bm-1", "score": 5.0},
            {"chunk_id": "bm-2", "score": 4.0},
        ]


@pytest.fixture
def hybrid():
    """构造降级版 HybridRetriever（不加载真模型/真 Qdrant）。"""
    from app.shared.rag.hybrid_retriever import HybridRetriever

    class _FakeEmbedder:
        def embed_query(self, query):
            import numpy as np

            return np.array([0.1] * 8)  # 带 .tolist() 的向量（与真模型接口一致）

    r = HybridRetriever.__new__(HybridRetriever)
    r.embedder = _FakeEmbedder()
    r.reranker = None
    r.fusion = "rrf"
    r.alpha = 0.5
    r.top_candidates = 20
    r.chunk_meta = {
        "bm-1": {"chunk_id": "bm-1", "doc_id": "SCM-PUR-001", "section_path": "s", "text": "采购条款一"},
        "bm-2": {"chunk_id": "bm-2", "doc_id": "SCM-PUR-002", "section_path": "s", "text": "采购条款二"},
        "vec-0": {"chunk_id": "vec-0", "doc_id": "SCM-ORG-001", "section_path": "s", "text": "向量条款零"},
        "vec-1": {"chunk_id": "vec-1", "doc_id": "SCM-ORG-002", "section_path": "s", "text": "向量条款一"},
    }
    return r


def test_store_query_retries_param():
    """store.query 支持 retries 参数（默认 3，可传 0 快速失败）。"""
    import inspect

    from app.shared.rag.store import SCMStore

    # 不实例化（会连 Qdrant），仅验证签名接受 retries——用构造参数检查
    sig = inspect.signature(SCMStore.query)
    assert "retries" in sig.parameters, "SCMStore.query 应支持 retries 参数"
    assert sig.parameters["retries"].default is None


def test_hybrid_degrades_bm25_only_on_vector_failure(hybrid):
    """Qdrant 挂（store.query 抛异常）→ BM25-only 降级 + degraded 标记。"""
    store = _FakeStore(fail=True)
    bm25 = _FakeBM25()
    hybrid.store = store
    hybrid.bm25 = bm25

    hits = hybrid.retrieve("采购申请需要经过哪几级审批", top_k=3)

    assert len(hits) == 2  # BM25 两条
    assert all(h["source"] == "bm25-degraded" for h in hits)
    assert all(h.get("degraded") is True for h in hits)
    # 快速失败：retries=0（不过度重试）
    assert store.calls and store.calls[0]["retries"] == 0


def test_hybrid_recovers_mixed_after_qdrant_back(hybrid):
    """Qdrant 恢复（store.query 正常）→ 混合检索自动回（含 vec/both）。"""
    store = _FakeStore(fail=False)
    bm25 = _FakeBM25()
    hybrid.store = store
    hybrid.bm25 = bm25

    # 恢复后走正常融合路径（RRF），结果应含 vec 来源
    hits = hybrid.retrieve("采购申请需要经过哪几级审批", top_k=5)
    sources = {h["source"] for h in hits}
    assert "vec" in sources, f"恢复后应有 vec 来源，实际 {sources}"
    assert not any(h.get("degraded") for h in hits), "恢复后不应带 degraded 标记"
