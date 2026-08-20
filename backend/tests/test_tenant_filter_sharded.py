"""W28 Day4 TenantFilter 改造测试：分片路由 + payload 双保险的调用方视角。

覆盖：
- retrieve() 把 tenant_id 强制注入检索器（接口对调用方透明，签名不变）
- verify_isolation 基础语义（payload 过滤级，W18 资产不破坏）
- verify_sharded_isolation 分片模式：路由层隔离 + 数据层隔离双路判定
- 分片 off（灰度）时路由层退化到 base collection（回退零成本）

CI 可跑（纯逻辑，mock retriever，无 Qdrant/模型）。
"""

import pytest

from app.domains.kb.tenant.tenant_filter import TenantFilter
from app.shared.rag import sharding


class _MockRetriever:
    """模拟隔离检索器：每个租户命中自己的专属 doc（租户专属语料模型）。"""

    def __init__(self):
        self.calls = []

    def retrieve(self, question, top_k=5, tenant_id=None, **kw):
        self.calls.append({"tenant_id": tenant_id, "top_k": top_k})
        if tenant_id is None:
            return [{"doc_id": f"pub-{i}"} for i in range(top_k)]
        return [{"doc_id": f"{tenant_id}-{i}"} for i in range(min(top_k, 3))]


@pytest.fixture
def sharding_on(monkeypatch):
    monkeypatch.setattr(sharding, "SHARDING_ENABLED", True)


@pytest.fixture
def sharding_off(monkeypatch):
    monkeypatch.setattr(sharding, "SHARDING_ENABLED", False)


# ==================== retrieve 透传 ====================


def test_retrieve_injects_tenant_id():
    tf = TenantFilter("tenant_a")
    r = _MockRetriever()
    hits = tf.retrieve(r, "采购审批", top_k=5)
    assert r.calls[0]["tenant_id"] == "tenant_a"
    assert r.calls[0]["top_k"] == 5
    assert hits and all("tenant_a" in h["doc_id"] for h in hits)


def test_retrieve_default_tenant():
    tf = TenantFilter()  # 未指定 → default
    r = _MockRetriever()
    tf.retrieve(r, "问题", top_k=3)
    assert r.calls[0]["tenant_id"] == "default"


# ==================== verify_isolation（payload 级基础语义） ====================


def test_verify_isolation_basic():
    r = _MockRetriever()
    res = TenantFilter().verify_isolation(r, "tenant_a", "tenant_b", "采购", top_k=5)
    assert res["isolated"] is True
    assert res["overlap"] == []


def test_verify_isolation_overlap_detected():
    """两租户共享 doc 时须检出 overlap（隔离判定忠实反映数据）。"""

    class _Shared:
        def retrieve(self, question, top_k=5, tenant_id=None, **kw):
            return [{"doc_id": "shared-doc"}, {"doc_id": f"{tenant_id}-doc"}]

    res = TenantFilter().verify_isolation(_Shared(), "a", "b", "q")
    assert res["isolated"] is False
    assert "shared-doc" in res["overlap"]


# ==================== verify_sharded_isolation（双路判定） ====================


def test_sharded_isolation_both_layers(sharding_on):
    """分片 on：路由层隔离（不同分片）+ 数据层隔离（零交集）→ 总判定 isolated。"""
    r = _MockRetriever()
    res = TenantFilter().verify_sharded_isolation(r, "tenant_a", "tenant_b", "采购")
    assert res["route"]["tenant_a"] != res["route"]["tenant_b"]
    assert res["route"]["isolated"] is True
    assert res["data"]["isolated"] is True
    assert res["isolated"] is True


def test_sharded_isolation_off_route_degraded_to_base(sharding_off):
    """分片 off（灰度）：路由层退化到 base collection，但数据层隔离仍判定——
    双路判定如实区分"物理分片"与"payload 过滤"两个隔离来源。"""
    r = _MockRetriever()
    res = TenantFilter().verify_sharded_isolation(r, "tenant_a", "tenant_b", "采购")
    assert res["route"]["tenant_a"] == res["route"]["tenant_b"] == sharding.base_collection()
    assert res["route"]["isolated"] is False  # 未物理分片
    assert res["data"]["isolated"] is True  # payload 过滤仍隔离
    assert res["isolated"] is False  # 双路 AND：未到 collection 分片级


def test_sharded_isolation_deterministic_route(sharding_on):
    """同租户路由稳定（两次调用同分片）——幂等迁移/重跑的前提。"""
    r = _MockRetriever()
    a1 = TenantFilter().verify_sharded_isolation(r, "t_huadong", "t_huabei", "q")
    a2 = TenantFilter().verify_sharded_isolation(r, "t_huadong", "t_huabei", "q")
    assert a1["route"] == a2["route"]
