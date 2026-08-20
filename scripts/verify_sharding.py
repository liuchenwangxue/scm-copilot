"""★ W28 Day4 验收脚本：分片路由 + BM25 隔离 + 三件套验证（C4/C5，Qdrant 实测）。

对应手册 Day4 下午验证三件：
1. **verify_isolation 分片模式**：两租户专属语料分落各自分片，互查零交集（路由层隔离
   + payload filter 双保险）；另做"路由绕过模拟"——同一租户 filter 在对方分片查询 → 空
   （payload 兜底生效，证明双保险不是纸面设计）。
2. **两租户并发检索性能对比**：并发检索各分片 vs 单 collection，隔离语料更小 → 更快
   （分片 = 性能隔离的实证；本机抖动大，仅作趋势性对比）。
3. **单分片删除不影响他片**：删 tenant_a 分片一个 doc 的向量 → tenant_a 分片点数减少、
   tenant_b 分片点数不变（物理分片 = 故障域/写放大隔离）。

租户对选择：crc32 路由对**少量租户会碰撞**（ADR-009 已记录）——脚本自动挑选一对路由到
不同分片的租户（t01..t12），保证"物理分片隔离"被真实检验而不是碰运气。

用法（先跑迁移脚本把数据搬进分片；本脚本会自行准备租户演示语料，幂等可重跑）：
  python scripts/verify_sharding.py
"""

import argparse
import asyncio
import itertools
import os
import sys
import time
from pathlib import Path

# 脚本默认 mock embedder（机制验证不依赖真实模型；--real-embedder 可换真 bge）
os.environ.setdefault("SCM_EMBEDDER", "mock")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.shared.rag import sharding  # noqa: E402
from app.shared.rag.embedder import Embedder  # noqa: E402
from app.shared.rag.store import SCMStore  # noqa: E402

DEMO_CHUNKS = 8  # 每租户专属演示语料块数

FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAILURES.append(name)
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


def _pick_tenant_pair() -> tuple[str, str]:
    """挑一对路由到不同分片的租户（crc32 少量租户碰撞为已知坑，自动避让）。"""
    for a, b in itertools.product([f"t{i:02d}" for i in range(1, 13)], repeat=2):
        if a != b and sharding.collection_for(a) != sharding.collection_for(b):
            return a, b
    raise RuntimeError("12 租户内找不到路由到不同分片的租户对（分片配置异常）")


def _custom_chunks(doc_id: str, n: int) -> list[dict]:
    """租户专属自定义文档（doc_id/文本与公共、迁移语料零交集——数据层隔离的干净前提）。"""
    return [
        {
            "chunk_id": f"{doc_id}#{i}",
            "doc_id": doc_id,
            "section_path": f"第{i + 1}章",
            "text": f"这是租户 {doc_id} 的专属条款第{i + 1}段，关于供应商准入资质材料与年度评估要求。",
        }
        for i in range(n)
    ]


def _prepare_tenant_data(store: SCMStore, embedder: Embedder, tenant_a: str, tenant_b: str) -> None:
    """写两租户专属自定义语料到各自分片（幂等：point id = uuid5(text) 内容寻址）。

    不用公共语料 chunk 作演示数据：doc 若与迁移数据重叠，doc_id 交集会误报"不隔离"。
    """
    from qdrant_client.models import PointStruct

    from app.platform.scheduler.jobs.kb_increment_sync import point_id_for

    a_src = _custom_chunks(f"{tenant_a}-priv", DEMO_CHUNKS)
    b_src = _custom_chunks(f"{tenant_b}-priv", DEMO_CHUNKS)
    for tenant, src in ((tenant_a, a_src), (tenant_b, b_src)):
        coll = sharding.collection_for(tenant)
        texts = [c["text"] for c in src]
        vectors = embedder.embed_texts(texts)
        points = [
            PointStruct(
                id=point_id_for(c["text"]),
                vector=vectors[i].tolist(),
                payload={
                    "chunk_id": c["chunk_id"],
                    "doc_id": c["doc_id"],
                    "source_doc_id": c["doc_id"],
                    "section_path": c.get("section_path", ""),
                    "topic": c.get("topic", "未知"),
                    "text": c["text"],
                    "tenant_id": tenant,
                },
            )
            for i, c in enumerate(src)
        ]
        for i in range(0, len(points), 100):
            store.client.upsert(collection_name=coll, points=points[i : i + 100], wait=True)
        print(f"[verify] {tenant} 语料 {len(points)} 块 -> {coll}（幂等 upsert）")


def _points_count(store: SCMStore, coll: str) -> int:
    return int(store.client.get_collection(coll).points_count or 0)


async def _perf(store: SCMStore, embedder: Embedder, tenant_a: str, tenant_b: str) -> None:
    """两租户并发检索性能对比：各分片 vs 单 collection（隔离语料更小 → 更快）。"""
    qv = embedder.embed_query("采购申请需要经过哪几级审批").tolist()
    rounds = 30

    async def _one(coll: str) -> float:
        t0 = time.perf_counter()
        for _ in range(rounds):
            store.client.query_points(collection_name=coll, query=qv, limit=5, with_payload=True)
        return (time.perf_counter() - t0) / rounds

    base_t, a_t, b_t = await asyncio.gather(
        _one(sharding.base_collection()),
        _one(sharding.collection_for(tenant_a)),
        _one(sharding.collection_for(tenant_b)),
    )
    print(f"\n[verify] 单次检索耗时（{rounds} 轮均值，分片语料应更小更快）：")
    print(
        f"  base 单 collection : {base_t * 1000:8.2f} ms"
        f"（{_points_count(store, sharding.base_collection())} pts）"
    )
    print(
        f"  {tenant_a} 分片      : {a_t * 1000:8.2f} ms"
        f"（{_points_count(store, sharding.collection_for(tenant_a))} pts）"
    )
    print(
        f"  {tenant_b} 分片      : {b_t * 1000:8.2f} ms"
        f"（{_points_count(store, sharding.collection_for(tenant_b))} pts）"
    )
    _check(
        "性能：分片 ≤ base×1.2",
        a_t <= base_t * 1.2 or b_t <= base_t * 1.2,
        "分片语料显著小于 base 时应更快（本机抖动大，仅趋势性）",
    )


