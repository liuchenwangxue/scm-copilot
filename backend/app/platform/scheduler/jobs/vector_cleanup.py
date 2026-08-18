"""vector_cleanup：向量卫生清理（每日 03:00，W25 Day2）。

cron: 0 3 * * *
作用（对照手册 Day2 下午）：
1. **孤儿向量**：Qdrant 中 payload `source_doc_id` 已不在 docs 表（active）→ 删除。
   - 增量同步失败中断（Qdrant 已插、docs 表未提交）或手动直接删文档等异常路径的兜底
   - 实现：scroll 全量遍历（点少，page_size=500 够快）→ 按 doc_id 聚合 → 缺失则过滤删除
2. **语义缓存过期键扫描**：Redis `scm:semcache:*:keys` 集合成员对应的 entry 已过期
   （TTL 到期但索引 set 没清，漏网）→ srem；entry 的 version 字段与 key 前缀版本不符
   （版本失效标记）→ 删 entry + srem。

幂等：清理是"目标状态收敛"，重复执行结果一致。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.models import DocMeta
from app.shared import config
from app.shared.rag.store import SCMStore
from app.shared.reliability.redis_client import RedisClient, get_redis_client

CRON = "0 3 * * *"

_SCROLL_PAGE = 500
_SEMCACHE_PREFIX = "scm:semcache:*:keys"


# ==================== 孤儿向量（纯逻辑可测） ====================


def _aggregate_by_doc(points: list[dict]) -> dict[str, int]:
    """把 Qdrant scroll 的 point 列表按 payload source_doc_id 聚合为 doc_id → 点数。

    输入元素来自 scroll(..., with_payload=True)，取 id + payload（去 payload 里的 text 省内存）。
    """
    agg: dict[str, int] = defaultdict(int)
    for p in points:
        pid = p.get("payload") or {}
        doc_id = pid.get("source_doc_id") or pid.get("doc_id")
        if doc_id:
            agg[doc_id] += 1
    return dict(agg)


def find_orphan_doc_ids(point_docs: dict[str, int], active_doc_ids: set[str]) -> list[str]:
    """判定孤儿：Qdrant 有向量但 docs 表无该 doc（active）→ 待清理。

    point_docs: {doc_id: 点数}；active_doc_ids: docs 表 status=active 的 doc_id 集合。
    """
    return sorted(d for d in point_docs if d not in active_doc_ids)


# ==================== 语义缓存过期键（纯逻辑可测） ====================


def find_expired_semcache_members(entries: dict[str, Any], version: str) -> list[str]:
    """判定失效缓存成员：entry 丢失（TTL 漏网）或 version 字段与当前版本不符。

    entries: {query_hash: entry_dict|None}（None 表示 entry key 已过期不存在）；
    version: 当前 SEMANTIC_CACHE_VERSION。
    返回应 srem/删除的 query_hash 列表。
    """
    stale: list[str] = []
    for qh, entry in entries.items():
        if entry is None:
            stale.append(qh)
            continue
        if entry.get("version") != version:
            stale.append(qh)
    return stale


# ==================== 业务执行 ====================


def _scan_qdrant_docs(store: SCMStore) -> dict[str, int]:
    """scroll 全量 points → {doc_id: 点数}（只取 id + payload 元数据，不取 text）。"""
    agg: dict[str, int] = defaultdict(int)
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection,
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        agg.update(_aggregate_by_doc([{"payload": p.payload} for p in points]))
        if not points or offset is None:
            break
    return dict(agg)


def _delete_orphan(store: SCMStore, doc_id: str) -> None:
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        FilterSelector,
        MatchValue,
    )

    store.client.delete(
        collection_name=store.collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="source_doc_id", match=MatchValue(value=doc_id))]
            )
        ),
        wait=True,
    )


def _clean_semcache(rc: RedisClient, version: str) -> dict:
    """扫描 `scm:semcache:*:keys` 集合：清理 TTL 漏网成员与版本失效标记。"""
    removed_members = 0
    removed_entries = 0
    for idx_key in rc.scan_keys(_SEMCACHE_PREFIX):
        hashes = rc.smembers(idx_key)
        if not hashes:
            continue
        ns_prefix = idx_key.removesuffix(":keys")  # scm:semcache:{version}
        entries: dict[str, Any] = {}
        for qh in hashes:
            raw = rc.get(f"{ns_prefix}:{qh}")
            if raw is None:
                entries[qh] = None
                continue
            try:
                import json

                entries[qh] = json.loads(raw)
            except (ValueError, TypeError):
                entries[qh] = None
        stale = find_expired_semcache_members(entries, version)
        if stale:
            rc.srem(idx_key, *stale)
            removed_members += len(stale)
            rc.delete_many([f"{ns_prefix}:{qh}" for qh in stale])
            removed_entries += len(stale)
    return {"removed_members": removed_members, "removed_entries": removed_entries}


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    store: SCMStore,
    rc: RedisClient,
    version: str,
) -> dict:
    from sqlalchemy import select

    # 1) 孤儿向量
    point_docs = _scan_qdrant_docs(store)
    async with session_factory() as session:
        active_ids = set(
            (await session.scalars(select(DocMeta.doc_id).where(DocMeta.status == "active"))).all()
        )
    orphans = find_orphan_doc_ids(point_docs, active_ids)
    deleted_docs = []
    for doc_id in orphans:
        _delete_orphan(store, doc_id)
        deleted_docs.append(doc_id)

    # 2) 语义缓存过期键（fail-open：Redis 不可用返回空，不影响主清理）
    sem = _clean_semcache(rc, version)

    return {
        "job": "vector_cleanup",
        "status": "success",
        "orphan_docs": deleted_docs,
        "orphan_points": sum(point_docs.get(d, 0) for d in orphans),
        "scanned_points": sum(point_docs.values()),
        "semcache": sem,
    }


async def run(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    store: SCMStore | None = None,
    rc: RedisClient | None = None,
    version: str | None = None,
) -> dict:
    """调度器入口（无参契约）。单测可注入依赖。"""
    from app.platform.scheduler import _runtime

    sf = session_factory or _runtime.get("session_factory")
    if sf is None:
        return {
            "job": "vector_cleanup",
            "status": "degraded",
            "error": "scheduler runtime not initialized",
        }
    store = store or SCMStore()
    rc = rc or get_redis_client()
    version = version or config.SEMANTIC_CACHE_VERSION
    try:
        return await _cleanup(sf, store, rc, version)
    except Exception as e:  # noqa: BLE001
        return {"job": "vector_cleanup", "status": "failed", "error": str(e)}


# 类型引用别名
AsyncSessionFactory = async_sessionmaker[AsyncSession]
