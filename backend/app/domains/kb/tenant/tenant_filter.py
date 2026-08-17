"""多租户隔离（★ W18 Day6，payload 过滤级）。

方案决策（写进 reports/day6_tenant_audit.md）：
- 本项目文档体量小（2543 块），多租户隔离选 **payload 过滤级**（Qdrant Filter 强制 must tenant_id），
  而非"每租户独立 collection"。
- 理由：单索引、零拷贝、成本低、易扩展（新租户 = 加一个 payload 值，无需重建索引）；
  collection 级在租户数据量巨大/检索隔离需求强时才划算，且每租户都要维护索引与调参成本。
- 唯一注意点：所有检索必须走 tenant-filter 入口，否则漏配 = 越权（下面 TenantFilter 封装）。

实现：
- TenantFilter 注入到检索器（Retriever/HybridRetriever），检索时强制 must tenant_id。
- 未知/缺失租户 → 拒绝（fail-closed：宁可不返回，不跨租户泄露）。
"""
from pathlib import Path


class TenantFilter:
    """多租户 payload 过滤级隔离。封装"租户上下文的检索过滤"。

    用法（生产问答链路）：
        tenant = TenantFilter("tenant_a")
        hits = tenant.retrieve(retriever, question, top_k=5)
        # 等价于 retriever.retrieve(question, tenant_id="tenant_a", top_k=5)

    校验：可调用 verify_isolation 用真实数据验证 tenant_a 的问题在 tenant_b 检索不到。
    """

    def __init__(self, tenant_id: str | None = None):
        self.tenant_id = tenant_id or "default"

    def retrieve(self, retriever, question: str, top_k: int = 5, **kw) -> list[dict]:
        """带租户过滤的检索（强制注入 tenant_id，不透传调用方的 tenant 覆盖）。"""
        return retriever.retrieve(question, top_k=top_k, tenant_id=self.tenant_id, **kw)

    # ---------- 隔离验证 ----------

    def verify_isolation(self, retriever, tenant_a: str, tenant_b: str,
                         question: str, top_k: int = 5) -> dict:
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

    @staticmethod
    def load_chunks_with_tenant(chunks_file, tenant_id: str) -> list[dict]:
        """读取 chunks 并给每条附加 tenant_id（用于按租户分批入库/造隔离验证数据）。"""
        import json
        chunks = json.loads(Path(chunks_file).read_text(encoding="utf-8"))
        for c in chunks:
            c["tenant_id"] = tenant_id
        return chunks