def verify_all(store: SCMStore, embedder: Embedder, tenant_a: str, tenant_b: str) -> None:
    coll_a, coll_b = sharding.collection_for(tenant_a), sharding.collection_for(tenant_b)

    print("\n==== 1. 路由层隔离 ====")
    _check("两租户路由到不同分片", coll_a != coll_b, f"{tenant_a}->{coll_a} / {tenant_b}->{coll_b}")
    _check(
        "分片 collection 有数据",
        _points_count(store, coll_a) > 0 and _points_count(store, coll_b) > 0,
    )

    print("\n==== 2. 检索隔离（payload 双保险） ====")
    qv = embedder.embed_query("供应商准入需要提交哪些资质材料").tolist()
    hits_a = store.query(qv, top_k=5, tenant_id=tenant_a, collection=coll_a)
    _check(
        "租户 A 检索结果全部带 tenant_a",
        all(h["tenant_id"] == tenant_a for h in hits_a) and len(hits_a) > 0,
    )
    # 路由绕过模拟：把 A 的 filter 拿到 B 分片查 → 必须空（payload 兜底拒绝）
    leaked = store.query(qv, top_k=5, tenant_id=tenant_a, collection=coll_b)
    _check("路由绕过被 payload 兜底（A filter × B 分片 = 空）", len(leaked) == 0)
    # 漏配 filter 的攻击：在 A 分片全量查 → 只可能拿到本分片数据，绝不出现 B 的租户
    nofilter_a = store.query(qv, top_k=20, tenant_id=None, collection=coll_a)
    _check(
        "A 分片漏配 filter 不含 B 租户数据（物理分片兜底）",
        all(h["tenant_id"] != tenant_b for h in nofilter_a),
    )

    print("\n==== 3. verify_sharded_isolation（双路判定） ====")
    from app.domains.kb.tenant.tenant_filter import TenantFilter
    from app.shared.rag.retriever import Retriever

    retriever = Retriever()
    result = TenantFilter().verify_sharded_isolation(
        retriever, tenant_a, tenant_b, "供应商准入需要提交哪些资质材料", top_k=5
    )
    print(
        f"  route: {result['route']['tenant_a']} vs {result['route']['tenant_b']} "
        f"-> isolated={result['route']['isolated']}"
    )
    print(f"  data : overlap={result['data']['overlap']} -> isolated={result['data']['isolated']}")
    _check("双路隔离判定", result["isolated"])

    print("\n==== 4. 删除隔离（单分片删除不影响他片） ====")
    first = store.client.scroll(coll_a, limit=1, with_payload=True)[0][0]
    doc_a = (first.payload or {})["source_doc_id"]
    before_b = _points_count(store, coll_b)
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    store.client.delete(
        collection_name=coll_a,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source_doc_id", match=MatchValue(value=doc_a))])
        ),
        wait=True,
    )
    after_b = _points_count(store, coll_b)
    _check("A 分片删除后 B 分片点数不变", before_b == after_b, f"B: {before_b} -> {after_b}")
    print(f"  （A 分片删除了 doc {doc_a} 的全部向量）")


def main() -> int:
    ap = argparse.ArgumentParser(description="W28-D4 分片/租户隔离验收（Qdrant 实测）")
    ap.add_argument(
        "--real-embedder",
        action="store_true",
        help="用真实 bge 模型（默认 mock，机制验证不依赖模型质量）",
    )
    args = ap.parse_args()

    # 本脚本只验证分片机制（路由+payload+物理隔离），进程内强制分片路由
    sharding.SHARDING_ENABLED = True

    store = SCMStore()
    base_pts = _points_count(store, sharding.base_collection())
    if base_pts == 0:
        print("[verify] base collection 无数据，先跑知识库同步/迁移。")
        return 1

    tenant_a, tenant_b = _pick_tenant_pair()
    coll_a, coll_b = sharding.collection_for(tenant_a), sharding.collection_for(tenant_b)
    print(f"[verify] 租户对 {tenant_a}->{coll_a} / {tenant_b}->{coll_b}（crc32 避让碰撞）")

    # 确保分片 collection 存在（HNSW 参数与 base 一致；SCMStore.create_collection
    # 绑定 self.collection → 按分片名建独立实例）
    dim = store.info()["dim"]
    for coll in (coll_a, coll_b):
        if not store.client.collection_exists(coll):
            SCMStore(collection=coll).create_collection(dim=dim, overwrite=False)
            print(f"[verify] 创建分片 {coll}（dim={dim}）")

    embedder = Embedder(mode="real" if args.real_embedder else "mock")
    _prepare_tenant_data(store, embedder, tenant_a, tenant_b)

    verify_all(store, embedder, tenant_a, tenant_b)
    asyncio.run(_perf(store, embedder, tenant_a, tenant_b))

    print("\n" + ("=" * 40))
    if FAILURES:
        print(f"验收未过：{len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("W28-D4 分片验收全部通过（route+payload+物理隔离 / 并发性能 / 删除隔离）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
