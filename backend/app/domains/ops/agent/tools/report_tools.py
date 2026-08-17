"""报表工具（W19 Day3）：generate_report（库存/对账）

只读中危操作（无审批）：实时接口 → 最近一次报表快照 → 兜底。
报表快照按 report_type 缓存最近一次成功结果（可能过期，标记 source=cache）。
"""
from app.domains.ops.agent.tools.registry import (
    BaseTool,
    BizApiError,
    ToolResult,
    ToolSpec,
    registry,
)
from app.shared.reliability.cache import QueryCache


class SnapshotMiss(Exception):
    pass


class ReportTools(BaseTool):
    name = "report"

    REPORT_TYPES = ("inventory", "reconciliation")

    def __init__(self, base_url: str, **kwargs):
        super().__init__(base_url, **kwargs)
        self._register_specs()
        # ★ W21 Day3 查询缓存（Redis 优先 + 内存兜底）
        self.query_cache = QueryCache()

    @staticmethod
    def _register_specs():
        if registry.get("generate_report"):
            return
        registry.register(ToolSpec(
            name="generate_report",
            description="生成库存报表或对账报表（只读，按日期范围汇总）",
            parameters_schema={"type": "object", "properties": {
                "report_type": {"type": "string", "enum": ["inventory", "reconciliation"]},
                "from": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "to": {"type": "string", "description": "结束日期 YYYY-MM-DD"}},
                "required": ["report_type"]},
            returns_schema={"report_type": "str", "summary": "dict", "rows": "list"},
            risk_level="medium", requires_approval=False,
            endpoint="GET /api/v1/reports/{type}?from=&to="))

    def generate_report(self, report_type: str, from_date: str | None = None,
                        to_date: str | None = None) -> ToolResult:
        if report_type not in self.REPORT_TYPES:
            return ToolResult(success=False, error=f"不支持的报表类型: {report_type}",
                              circuit_state=self.breaker.state, meta={"biz_error": True})
        params = {"from": from_date, "to": to_date} if (from_date or to_date) else None

        # ★ W21 Day3 缓存命中（key=报表类型+日期参数）：TTL 内二次查询不落库/不调 LLM
        cached, hit = self.query_cache.get("generate_report", report_type,
                                           from_date or "", to_date or "")
        if hit and cached is not None:
            self._snapshot_set(report_type, cached)
            return ToolResult(success=True, data=cached,
                              meta={"cache_hit": True, "source": "cache"})

        result = self._invoke(
            primary=lambda: self._report_http(report_type, params),
            backups=(lambda: self._snapshot_or_raise(report_type),),
            fallback=lambda: self._unavailable("报表生成"),
            breaker_name="generate_report",
        )
        # 主源成功 → 刷新报表快照 + 查询缓存
        if result.success and result.level == 0 and result.data:
            self._snapshot_set(report_type, result.data)
            self.query_cache.set(result.data, "generate_report", report_type,
                                 from_date or "", to_date or "")
        return result

    # ---- HTTP ----
    def _report_http(self, report_type: str, params: dict | None) -> dict:
        r = self.client.get(f"/api/v1/reports/{report_type}", params=params)
        if r.status_code >= 400:
            err = r.json().get("error", {})
            raise BizApiError(r.status_code, err.get("message", r.text[:120]), err.get("detail"))
        return r.json()

    def _snapshot_or_raise(self, key: str) -> dict:
        v = self._snapshot_get(key)
        if v is None:
            raise SnapshotMiss(f"snapshot miss: {key}")
        return v
