"""W25 Day6 工具钩子单测：注册表 / 审计埋点 / 参数校验 / 缓存失效 / diff 复用。

覆盖手册 Day6 上午：
- PreToolUse：参数校验（契约 required）+ 审计埋点（before 状态 + 高危标记）
- PostToolUse：结果审计（after + 耗时）+ 语义缓存失效（写类工具触发）
- 钩子抛错放行（手册坑：横切关注点故障不影响工具调用）
- approval_gate 复用 make_after_state（单一来源防漂移）

纯逻辑可测（审计用 tmp_path 的 AuditLogger；缓存失效用 FakeRedis），CI 可跑。
"""

import time

import pytest

from app.domains.ops.agent.tools.registry import ToolResult, ToolSpec
from app.domains.ops.security.audit import AuditLogger
from app.platform import hooks
from app.platform.hooks import (
    EVENT_POST,
    EVENT_PRE,
    ToolUseContext,
    audit_post_hook,
    audit_pre_hook,
    invalidate_cache_hook,
    make_after_state,
    reset_hooks,
    run_post_hooks,
    run_pre_hooks,
    validate_params_hook,
)


@pytest.fixture(autouse=True)
def _isolate_hooks(tmp_path):
    """每个测试：重置注册表 + 用 tmp_path 审计器（不写真实 audit.log）。"""
    reset_hooks()
    # 重新注册内置钩子（reset 后需恢复；与 hooks.py 模块加载行为一致）
    hooks.register_hook(EVENT_PRE, audit_pre_hook)
    hooks.register_hook(EVENT_PRE, validate_params_hook)
    hooks.register_hook(EVENT_POST, audit_post_hook)
    hooks.register_hook(EVENT_POST, invalidate_cache_hook)
    hooks.set_audit_logger(AuditLogger(tmp_path / "audit.log"))
    yield
    reset_hooks()
    hooks.set_audit_logger(None)


# ==================== 注册表 / 触发语义 ====================

def test_register_and_trigger_order():
    """注册表顺序 = 触发顺序；PreToolUse 首个非 None 即阻断。"""
    reset_hooks()
    seen: list[str] = []

    def h1(ctx):
        seen.append("h1")
        return "blocked-by-h1"

    def h2(ctx):
        seen.append("h2")
        return None

    hooks.register_hook(EVENT_PRE, h1)
    hooks.register_hook(EVENT_PRE, h2)
    ctx = ToolUseContext(tool_name="query_order", params={})
    result = run_pre_hooks(ctx)
    assert result == "blocked-by-h1"
    assert seen == ["h1"]  # 第二个钩子未执行（首个阻断即停，s04 语义）


def test_trigger_returns_none_when_all_pass():
    reset_hooks()

    def h1(ctx):
        return None

    hooks.register_hook(EVENT_PRE, h1)
    assert run_pre_hooks(ToolUseContext(tool_name="x", params={})) is None


def test_hook_exception_is_swallowed():
    """手册坑：钩子抛错记日志放行，不让工具调用失败（横切关注点故障隔离）。"""
    reset_hooks()

    def bad(ctx):
        raise RuntimeError("hook bug")

    def good(ctx):
        return None

    hooks.register_hook(EVENT_PRE, bad)
    hooks.register_hook(EVENT_PRE, good)
    ctx = ToolUseContext(tool_name="x", params={})
    assert run_pre_hooks(ctx) is None  # bad 被吞掉，good 正常放行


def test_register_duplicate_ignored():
    reset_hooks()

    def h(ctx):
        return None

    hooks.register_hook(EVENT_PRE, h)
    hooks.register_hook(EVENT_PRE, h)
    assert len(hooks.HOOKS[EVENT_PRE]) == 1


def test_unknown_event_raises():
    with pytest.raises(KeyError):
        hooks.register_hook("NotAnEvent", lambda ctx: None)
    with pytest.raises(KeyError):
        hooks.trigger_hooks("NotAnEvent", ToolUseContext(tool_name="x", params={}))


# ==================== 审计埋点 ====================

