"""★ W28 Day4 迁移脚本：单 collection → 4 分片（C4，幂等可重跑）。

背景：租户隔离从 payload 过滤级演进到 collection 分片级。本脚本把现有单
collection（`scm_kb_v1`，point id = uuid5(text) 内容寻址）按租户哈希搬入
`scm_kb_v1_0..N-1` 分片，**point id 原样保留**（同内容幂等，重跑覆盖零重复）。

租户归属判定（数据本身可能没有 tenant_id payload）：
- payload 已有 `tenant_id` → 直接用（真多租户数据）
- 无 → 按 `--tenant-policy`：
  · `default`：全部归 `default` 租户（现有公共语料的保守归属）
  · `spread`：按 doc_id 哈希分配到 `--tenants` 个演示租户（t01..tNN）——
    用于验证 crc32 分布倾斜（手册坑：4 分片 × 少量租户会倾斜，演示补足 12 租户）

灰度说明：迁移期间新旧 collection 并存——运行时 `SCM_SHARDING=off`（默认）仍走
单 collection，行为不变；切换 `on` 即走分片（零迁移成本回退）。

用法：
  python scripts/migrate_sharding.py --dry-run --tenant-policy spread --tenants 12
  python scripts/migrate_sharding.py --tenant-policy default
  python scripts/migrate_sharding.py --tenant-policy spread --tenants 12 --shards 4
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from qdrant_client.models import PointStruct  # noqa: E402

from app.shared import config  # noqa: E402
from app.shared.rag import sharding  # noqa: E402
from app.shared.rag.store import SCMStore  # noqa: E402

_SCROLL_BATCH = 500


def _tenant_of(payload: dict, policy: str, tenants: int, doc_id: str) -> str:
    """判定一个 point 的租户归属。"""
    if payload.get("tenant_id"):
        return payload["tenant_id"]
    if policy == "spread":
        import hashlib

        n = int(hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:4], 16) % tenants
        return f"t{n + 1:02d}"  # t01..tNN
    return "default"


def _scroll_all(store: SCMStore, collection: str) -> list:
    """scroll 全部 points（含向量 + payload），分页直到 next_offset 为空。"""
    pts, offset = [], None
    while True:
        batch, offset = store.client.scroll(
            collection_name=collection,
            limit=_SCROLL_BATCH,
            with_vectors=True,
            with_payload=True,
            offset=offset,
        )
        pts.extend(batch)
        if not offset:
            break
    return pts


def main() -> int:
    ap = argparse.ArgumentParser(description="单 collection → 分片迁移（幂等可重跑）")
    ap.add_argument("--dry-run", action="store_true", help="只统计分布，不写 Qdrant")
    ap.add_argument("--shards", type=int, default=config.SHARD_COUNT, help="分片数")
    ap.add_argument(
        "--tenant-policy",
        choices=["default", "spread"],
        default="default",
        help="无 tenant_id 数据的归属策略",
    )
    ap.add_argument(
        "--tenants", type=int, default=12, help="spread 策略的演示租户数（默认 12——补足分布验证）"
    )
    args = ap.parse_args()

    # 迁移目标按分片路由计算（与"将来 SCM_SHARDING=on"完全一致）；
    # 进程内临时开启路由，不改运行时开关（迁移期间新旧并存、回退零成本）。
    sharding.SHARDING_ENABLED = True
    sharding.SHARD_COUNT = args.shards

    store = SCMStore()
    base = sharding.base_collection()
    try:
        store.client.get_collection(base)
    except Exception as e:  # noqa: BLE001
        print(f"[migrate] base collection 不存在（{e}）；分片迁移无从谈起，退出。")
        return 1

    # 读 base 全部 points（dry-run 之前先读，保证 dry-run 零副作用——不建 collection）
    points = _scroll_all(store, base)
    print(f"[migrate] base {base} 共 {len(points)} points")

    # 按租户分组 → 路由到分片
    per_coll: dict[str, list] = {c: [] for c in sharding.all_collections(args.shards)}
    tenant_of: dict[str, int] = {}
    for p in points:
        payload = p.payload or {}
        t = _tenant_of(payload, args.tenant_policy, args.tenants, payload.get("doc_id", ""))
        tenant_of.setdefault(t, 0)
        tenant_of[t] += 1
        per_coll[sharding.collection_for(t, args.shards)].append(p)

    print("\n租户分布（crc32 路由；少量租户倾斜为已知坑，ADR-009 已记录）：")
    for t, n in sorted(tenant_of.items()):
        print(f"  {t:<12} -> {sharding.collection_for(t, args.shards):<20} {n} points")

    if args.dry_run:
        print("\n[dry-run] 分片目标统计（未创建 collection、未写入）：")
        for coll, pts in per_coll.items():
            print(f"  {coll:<20} {len(pts)} points")
        return 0

    # 建分片 collection（HNSW 参数与 base 一致，保证分片间召回率齐——手册坑；
    # 注意：SCMStore.create_collection 绑定 self.collection，需按分片名建独立实例）
    dim = store.info()["dim"]
    for coll in sharding.all_collections(args.shards):
        if not store.client.collection_exists(coll):
            SCMStore(collection=coll).create_collection(dim=dim, overwrite=False)
            print(f"[migrate] 创建分片 {coll}（dim={dim}）")
        else:
            print(f"[migrate] 分片 {coll} 已存在（幂等：只 upsert 覆盖，不重建）")

    # 幂等 upsert：point id 原样保留（uuid5 内容寻址）——重跑覆盖零重复
    for coll, pts in per_coll.items():
        if not pts:
            continue
        for i in range(0, len(pts), 100):
            chunk = [
                PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in pts[i : i + 100]
            ]
            store.client.upsert(collection_name=coll, points=chunk, wait=True)
        print(f"[migrate] -> {coll:<20} upsert {len(pts)} points（幂等）")

    # 落一份分布证据（演示/报告用）
    report = Path(config.REPORTS_DIR) / "sharding_migrate_report.json"
    report.write_text(
        __import__("json").dumps(
            {
                "base": base,
                "shards": args.shards,
                "tenant_policy": args.tenant_policy,
                "total": len(points),
                "per_coll": {c: len(v) for c, v in per_coll.items()},
                "tenants": tenant_of,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n[migrate] 分布证据 -> {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
