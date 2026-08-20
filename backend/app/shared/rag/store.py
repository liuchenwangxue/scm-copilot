"""Qdrant 存储封装（参考 W7 poi_store.py，升级为项目 A 用法）。

职责：
- collection 管理（创建/删除/重建，参数可配——Day4 B1 HNSW 实验用）
- 批量 upsert（100 块/批，payload 带 doc_id / section_path / category / topic）
- 查询（Top-K，可选 payload 过滤——为 W18 多租户隔离预留）
"""

from typing import cast

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    VectorParams,
)

from app.shared import config


class SCMStore:
    """供应链知识库 Qdrant 存储。"""

    def __init__(
        self, collection: str | None = None, url: str | None = None, timeout: int | None = None
    ):
        self.collection = collection or config.SCM_COLLECTION
        # ★ W26 Day2 演练优化：check_compatibility=False——跳过每次查询前的
        #   server version 检查。Qdrant 挂时该检查会先等待超时（拖慢降级链），
        #   关闭后 query 直接失败 → 快速转 BM25-only 降级。
        self.client = QdrantClient(
            url=url or config.QDRANT_URL,
            timeout=timeout or config.QDRANT_TIMEOUT,
            check_compatibility=False,
        )

    # ---------- collection 管理 ----------

    def create_collection(
        self, dim: int, hnsw_m: int = 16, hnsw_ef: int = 200, overwrite: bool = False
    ) -> dict:
        """创建 collection（不存在才建；overwrite=True 强制重建）。
        hnsw_m / hnsw_ef 对应 Qdrant HNSW 的 m / ef_construct（qdrant-client>=1.10 字段名）。
        返回 collection 信息。"""
        from qdrant_client.models import HnswConfigDiff

        exists = self.client.collection_exists(self.collection)
        if exists and not overwrite:
            return self.info()
        if exists and overwrite:
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(
                size=dim,
                distance=Distance.COSINE,
                hnsw_config=HnswConfigDiff(
                    m=hnsw_m, ef_construct=hnsw_ef, full_scan_threshold=10000
                ),
            ),
        )
        return self.info()

    def delete_collection(self) -> None:
        self.client.delete_collection(self.collection)

    def info(self) -> dict:
        """collection 状态（向量数 / 维度 / 配置）。"""
        from qdrant_client.models import HnswConfigDiff, VectorParams

        c = self.client.get_collection(self.collection)
        vp = c.config.params.vectors
        # 多向量配置时是 dict（本 collection 单向量，取 default 即可，防御性处理）
        if isinstance(vp, dict):
            params = vp.get("default")
            if params is None:
                params = next(iter(vp.values()), VectorParams(size=0, distance=Distance.COSINE))
            params = cast(VectorParams, params)
        else:
            params = cast(VectorParams, vp)
        hnsw = params.hnsw_config if params.hnsw_config is not None else HnswConfigDiff()
        return {
            "collection": self.collection,
            "points_count": c.points_count,
            "dim": params.size,
            "distance": str(params.distance),
            "hnsw": {
                "m": hnsw.m,
                "ef_construct": hnsw.ef_construct,
            },
        }

    # ---------- 写入 ----------

    def upsert_with_vectors(
        self, chunks: list[dict], vectors, tenant_id: str | None = None, id_offset: int = 0
    ) -> int:
        """携带预计算向量的 upsert（向量与 chunks 顺序一一对应）。

        tenant_id（★ W18 Day6 多租户隔离）：非空时写入每个 point 的 payload。
        id_offset：给 point id 加偏移，避免同一 collection 里不同租户的 point id 冲突（覆盖）。
        不重建索引即可实现"payload 过滤级"租户隔离（体量小选 payload 级，成本低易扩展）。"""
        from qdrant_client.models import PointStruct

        assert len(chunks) == len(vectors), "chunks 与 vectors 数量不一致"

        topic_map = {
            "PUR": "采购",
            "SUP": "供应商",
            "INV": "库存",
            "LOG": "物流",
            "QC": "质量",
            "FIN": "结算",
            "CMP": "合规",
            "ORG": "组织",
        }
        points = []
        for i, c in enumerate(chunks):
            payload = {
                "chunk_id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "section_path": c.get("section_path", ""),
                "topic": topic_map.get(
                    c.get("doc_id", "").split("-")[1] if "-" in c.get("doc_id", "") else "", "未知"
                ),
                "text": c["text"],
            }
            if tenant_id:
                payload["tenant_id"] = tenant_id
            points.append(
                PointStruct(id=id_offset + i, vector=vectors[i].tolist(), payload=payload)
            )
        for i in range(0, len(points), 100):
            self.client.upsert(collection_name=self.collection, points=points[i : i + 100])
            print(f"[store] upsert 第 {i // 100 + 1} 批：{min(100, len(points) - i)} 块")
        return len(points)

    # ---------- 查询 ----------

    def query(
        self,
        query_vector: list[float],
        top_k: int = 5,
        topic: str | None = None,
        tenant_id: str | None = None,
        retries: int | None = None,
        collection: str | None = None,
        **kw,
    ) -> list[dict]:
        """Top-K 查询。topic 非空时按 payload.topic 过滤；tenant_id 非空时按 payload.tenant_id 过滤
        （★ W18 Day6 多租户隔离：payload 过滤级，强制 must 条件）。

        ★ W28 Day4（多租户分片 C4）：`collection` 可覆盖目标 collection——分片模式下
        检索器先路由 `sharding.collection_for(tenant_id)` 再查询；默认 None 用本实例
        collection（= 行为与分片前完全一致，调用方无感知）。

        可靠性：Qdrant 502/503（容器重启/瞬时抖动）→ 指数退避重试 3 次
        （W10 可靠性经验；实测 Docker Desktop 重启时 Qdrant 会短暂 502，
         评测全程不应因单次抖动崩溃）。

        ★ W26 Day2 故障演练修复：`retries` 可配——HybridRetriever 降级路径传
        retries=0（快速失败走 BM25-only），避免杀 Qdrant 时 30s 级重试拖垮
        响应（"降级不雪崩"：向量路快速失败，检索降级链接管）。"""
        if retries is None:
            retries = 3
        coll = collection or self.collection
        must = []
        if topic:
            must.append(FieldCondition(key="topic", match=MatchValue(value=topic)))
        if tenant_id:
            must.append(FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)))
        # 类型注释：qdrant-client 的 Filter.must 声明较宽（含 nested/filter 等），
        # 本用法合法（纯 FieldCondition 列表）——按手册"不被第三方卡住"原则标注忽略
        qfilter = Filter(must=must) if must else None  # type: ignore[arg-type]

        import random as _random
        import time

        last_exc: BaseException | None = None
        for attempt in range(retries + 1):  # 1 次 + retries 次重试
            try:
                hits = self.client.query_points(
                    collection_name=coll,
                    query=query_vector,
                    query_filter=qfilter,
                    limit=top_k,
                    with_payload=True,
                    **kw,
                ).points
                out = []
                for h in hits:
                    p = h.payload or {}
                    out.append(
                        {
                            "chunk_id": p["chunk_id"],
                            "doc_id": p["doc_id"],
                            "section_path": p.get("section_path", ""),
                            "topic": p.get("topic", ""),
                            "tenant_id": p.get("tenant_id", ""),
                            "text": p.get("text", ""),
                            "score": round(float(h.score), 4),
                        }
                    )
                return out
            except Exception as e:
                last_exc = e
                text = str(e)
                # 只重试瞬时故障（502/503/连接错误/WinError 10061 连接被拒），参数错误不重试
                retry_kw = (
                    "502",
                    "503",
                    "Bad Gateway",
                    "Connection",
                    "connection",
                    "10061",
                    "connect",
                    "Connection refused",
                    "refused",
                )
                if not any(k in text for k in retry_kw):
                    raise
                if attempt < 3:
                    delay = 0.5 * (2**attempt) + _random.uniform(0, 0.5)
                    print(
                        f"[store] Qdrant 瞬时故障重试 {attempt + 1}/3: {text[:70]}（{delay:.1f}s）"
                    )
                    time.sleep(delay)
        if last_exc is None:  # 防御：循环正常退出（理论上不会走到）
            raise RuntimeError("Qdrant 查询异常退出")
        raise last_exc