def test_audit_pre_hook_records_before_state(tmp_path):
    """PreToolUse 审计：工具名/风险等级/审批要求/参数摘要（不记敏感值）。"""
    audit = AuditLogger(tmp_path / "audit.log")
    hooks.set_audit_logger(audit)
    spec = ToolSpec(
        name="update_order",
        description="修改订单",
        parameters_schema={},
        returns_schema={},
        risk_level="high",
        requires_approval=True,
        endpoint="PATCH /orders/{id}",
    )
    ctx = ToolUseContext(
        tool_name="update_order",
        params={"order_id": "PO-0001", "amount": 9000.0, "delivery_date": "2026-09-01"},
        session_id="s1",
        spec=spec,
    )
    audit_pre_hook(ctx)
    rec = audit.filter("tool_pre_use")[0]
    assert rec["tool"] == "update_order"
    assert rec["risk_level"] == "high"
    assert rec["requires_approval"] is True
    # 敏感值不记（amount/delivery_date/reason 置空）
    assert rec["params"] == {"order_id": "PO-0001", "amount": "", "delivery_date": ""}


def test_audit_post_hook_records_after_state_and_duration(tmp_path):
    """PostToolUse 审计：成功/耗时/降级/熔断状态。"""
    audit = AuditLogger(tmp_path / "audit.log")
    hooks.set_audit_logger(audit)
    ctx = ToolUseContext(tool_name="query_order", params={"order_id": "PO-0001"}, session_id="s1")
    ctx.result = ToolResult(success=True, data={"order_id": "PO-0001"}, degraded=False,
                            circuit_state="CLOSED")
    ctx.duration_ms = 123.4
    audit_post_hook(ctx)
    rec = audit.filter("tool_post_use")[0]
    assert rec["success"] is True
    assert rec["duration_ms"] == 123.4
    assert rec["circuit_state"] == "CLOSED"
    assert rec["error"] == ""


# ==================== 参数校验 ====================

def test_validate_params_hook_blocks_missing_required():
    """依据 ToolSpec.parameters_schema.required 拦截缺参。"""
    spec = ToolSpec(
        name="update_order",
        description="修改订单",
        parameters_schema={"type": "object", "properties": {"order_id": {"type": "string"}},
                           "required": ["order_id"]},
        returns_schema={},
        risk_level="high",
        requires_approval=True,
        endpoint="PATCH /orders/{id}",
    )
    ctx = ToolUseContext(tool_name="update_order", params={}, spec=spec)
    blocked = validate_params_hook(ctx)
    assert blocked is not None and "order_id" in blocked
    # 参数齐全放行
    ctx.params = {"order_id": "PO-0001"}
    assert validate_params_hook(ctx) is None


def test_validate_params_hook_unknown_tool_passes():
    """无契约（spec 不存在）→ 放行（钩子故障/缺契约不阻断）。"""
    ctx = ToolUseContext(tool_name="ghost_tool", params={}, spec=None)
    assert validate_params_hook(ctx) is None


# ==================== 语义缓存失效 ====================

class FakeRedis:
    """最小 Redis 假件：get/set/delete（QueryCache 用）。"""

    def __init__(self):
        self._store: dict[str, str] = {}
        self.available = True

    def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        return self._store.pop(key, None) is not None


def test_invalidate_cache_hook_after_write_success():
    """写类工具成功后，同源 query_order 缓存立即失效（写后读即新）。"""
    from app.platform.hooks import invalidate_order_query_cache
    from app.shared.reliability.cache import QueryCache

    fake = FakeRedis()
    # 预置一条 query_order 缓存
    key = QueryCache.build_key("query_order", "PO-0001")
    fake.set(f"cache:{key}", '{"order_id":"PO-0001"}')
    assert fake.get(f"cache:{key}") is not None

    assert invalidate_order_query_cache("PO-0001", redis_client=fake) is True
    assert fake.get(f"cache:{key}") is None


def test_invalidate_order_query_cache_fail_open():
    """Redis 不可用 → 失效失败返回 False（fail-open 静默）。"""
    from app.platform.hooks import invalidate_order_query_cache

    class DownRedis:
        def delete(self, key):
            raise ConnectionError("redis down")

    assert invalidate_order_query_cache("PO-0001", redis_client=DownRedis()) is False


