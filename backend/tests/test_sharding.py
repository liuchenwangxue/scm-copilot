"""W28 Day4 多租户分片路由测试（C4/B7）：crc32 路由确定性/分布/开关灰度。

CI 可跑（纯函数，无 Qdrant/模型）：collection_for / shard_of / all_collections /
distribution 全部是确定性逻辑，路由正确性 = 隔离正确性的第一道闸。
"""

import pytest

from app.shared.rag import sharding


@pytest.fixture
def sharding_on(monkeypatch):
    """进程内开启分片路由（等价 SCM_SHARDING=on）。"""
    monkeypatch.setattr(sharding, "SHARDING_ENABLED", True)
    return sharding


@pytest.fixture
def sharding_off(monkeypatch):
    """分片关闭（默认灰度态）。"""
    monkeypatch.setattr(sharding, "SHARDING_ENABLED", False)
    return sharding


# ==================== 开关灰度（off = 行为与分片前一致） ====================


def test_off_returns_base_collection(sharding_off):
    """SCM_SHARDING=off → 任何租户都路由到 base collection（灰度回退零成本）。"""
    assert sharding.collection_for("any_tenant") == sharding.base_collection()
    assert sharding.collection_for("t_huadong") == sharding.base_collection()


def test_off_all_collections_is_base_only(sharding_off):
    assert sharding.all_collections() == [sharding.base_collection()]
    assert sharding.distribution(["a", "b", "c"]) == {sharding.base_collection(): 3}


# ==================== 路由确定性 ====================


def test_collection_for_deterministic(sharding_on):
    """同租户永远同分片（幂等迁移的前提）。"""
    assert sharding.collection_for("tenant_a") == sharding.collection_for("tenant_a")
    assert sharding.collection_for("t_huadong") == sharding.collection_for("t_huadong")


def test_collection_for_differs_by_tenant(sharding_on):
    """不同租户路由到不同分片（至少对测试租户对成立——见分布测试）。"""
    colls = {sharding.collection_for(t) for t in ("t_huadong", "t_huabei", "t_huanan")}
    assert len(colls) >= 2, f"3 租户应至少落 2 个分片，实际 {colls}"


def test_collection_name_pattern(sharding_on):
    coll = sharding.collection_for("tenant_a")
    assert coll.startswith(sharding.base_collection() + "_")
    shard = int(coll.rsplit("_", 1)[1])
    assert 0 <= shard < sharding.SHARD_COUNT


def test_shard_of_in_range(sharding_on):
    for t in ("", "a", "t_huadong", "中文租户", "a" * 100):
        assert 0 <= sharding.shard_of(t) < sharding.SHARD_COUNT


def test_shard_count_param(sharding_on):
    """自定义分片数：路由与默认 4 分片一致（迁移/演示可调）。"""
    t = "t_huadong"
    assert (
        sharding.collection_for(t, shards=8)
        == f"{sharding.base_collection()}_{sharding.shard_of(t, 8)}"
    )
    assert len(sharding.all_collections(shards=8)) == 8


# ==================== 分布 ====================


def test_distribution_12_tenants_covers_all_shards(sharding_on):
    """演示数据补足 12 租户：crc32 应覆盖全部 4 分片（手册坑：少量租户会倾斜）。"""
    tenants = [f"t{i:02d}" for i in range(1, 13)]
    dist = sharding.distribution(tenants)
    assert sum(dist.values()) == 12
    assert set(dist) == set(sharding.all_collections())
    assert all(v > 0 for v in dist.values()), f"12 租户应铺满 4 分片，实际 {dist}"


def test_route_matches_zlib_spec(sharding_on):
    """路由实现与规格 `crc32(tenant_id) % shards` 严格一致（迁移脚本复用同一路由）。"""
    import zlib

    for t in ("default", "t_huadong", "华东区域"):
        expect = f"{sharding.base_collection()}_{zlib.crc32(t.encode('utf-8')) % 4}"
        assert sharding.collection_for(t) == expect
        assert sharding.shard_of(t) == zlib.crc32(t.encode("utf-8")) % 4


# ==================== 边界 ====================


def test_empty_tenant_id_routes_stably(sharding_on):
    a = sharding.collection_for("")
    b = sharding.collection_for("")
    assert a == b


def test_unicode_tenant(sharding_on):
    """中文/带特殊字符租户名不崩，确定性路由。"""
    a = sharding.collection_for("华东区域")
    assert a == sharding.collection_for("华东区域")
