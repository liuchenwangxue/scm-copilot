"""多租户 collection 分片路由（★ W28 Day4，C4/B7 项落地）。

背景（三级演进的第一级→第二级）：
- W18 起租户隔离是 **payload 过滤级**（单 collection `scm_kb_v1`，检索强制 must tenant_id）。
- W28-D4 演进到 **collection 分片级**：租户 → 按 `crc32(tenant_id) % shards` 路由到独立
  collection（`scm_kb_v1_0` ~ `scm_kb_v1_3`）。分片是**性能隔离**（隔离语料更小 → 检索更快、
  单分片写放大可控）；payload filter 作为**正确性兜底**保留（双保险——即使路由层被绕过，
  检索依然强制 must tenant_id，杜绝跨租户泄露）。
- 第三级"独立实例"仅留作 ADR-009 的演进路径，不在本文件实现。

开关与灰度（迁移期间新旧并存、回退零成本）：
- `SCM_SHARDING=on|off`（默认 off）：off 时 `collection_for()` 恒返回 base collection，
  行为与 W28-D4 之前完全一致（回归测试保证调用方无感知）。
- `SCM_SHARD_COUNT=4`：分片数。演示数据可调大（如 12 租户验证分布）后改回 4。

命名：`{SCM_COLLECTION}_{crc32 % shards}`——基于 base collection 派生，SCM_COLLECTION
可配置时依然自洽（手册示意为 `kb_N`，本项目以 `scm_kb_v1_N` 落地，语义等价）。
"""

import zlib

from app.shared import config

# 开关与分片数统一由 shared/config.py 管理（.env 注入；测试用 monkeypatch）
SHARDING_ENABLED = config.SCM_SHARDING in ("1", "on", "true", "yes")
SHARD_COUNT = config.SHARD_COUNT


def base_collection() -> str:
    """未分片时使用的 collection（= config.SCM_COLLECTION，默认 scm_kb_v1）。"""
    return config.SCM_COLLECTION


def shard_of(tenant_id: str, shards: int | None = None) -> int:
    """租户 → 分片号。crc32 确定性路由（同租户永远同分片，幂等迁移前提）。

    注意：crc32 分布对少量租户可能倾斜（坑提示）——演示数据补足 12 租户验证分布，
    或直接说明并接受倾斜（分片数 > 租户数的场景本来就是过度设计）。
    """
    n = shards or config.SHARD_COUNT
    return zlib.crc32(tenant_id.encode("utf-8")) % n


def collection_for(tenant_id: str, shards: int | None = None) -> str:
    """租户 → collection 名。`SCM_SHARDING=off` 时恒返回 base collection（灰度开关）。"""
    if not SHARDING_ENABLED:
        return base_collection()
    return f"{base_collection()}_{shard_of(tenant_id, shards)}"


def all_collections(shards: int | None = None) -> list[str]:
    """全部分片 collection 名（迁移脚本建 collection / 验证脚本盘点用）。"""
    if not SHARDING_ENABLED:
        return [base_collection()]
    n = shards or config.SHARD_COUNT
    return [f"{base_collection()}_{i}" for i in range(n)]


def distribution(tenants: list[str], shards: int | None = None) -> dict[str, int]:
    """多租户在分片上的分布（演示/迁移干跑用：验证 crc32 倾斜程度）。"""
    if not SHARDING_ENABLED:
        return {base_collection(): len(tenants)}
    n = shards or config.SHARD_COUNT
    counts = {f"{base_collection()}_{i}": 0 for i in range(n)}
    for t in tenants:
        counts[f"{base_collection()}_{shard_of(t, n)}"] += 1
    return counts