def test_invalidate_cache_hook_skip_read_tools():
    """只读工具不失效缓存（query_order/generate_report 是查询缓存受益者）。"""
    ctx = ToolUseContext(tool_name="query_order", params={"order_id": "PO-0001"}, spec=object())
    ctx.result = ToolResult(success=True, data={})
    invalidate_cache_hook(ctx)
    assert ctx.meta.get("cache_invalidated") is None  # 未走到失效分支


def test_invalidate_cache_hook_skip_failed_write():
    """写类工具失败 → 不失效（避免失效有价值缓存）。"""
    ctx = ToolUseContext(tool_name="update_order", params={"order_id": "PO-0001"}, spec=object())
    ctx.result = ToolResult(success=False, error="biz error")
    invalidate_cache_hook(ctx)
    assert ctx.meta.get("cache_invalidated") is None


# ==================== before/after diff 复用（approval_gate 单一来源） ====================

def test_make_after_state_update_order():
    before = {"order_id": "PO-0001", "status": "open", "amount": 8000.0,
              "delivery_date": "2026-08-30", "supplier_id": "S1"}
    after = make_after_state("update_order", {"order_id": "PO-0001", "amount": 9000.0},
                             before)
    assert after["amount"] == 9000.0
    assert after["delivery_date"] == "2026-08-30"  # 未传不变
    assert after["status"] == "open"


def test_make_after_state_cancel_order():
    before = {"order_id": "PO-0002", "status": "open", "amount": 5000.0}
    after = make_after_state("cancel_order", {"order_id": "PO-0002", "reason": "取消"}, before)
    assert after["status"] == "closed"
    assert after["amount"] == 5000.0


# ==================== 全链路：run_pre/run_post ====================

def test_full_pipeline_blocked_write(tmp_path):
    """写类工具缺必填参数 → PreToolUse 阻断（无工具执行、有审计）。"""
    audit = AuditLogger(tmp_path / "audit.log")
    hooks.set_audit_logger(audit)
    spec = ToolSpec(
        name="cancel_order",
        description="取消订单",
        parameters_schema={"type": "object",
                           "properties": {"order_id": {"type": "string"},
                                          "reason": {"type": "string"}},
                           "required": ["order_id", "reason"]},
        returns_schema={},
        risk_level="high",
        requires_approval=True,
        endpoint="POST /orders/{id}/cancel",
    )
    ctx = ToolUseContext(tool_name="cancel_order", params={"order_id": "PO-0001"},
                         spec=spec, session_id="s1")
    blocked = run_pre_hooks(ctx)
    assert blocked is not None and "reason" in blocked
    # before 审计已落（PreToolUse 审计埋点在阻断前执行）
    recs = audit.filter("tool_pre_use")
    assert recs and recs[0]["tool"] == "cancel_order"
    assert recs[0]["requires_approval"] is True


def test_full_pipeline_success(tmp_path):
    """合法工具调用：Pre 放行 + Post 审计（含耗时）。"""
    audit = AuditLogger(tmp_path / "audit.log")
    hooks.set_audit_logger(audit)
    spec = ToolSpec(
        name="query_order",
        description="查订单",
        parameters_schema={"type": "object",
                           "properties": {"order_id": {"type": "string"}},
                           "required": ["order_id"]},
        returns_schema={},
        risk_level="low",
        requires_approval=False,
        endpoint="GET /orders/{id}",
    )
    ctx = ToolUseContext(tool_name="query_order", params={"order_id": "PO-0001"},
                         spec=spec, session_id="s1")
    assert run_pre_hooks(ctx) is None
    time.sleep(0.01)
    ctx.result = ToolResult(success=True, data={}, circuit_state="CLOSED")
    ctx.duration_ms = 15.0
    run_post_hooks(ctx)
    events = [r["event"] for r in audit.read_all()]
    assert events == ["tool_pre_use", "tool_post_use"]
