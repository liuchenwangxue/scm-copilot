"""订单工具（W19 Day3）：query_order / update_order / cancel_order

每个工具 = 熔断器（独立实例，互不影响）+ 降级链（主源重试 → 备用只读快照 → 兜底）+ 幂等键（写操作）

降级链分层（Day3 验收核心）：
- 读操作 query_order：实时接口 → 只读快照（上次成功结果）→ "服务暂不可用"
- 写操作 update/cancel：实时接口 →（无快照，写操作不能降级到旧数据）→ "服务暂不可用，请稍后"
- 业务错误（400/404/409）不重试不降级，如实透传（如"订单已关闭，无法修改"）
"""
import httpx

from app.domains.ops.agent.tools.registry import (
    BaseTool,
    BizApiError,
    ToolResult,
    ToolSpec,
    registry,
)
from app.shared.reliability.cache import QueryCache


class SnapshotMiss(Exception):
    """备用快照未命中，触发下一级降级。"""


class OrderTools(BaseTool):
    name = "order"

    def __init__(self, base_url: str, use_cache: bool = True, **kwargs):
        super().__init__(base_url, **kwargs)
        self._register_specs()
        # ★ W21 Day3 查询缓存（Redis 优先 + 内存兜底）：TTL 内二次查询不落库
        # use_cache=False：熔断/降级链回归测试旁路缓存（缓存已由 day3 单独验证）
        self.query_cache = QueryCache() if use_cache else None

    # ---- 注册（幂等：多次实例化不重复注册）----
    @staticmethod
    def _register_specs():
        if registry.get("query_order"):
            return
        registry.register(ToolSpec(
            name="query_order",
            description="查询采购订单状态与明细（订单号/状态/金额/交期/供应商）",
            parameters_schema={"type": "object", "properties": {
                "order_id": {"type": "string", "description": "采购订单号，如 PO-0001"}},
                "required": ["order_id"]},
            returns_schema={"order_id": "str", "status": "str", "amount": "float",
                            "delivery_date": "str", "supplier_id": "str"},
            risk_level="low", requires_approval=False,
            endpoint="GET /api/v1/orders/{order_id}"))
        registry.register(ToolSpec(
            name="update_order",
            description="修改订单金额或交期（高危操作，需人工确认）",
            parameters_schema={"type": "object", "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number", "description": "新金额（>0）"},
                "delivery_date": {"type": "string", "description": "新交期 YYYY-MM-DD"}},
                "required": ["order_id"]},
            returns_schema="更新后的订单详情",
            risk_level="high", requires_approval=True,
            endpoint="PATCH /api/v1/orders/{order_id}"))
        registry.register(ToolSpec(
            name="cancel_order",
            description="取消订单（高危操作，需人工确认，需说明原因）",
            parameters_schema={"type": "object", "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string", "description": "取消原因（展示在审批表单）"}},
                "required": ["order_id", "reason"]},
            returns_schema="取消后的订单详情（status=closed）",
            risk_level="high", requires_approval=True,
            endpoint="POST /api/v1/orders/{order_id}/cancel"))

    # ================= 读操作 =================

    def query_order(self, order_id: str) -> ToolResult:
        """查订单：★缓存(TTL 60s) → 实时接口 → 只读快照 → 兜底。主源成功写缓存+快照。"""
        # ★ W21 Day3 缓存命中：TTL 内二次查询不落库（meta 打 cache_hit 标记）
        cached, hit = (self.query_cache.get("query_order", order_id)
                       if self.query_cache is not None else (None, False))
        if hit and cached is not None:
            self._snapshot_set(order_id, cached)  # 顺手刷新快照（缓存即最新）
            return ToolResult(success=True, data=cached,
                              meta={"cache_hit": True, "source": "cache"})
        result = self._invoke(
            primary=lambda: self._get_http(order_id),
            backups=(lambda: self._snapshot_or_raise(order_id),),
            fallback=lambda: self._unavailable("订单查询"),
            breaker_name="query_order",
        )
        # 主源成功 → 刷新只读快照 + 查询缓存
        if result.success and result.level == 0 and result.data:
            self._snapshot_set(order_id, result.data)
            if self.query_cache is not None:
                self.query_cache.set(result.data, "query_order", order_id)
        return result

    # ================= 写操作（高危，Day4 接审批） =================

    def update_order(self, order_id: str, amount=None, delivery_date=None,
                     idempotency_key: str | None = None) -> ToolResult:
        """改金额/交期。幂等键：Day3 由调用方传或自动生成（防重），Day4 换正式幂等模块。
        业务错误（409 状态冲突/404/400）由 _invoke 统一透传，不重试不降级。
        """
        key = idempotency_key or self.new_idempotency_key()
        return self._invoke(
            primary=lambda: self._patch_http(order_id, amount, delivery_date, key),
            fallback=lambda: self._unavailable("订单修改"),
            breaker_name="update_order",
        )

    def cancel_order(self, order_id: str, reason: str,
                     idempotency_key: str | None = None) -> ToolResult:
        """取消订单。同样：业务错误透传，瞬时故障重试，熔断降级。"""
        key = idempotency_key or self.new_idempotency_key()
        return self._invoke(
            primary=lambda: self._cancel_http(order_id, reason, key),
            fallback=lambda: self._unavailable("订单取消"),
            breaker_name="cancel_order",
        )

    # ================= HTTP 细节 =================

    def _get_http(self, order_id: str) -> dict:
        r = self.client.get(f"/api/v1/orders/{order_id}")
        return self._ensure_ok(r)

    def _patch_http(self, order_id: str, amount, delivery_date, idem_key: str) -> dict:
        body = {}
        if amount is not None:
            body["amount"] = amount
        if delivery_date is not None:
            body["delivery_date"] = delivery_date
        r = self.client.patch(f"/api/v1/orders/{order_id}", json=body,
                              headers={"Idempotency-Key": idem_key})
        return self._ensure_ok(r)

    def _cancel_http(self, order_id: str, reason: str, idem_key: str) -> dict:
        r = self.client.post(f"/api/v1/orders/{order_id}/cancel",
                             json={"reason": reason},
                             headers={"Idempotency-Key": idem_key})
        return self._ensure_ok(r)

    def _ensure_ok(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            raise BizApiError(r.status_code, err.get("message", r.text[:120]), err.get("detail"))
        return r.json()

    def _snapshot_or_raise(self, key: str) -> dict:
        v = self._snapshot_get(key)
        if v is None:
            raise SnapshotMiss(f"snapshot miss: {key}")
        return v
