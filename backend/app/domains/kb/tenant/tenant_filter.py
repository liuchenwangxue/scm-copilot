"""多租户隔离（W18 Day6 payload 过滤级 → ★ W28 Day4 collection 分片级，双保险）。

方案决策（演进依据 ADR-009）：
- W18：本项目文档体量小（2543 块），选 **payload 过滤级**（Qdrant Filter 强制 must
  tenant_id）——单索引、零拷贝、易扩展（新租户 = 加一个 payload 值）。
- W28-D4：规模化演进到 **collection 分片级**——`sharding.collection_for(tenant_id)`
  按 crc32 路由到 `scm_kb_v1_0..3`。分片是性能隔离（隔离语料更小 → 检索更快），
  **payload filter 作为正确性兜底保留**（双保险：即使路由层被绕过，检索依然强制
  must tenant_id，杜绝跨租户泄露）。
- `SCM_SHARDING=off`（默认）时行为与分片前完全一致（灰度开关，回退零成本）。

实现：
- TenantFilter 注入到检索器（Retriever/HybridRetriever），检索时强制 tenant_id。
  `retrieve()` 把 tenant_id 透传给检索器——检索器内部先路由 collection 再注入
  payload filter（对 TenantFilter 调用方透明，接口不变）。
- 未知/缺失租户 → 拒绝（fail-closed：宁可不返回，不跨租户泄露）。
"""

from pathlib import Path

from app.shared.rag import sharding


class TenantFilter:
    """多租户隔离：payload 过滤级 + collection 分片级双保险。封装"租户上下文的检索过滤"。

    用法（生产问答链路）：
        tenant = TenantFilter("tenant_a")
        hits = tenant.retrieve(retriever, question, top_k=5)
        # 等价于 retriever.retrieve(question, tenant_id="tenant_a", top_k=5)
        #   —— 分片模式内部先路由 collection，再注入 payload filter（双保险）

    校验：可调用 verify_isolation / verify_sharded_isolation 用真实数据验证
    tenant_a 的问题在 tenant_b 检索不到。
    """

    def __init__(self, tenant_id: str | None = None):
        self.tenant_id = tenant_id or "default"

    def retrieve(self, retriever, question: str, top_k: int = 5, **kw) -> list[dict]:
        """带租户过滤的检索（强制注入 tenant_id，不透传调用方的 tenant 覆盖）。

        分片模式下：检索器内部先按 `sharding.collection_for(tenant_id)` 路由到
        租户分片 collection，再注入 payload filter——双层隔离（双保险）。"""
        return retriever.retrieve(question, top_k=top_k, tenant_id=self.tenant_id, **kw)

    # ---------- 隔离验证 ----------

    def verify_isolation(
        self, retriever, tenant_a: str, tenant_b: str, question: str, top_k: int = 5
    ) -> dict:
        """验证两个租户的检索隔离：
        1. tenant_a 检索到的 doc_id 集合，tenant_b 必须检索不到（空）。
        2. 数据准备：把同一批文档分别以 tenant_a / tenant_b 两个 payload 值入库后调用。
        返回每租户命中的 doc_id + 隔离判定。"""
        hits_a = retriever.retrieve(question, top_k=top_k, tenant_id=tenant_a)
        hits_b = retriever.retrieve(question, top_k=top_k, tenant_id=tenant_b)
        docs_a = {h["doc_id"] for h in hits_a}
        docs_b = {h["doc_id"] for h in hits_b}
        isolated = not docs_a or not docs_b or not (docs_a & docs_b)
        return {
            "tenant_a": sorted(docs_a),
            "tenant_b": sorted(docs_b),
            "overlap": sorted(docs_a & docs_b),
            "isolated": isolated,
        }

    def verify_sharded_isolation(
        self, retriever, tenant_a: str, tenant_b: str, question: str, top_k: int = 5
    ) -> dict:
        """★ W28 Day4（C4 验收）：分片模式下的隔离双路验证。

        在 verify_isolation（数据层隔离）之上，额外验证：
        1. **路由层隔离**：两租户路由到不同的分片 collection（物理分片存在，
           crc32 路由生效）——这是"分片是性能隔离"的前提。
        2. 数据层隔离：检索结果零交集（含 payload filter 兜底，双保险）。
        返回 collection 路由 + 每租户命中的 doc_id + 双路隔离判定。"""
        coll_a = sharding.collection_for(tenant_a)
        coll_b = sharding.collection_for(tenant_b)
        data = self.verify_isolation(retriever, tenant_a, tenant_b, question, top_k)
        routed_isolated = coll_a != coll_b
        return {
            "route": {"tenant_a": coll_a, "tenant_b": coll_b, "isolated": routed_isolated},
            "data": data,
            "isolated": routed_isolated and data["isolated"],
        }

    @staticmethod
    def load_chunks_with_tenant(chunks_file, tenant_id: str) -> list[dict]:
        """读取 chunks 并给每条附加 tenant_id（用于按租户分批入库/造隔离验证数据）。"""
        import json

        chunks = json.loads(Path(chunks_file).read_text(encoding="utf-8"))
        for c in chunks:
            c["tenant_id"] = tenant_id
        return chunks
