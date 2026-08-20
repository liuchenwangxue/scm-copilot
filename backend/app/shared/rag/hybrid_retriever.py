"""混合检索器：BM25 + 向量双路召回 → RRF 融合（W18 Day2，欠账 A12 落地）。

为什么混合（生产意义）：
- 纯向量：语义召回强，但对"精确数字/条款/专有名词"弱（问题表述与文档用词不同时失效）
- BM25：词面精确匹配强（条款号/数字/专有名词），但无语义泛化
- 供应链制度文档大量是"第X条/XX万元/XX个工作日"这类精确条款 → BM25 与向量天然互补
- RRF（Reciprocal Rank Fusion）：不依赖分数归一化（向量余弦 vs BM25 分数量纲完全不同），
  只按排名融合：score = Σ 1/(k + rank)。鲁棒、无需调权重。

接口与 Retriever 完全一致（retrieve(query, top_k) 返回同结构 list[dict]），
评测流水线只改注入点即可 A/B（阶段三原则：同评测集、同指标）。

来源标签：每个结果带 source ∈ {"vec", "bm25", "both"}（面试可讲"召回互补"）。
"""

import json
import re
from pathlib import Path

from app.shared import config
from app.shared.rag import sharding
from app.shared.rag.embedder import Embedder
from app.shared.rag.store import SCMStore

RRF_K = 60  # RRF 常数（手册指定 k=60）
VEC_CANDIDATES = 10  # 向量召回候选数
BM25_CANDIDATES = 10  # BM25 召回候选数


def _jieba_cut(text: str) -> list[str]:
    """jieba 分词（BM25 中文必须分词，否则中文检索基本废——手册 Day2 坑提示）。"""
    import jieba

    return [t for t in jieba.cut(text) if t.strip()]


class BM25Index:
    """chunk 级 BM25 索引：基于 chunks_title.json 构建，可缓存到文件。

    tokenize 用 jieba；索引体 rank_bm25.BM25Okapi。
    缓存文件: data/bm25_index_cache.json（词表 + 文档 token 化序列，够小可落地）。

    ★ W28 Day4（堵 C5）：新增租户维度——构建/加载时解析每条 chunk 的 `tenant_id`
    （缺失 = None = 公共语料）。`search()` 支持 `tenant_id`：先按租户过滤出候选
    文档再打分排序（隔离实验/分片检索时绝不让 BM25 路跨租户泄露）。
    实现说明：rank_bm25 的 BM25Okapi 分数逐文档独立（不受其他文档影响），故
    "全量打分 + 候选集排序" 与 "候选集重建 BM25 打分" 结果完全等价，且省去
    每次查询重建小索引的开销——过滤语义上发生在打分层之前（候选集即租户语料）。
    """

    def __init__(self, chunks: list[dict] | None = None, cache_file: Path | None = None):
        self.cache_file = cache_file or (config.DATA_DIR / "bm25_index_cache.json")
        self.chunks = chunks
        self.bm25 = None
        self.tokenized: list[list[str]] = []  # 与 chunks 顺序一致
        self.chunk_index: dict[str, int] = {}  # chunk_id -> idx
        self.chunk_tenant: dict[str, str | None] = {}  # chunk_id -> tenant_id | None（★ C5）

    def _index_tenants(self) -> None:
        """解析 chunk 的租户归属（缺失 → None = 公共语料）。构建/加载后都要调用。"""
        self.chunk_tenant = {c["chunk_id"]: c.get("tenant_id") for c in self.chunks or []}

    def build(self) -> None:
        """从 chunks 构建 BM25 索引（无缓存时）。"""
        from rank_bm25 import BM25Okapi

        if self.chunks is None:
            raise ValueError("chunks 未提供")
        self.tokenized = [_jieba_cut(c["text"]) for c in self.chunks]
        self.bm25 = BM25Okapi(self.tokenized)
        self.chunk_index = {c["chunk_id"]: i for i, c in enumerate(self.chunks)}
        self._index_tenants()

    def save(self) -> None:
        if self.chunks is None or self.tokenized is None:
            return
        self.cache_file.write_text(
            json.dumps({"chunks": self.chunks, "tokenized": self.tokenized}, ensure_ascii=False),
            encoding="utf-8",
        )

    def load(self) -> bool:
        """从缓存加载；成功返回 True。"""
        if not self.cache_file.exists():
            return False
        data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        from rank_bm25 import BM25Okapi

        self.chunks = data["chunks"]
        self.tokenized = data["tokenized"]
        self.bm25 = BM25Okapi(self.tokenized)
        self.chunk_index = {c["chunk_id"]: i for i, c in enumerate(self.chunks)}
        self._index_tenants()
        return True

    def search(self, query: str, top_k: int = 10, tenant_id: str | None = None) -> list[dict]:
        """BM25 Top-K，返回 [{chunk_id, score}]（rank_bm25 score 越大越相关）。

        tenant_id 非空 → 只在该租户语料内检索（fail-safe：租户语料为空返回 []，
        宁可不返回也不跨租户泄露——与 TenantFilter 的 fail-closed 一致）。"""
        if self.bm25 is None:
            raise RuntimeError("BM25 未构建")
        q_tokens = _jieba_cut(query)
        scores = self.bm25.get_scores(q_tokens)
        if tenant_id:
            candidates = [
                i
                for i, c in enumerate(self.chunks)
                if self.chunk_tenant.get(c["chunk_id"]) == tenant_id
            ]
            if not candidates:
                return []
            top_idx = sorted(candidates, key=lambda i: scores[i], reverse=True)[:top_k]
        else:
            top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [
            {"chunk_id": self.chunks[i]["chunk_id"], "score": float(scores[i])} for i in top_idx
        ]


class HybridRetriever:
    """混合检索：向量 Top-10 + BM25 Top-10 → 融合 → Top-N。

    融合方式（对齐 W4 方法论，W18 Day2b 补充 weighted）：
    - fusion="rrf"      （默认）RRF：score = Σ 1/(k + rank)，k=60，免调权重
    - fusion="weighted" 线性加权：候选并集 min-max 归一化后 score = α·vec + (1-α)·bm25，
                        α 由扫描确定（W4 实测制度条文 BM25 是主力，α≈0.3 即 BM25 占 70%）

    可选 reranker：构造传入 reranker 对象（实现 .rerank(query, candidates) -> 重排后 list）。
    有 reranker 时先取 Top-20 候选再重排取 top_k（Top-20→Top-5 精排）。
    """

    def __init__(
        self,
        collection: str | None = None,
        reranker=None,
        top_candidates: int = 20,
        fusion: str = "rrf",
        alpha: float = 0.5,
    ):
        self.embedder = Embedder()
        self.store = SCMStore(collection=collection or config.SCM_COLLECTION)
        self.reranker = reranker
        self.top_candidates = top_candidates  # 融合/重排候选数
        self.fusion = fusion  # rrf | weighted
        self.alpha = alpha  # weighted 的向量权重（BM25 权重 = 1-α）

        chunks = json.loads(Path(config.CHUNKS_FILE).read_text(encoding="utf-8"))
        self.chunk_meta = {c["chunk_id"]: c for c in chunks}
        self.bm25 = BM25Index(chunks)
        if not self.bm25.load():
            print("[hybrid] BM25 无缓存，构建中……")
            self.bm25.build()
            self.bm25.save()

    def _chunk_meta(self, chunk_id: str) -> dict:
        # ★ W28 Day4（租户隔离语料容错）：租户分片里的隔离语料不在公共 chunks_title.json
        #   中，`chunk_meta` 查不到 → 返回 minimal 元数据而非 KeyError（分片演示/隔离
        #   实验可以带 tenant 语料走完整混合检索，不炸链路）。
        c = self.chunk_meta.get(chunk_id)
        if c is None:
            return {
                "chunk_id": chunk_id,
                "doc_id": "",
                "section_path": "",
                "topic": "未知",
                "text": "",
            }
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
        return {
            "chunk_id": c["chunk_id"],
            "doc_id": c["doc_id"],
            "section_path": c.get("section_path", ""),
            "topic": topic_map.get(c["doc_id"].split("-")[1] if "-" in c["doc_id"] else "", "未知"),
            "text": c["text"],
        }

    def _rrf_fuse(
        self, vec_ranks: dict[str, int], bm25_ranks: dict[str, int], top_k: int
    ) -> list[dict]:
        """RRF 融合：score = Σ 1/(k + rank)。vec_ranks/bm25_ranks: chunk_id -> 0-based rank。"""
        fused: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        for cid, r in vec_ranks.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
            sources.setdefault(cid, set()).add("vec")
        for cid, r in bm25_ranks.items():
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
            sources.setdefault(cid, set()).add("bm25")
        # 分数降序 + chunk_id 升序（确定性 tie-breaker，避免 set 哈希顺序影响复现）
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        return [
            {
                "chunk_id": cid,
                "fused_score": round(sc, 6),
                "source": "both" if len(sources[cid]) == 2 else next(iter(sources[cid])),
            }
            for cid, sc in ordered
        ]

    def _weighted_fuse(
        self, vec_scores: dict[str, float], bm25_scores: dict[str, float], top_k: int
    ) -> list[dict]:
        """线性加权融合（W4 同款）：候选并集各自 min-max 归一化到 [0,1]，
        final = α·vec_norm + (1-α)·bm25_norm。α 由扫描确定（默认 0.5）。"""
        union = set(vec_scores) | set(bm25_scores)

        def _minmax(d: dict[str, float]) -> dict[str, float]:
            lo, hi = min(d.values()), max(d.values())
            if hi - lo < 1e-12:
                return {k: 0.0 for k in d}
            return {k: (v - lo) / (hi - lo) for k, v in d.items()}

        if union:
            # 只对候选并集内的分数归一化（局部归一化，W4 同款，避免全库稀疏失真）
            cand_vec = _minmax({k: vec_scores.get(k, 0.0) for k in union if k in vec_scores})
            cand_bm25 = _minmax({k: bm25_scores.get(k, 0.0) for k in union if k in bm25_scores})

        fused: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        for cid in union:
            v = cand_vec.get(cid, 0.0)
            b = cand_bm25.get(cid, 0.0)
            fused[cid] = self.alpha * v + (1.0 - self.alpha) * b
            sources[cid] = set()
            if cid in vec_scores:
                sources[cid].add("vec")
            if cid in bm25_scores:
                sources[cid].add("bm25")

        # 分数降序 + chunk_id 升序（确定性 tie-breaker，避免 set 哈希顺序影响复现）
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        return [
            {
                "chunk_id": cid,
                "fused_score": round(sc, 6),
                "source": "both" if len(sources[cid]) == 2 else next(iter(sources[cid])),
            }
            for cid, sc in ordered
        ]

    def retrieve(
        self, query: str, top_k: int = 5, topic: str | None = None, tenant_id: str | None = None
    ) -> list[dict]:
        """混合检索入口。有 reranker 时：融合 Top-20 → rerank → top_k。

        ★ W28 Day4（多租户分片 C4/C5）：`tenant_id` 非空时——向量路先经
        `sharding.collection_for()` 路由到租户分片 collection + payload filter（双保险），
        BM25 路按租户过滤再打分（堵 C5 漏洞）；`tenant_id` 为空 = 公共语料检索，
        行为与分片前完全一致（调用方无感知）。

        ★ W26 Day2 故障演练修复（杀 Qdrant 不雪崩）：向量路（Qdrant）异常 → 捕获 →
        降级 **BM25-only**，结果带 `degraded=True` 标记进响应/日志（召回降级可观测）；
        恢复后（下次请求 store.query 成功）自动回混合检索，无需重启。
        """
        qv = self.embedder.embed_query(query)
        # 分片路由：off → base collection（现状）；on → 租户分片（双保险的 collection 层）
        coll = sharding.collection_for(tenant_id) if tenant_id else self.store.collection
        try:
            # ★ W26 Day2 演练优化：降级链要"快速失败"——retries=0 试一次，
            #   Qdrant 挂时立即转 BM25-only（否则 30s 级重试拖垮响应）
            vec_hits = self.store.query(
                qv.tolist(),
                top_k=VEC_CANDIDATES,
                topic=topic,
                tenant_id=tenant_id,
                retries=0,
                collection=coll,
            )
            vec_ok = True
        except Exception as e:  # noqa: BLE001  # Qdrant 挂 → BM25-only 降级
            print(
                f"[hybrid] Qdrant 向量路不可用（{type(e).__name__}: {str(e)[:60]}）"
                f"→ 降级 BM25-only（degraded 标记进响应/日志）"
            )
            vec_hits = []
            vec_ok = False
        vec_ranks = {h["chunk_id"]: i for i, h in enumerate(vec_hits)}
        bm25_hits = self.bm25.search(query, top_k=BM25_CANDIDATES, tenant_id=tenant_id)
        bm25_ranks = {h["chunk_id"]: i for i, h in enumerate(bm25_hits)}
        if not vec_ok:
            # 降级路径：只用 BM25 结果（绕过融合），每条带 degraded 标记
            bm25_ordered = sorted(bm25_hits, key=lambda h: -h["score"])[:top_k]
            out = []
            for h in bm25_ordered:
                meta = self._chunk_meta(h["chunk_id"])
                meta["score"] = round(float(h["score"]), 4)
                meta["source"] = "bm25-degraded"
                meta["degraded"] = True
                out.append(meta)
            return out

        cand_k = self.top_candidates if self.reranker else top_k
        if self.fusion == "weighted":
            # weighted 需要分数（相似度/BM25 score）而非排名——排名会被当分数用反
            vec_scores = {h["chunk_id"]: float(h["score"]) for h in vec_hits}
            bm25_scores = {h["chunk_id"]: float(h["score"]) for h in bm25_hits}
            fused = self._weighted_fuse(vec_scores, bm25_scores, top_k=cand_k)
        else:
            fused = self._rrf_fuse(vec_ranks, bm25_ranks, top_k=cand_k)

        # 补全 chunk 元数据（重排器需要 text/doc_id；结果也统一带 source 标签）
        enriched = []
        for f in fused:
            meta = self._chunk_meta(f["chunk_id"])
            meta["fused_score"] = f["fused_score"]
            meta["source"] = f["source"]
            enriched.append(meta)

        if self.reranker is not None:
            enriched = self.reranker.rerank(query, enriched, top_k=top_k)

        out = []
        for meta in enriched:
            meta["score"] = meta.get("fused_score", meta.get("score", 0.0))
            out.append(meta)
        return out

    def retrieve_top_docs(self, query: str, top_k: int = 5) -> list[str]:
        hits = self.retrieve(query, top_k=top_k)
        seen, docs = set(), []
        for h in hits:
            if h["doc_id"] not in seen:
                seen.add(h["doc_id"])
                docs.append(h["doc_id"])
        return docs
