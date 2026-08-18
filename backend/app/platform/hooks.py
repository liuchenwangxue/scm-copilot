"""★ 工具调用钩子（W25 Day6）——learn-claude-code s04 机制的实物落点。

设计（对照手册 Day6 上午 + s04 教学"挂在循环上，不写进循环里"）：
- 工具执行循环（ops `execute_node`）只调用 `trigger_hooks("PreToolUse"/"PostToolUse", ctx)`，
  扩展逻辑全在 hook 回调里——审计/参数校验/缓存失效不侵入执行链。
- 事件契约（对齐 s04 教学版 + CC 的 HookResult 语义）：
  · PreToolUse：工具执行前 → 参数校验 + 高危标记 + 审计埋点（before 状态）。
    首个返回非 None 的回调返回**阻断消息**（停止本次工具执行，与 s04 语义一致）。
  · PostToolUse：工具执行后 → 结果审计（after 状态 + 耗时）+ 语义缓存失效标记
    （写类工具触发）。

内置三用途（面试题：s04 的 Pre/PostToolUse 在平台里怎么落地）：
1. **审计埋点**：`tool_pre_use` / `tool_post_use` 落审计（含风险等级/审批要求/耗时/结果）
2. **参数校验**：依据 ToolSpec.parameters_schema 的必填参数提前拦截坏参数
3. **缓存失效**：写类工具（update_order/cancel_order）成功后失效对应查询缓存

设计原则（手册坑：钩子抛错别让工具调用失败）：
- 钩子是横切关注点：回调 try/except 记日志放行，异常不影响工具执行与结果
- 回调顺序 = 注册顺序；PreToolUse 取首个非 None 为阻断消息
- spec 惰性获取（函数内 import），platform 层不强依赖 ops 域实现（模块可独立导入）

ADR 修订记录（对齐《04》ADR 修订纪律）：钩子故障放行写入 w25_report；
若未来要"钩子阻断业务"，需显式配置 allow_deny 层（s04 的 deny/ask 不变式），本版不做。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("scm.platform.hooks")

# 写类工具（PostToolUse 触发语义缓存失效；与 registry 的 risk_level="high" 对齐）
WRITE_TOOLS = ("update_order", "cancel_order")

# 事件名（对齐 s04 四事件：UserPromptSubmit / PreToolUse / PostToolUse / Stop；
# 本平台先落地工具相关两个，其余事件随业务扩展）
EVENT_PRE = "PreToolUse"
EVENT_POST = "PostToolUse"


@dataclass
class ToolUseContext:
    """一次工具调用的钩子上下文（s04 的 block 的等价物，含结果/耗时）。"""

    tool_name: str
    params: dict
    spec: object | None = None                  # ToolSpec（惰性填充）
    session_id: str = ""
    actor: str = ""
    result: object | None = None                 # ToolResult（PostToolUse 填充）
    duration_ms: float = 0.0
    trace_id: str = ""
    meta: dict = field(default_factory=dict)


# ==================== 钩子注册表（s04 教学核心） ====================

HOOKS: dict[str, list[Callable[[ToolUseContext], str | None]]] = {
    EVENT_PRE: [],
    EVENT_POST: [],
}


def register_hook(event: str, callback: Callable[[ToolUseContext], str | None]) -> None:
    """注册一个事件钩子（幂等：同回调不重复注册）。"""
    if event not in HOOKS:
        raise KeyError(f"unknown hook event: {event}")
    if callback not in HOOKS[event]:
        HOOKS[event].append(callback)


def trigger_hooks(event: str, ctx: ToolUseContext) -> str | None:
    """触发某事件的所有钩子。

    PreToolUse：返回首个非 None 回调的**阻断消息**（非 None 即"停"）。
    PostToolUse：返回值忽略（审计/缓存失效等副作用在回调内完成）。
    钩子抛错 → 记日志放行（横切关注点故障不影响工具调用）。
    """
    if event not in HOOKS:
        raise KeyError(f"unknown hook event: {event}")
    for cb in list(HOOKS[event]):
        try:
            r = cb(ctx)
        except Exception:  # noqa: BLE001  # 钩子故障放行（手册坑 → ADR 修订记录）
            logger.exception("hook %s failed (event=%s, tool=%s)", getattr(cb, "__name__", cb), event, ctx.tool_name)
            continue
        if event == EVENT_PRE and r is not None:
            return str(r)
    return None


def reset_hooks() -> None:
    """清空所有钩子（测试隔离用）。"""
    HOOKS[EVENT_PRE].clear()
    HOOKS[EVENT_POST].clear()


# ==================== 审计（s04 log_hook 的实物版） ====================

# 模块级默认审计器（与 ops graph.py 同源，写同一审计文件；可被 set_audit_logger 替换）
_audit_logger = None


def set_audit_logger(logger_obj) -> None:
    """注入审计器（测试传 tmp_path 的 AuditLogger；部署用默认 ops AuditLogger）。"""
    global _audit_logger
    _audit_logger = logger_obj


def _get_audit():
    global _audit_logger
    if _audit_logger is None:
        from app.domains.ops import config as ops_config
        from app.domains.ops.security.audit import AuditLogger

        _audit_logger = AuditLogger(ops_config.AUDIT_LOG)
    return _audit_logger


def _spec_of(ctx: ToolUseContext):
    """惰性取 ToolSpec（s04 中从注册表查工具契约）。"""
    if ctx.spec is not None:
        return ctx.spec
    try:
        from app.domains.ops.agent.tools.registry import registry

        ctx.spec = registry.get(ctx.tool_name)
    except Exception:  # noqa: BLE001  # spec 缺失不阻塞钩子（降级为无契约调用）
        ctx.spec = None
    return ctx.spec


def audit_pre_hook(ctx: ToolUseContext) -> None:
    """PreToolUse 审计埋点（before 状态）：记录工具名/参数摘要/风险等级/审批要求。

    不记敏感内容（参数只取 order_id 等业务 ID，金额/日期仅标记有无）。
    """
    spec = _spec_of(ctx)
    risk = getattr(spec, "risk_level", "unknown") if spec else "unknown"
    req_approval = bool(getattr(spec, "requires_approval", False)) if spec else False
    params_snapshot = {
        k: ("" if k in ("amount", "delivery_date", "reason") else str(v)[:64])
        for k, v in (ctx.params or {}).items()
    }
    _get_audit().log(
        "tool_pre_use",
        tool=ctx.tool_name,
        risk_level=risk,
        requires_approval=req_approval,
        params=params_snapshot,
        session_id=ctx.session_id,
        actor=ctx.actor or "",
        trace_id=ctx.trace_id or "",
    )


def audit_post_hook(ctx: ToolUseContext) -> None:
    """PostToolUse 结果审计（after 状态 + 耗时）：成功/失败/降级/熔断状态。"""
    result = ctx.result
    success = bool(getattr(result, "success", False)) if result is not None else False
    error = str(getattr(result, "error", "") or "")[:120]
    degraded = bool(getattr(result, "degraded", False)) if result is not None else False
    circuit = getattr(result, "circuit_state", "N/A") if result is not None else "N/A"
    _get_audit().log(
        "tool_post_use",
        tool=ctx.tool_name,
        success=success,
        duration_ms=round(ctx.duration_ms, 1),
        degraded=degraded,
        circuit_state=circuit,
        error=error or "",
        session_id=ctx.session_id,
        actor=ctx.actor or "",
        trace_id=ctx.trace_id or "",
    )


# ==================== 参数校验（s04 permission_hook 的实物版：契约校验） ====================

def validate_params_hook(ctx: ToolUseContext) -> str | None:
    """PreToolUse 参数校验：依据 ToolSpec.parameters_schema 的 required 提前拦截。

    返回非 None → 阻断消息（stop 语义）；参数合法返回 None 放行。
    """
    spec = _spec_of(ctx)
    if spec is None:
        return None
    schema = getattr(spec, "parameters_schema", None) or {}
    required = schema.get("required", []) or []
    missing = [k for k in required if (ctx.params or {}).get(k) in (None, "", [])]
    if missing:
        return f"{ctx.tool_name}: 缺少必填参数 {', '.join(missing)}"
    return None


# ==================== 语义缓存失效（写类工具触发，PostToolUse） ====================

def invalidate_order_query_cache(order_id: str, redis_client=None) -> bool:
    """失效某订单的 query_order 查询缓存（Redis 前缀 `cache:`）。

    独立函数便于单测（注入 fake redis）；返回是否实际删除（Redis 不可用 → False）。
    """
    from app.shared.reliability.cache import QueryCache
    from app.shared.reliability.redis_client import get_redis_client

    key = QueryCache.build_key("query_order", order_id)
    rc = redis_client or get_redis_client()
    try:
        return bool(rc.delete(f"cache:{key}"))
    except Exception:  # noqa: BLE001  # 缓存失效失败静默（fail-open）
        logger.exception("hook invalidate cache failed: %s", order_id)
        return False


def invalidate_cache_hook(ctx: ToolUseContext) -> None:
    """PostToolUse 缓存失效：写类工具成功后失效同源读缓存。

    update_order/cancel_order 命中订单后，query_order 的缓存（Redis 前缀 `cache:`）
    立即失效——"写后读即新"（W21 查询缓存 TTL 60s 的最终一致性收窄到写后即时）。
    只读工具（query_order/generate_report）不失效；写失败不失效（避免删掉有价值缓存）。
    """
    if ctx.tool_name not in WRITE_TOOLS:
        return
    result = ctx.result
    if result is None or not getattr(result, "success", False):
        return
    order_id = (ctx.params or {}).get("order_id", "")
    if not order_id:
        return
    ok = invalidate_order_query_cache(order_id)
    ctx.meta["cache_invalidated"] = ok
    logger.info("hook invalidate cache: %s order=%s invalidated=%s", ctx.tool_name, order_id, ok)


# ==================== before/after diff 辅助（approval_gate 复用钩子能力） ====================

def make_after_state(tool_name: str, params: dict, before: dict) -> dict:
    """构造目标状态（update_order/cancel_order 的 after）——approval_gate 复用。

    s04 扩展点语义：钩子不仅旁路观测，还提供"变更前后状态"的契约化计算，
    approval_gate 用它与 approval.py 的 build_diff 组成审批表单（before/after diff）。
    """
    after = dict(before)
    if tool_name == "update_order":
        if params.get("amount") is not None:
            after["amount"] = float(params["amount"])
        if params.get("delivery_date"):
            after["delivery_date"] = params["delivery_date"]
    elif tool_name == "cancel_order":
        after = {**before, "status": "closed"}
    return after


# ==================== 注册内置钩子（幂等：重复导入不重复注册） ====================

register_hook(EVENT_PRE, audit_pre_hook)
register_hook(EVENT_PRE, validate_params_hook)
register_hook(EVENT_POST, audit_post_hook)
register_hook(EVENT_POST, invalidate_cache_hook)


# ==================== 便捷入口 ====================

def run_pre_hooks(ctx: ToolUseContext) -> str | None:
    """工具执行前调用：返回阻断消息（None = 放行）。"""
    return trigger_hooks(EVENT_PRE, ctx)


def run_post_hooks(ctx: ToolUseContext) -> None:
    """工具执行后调用：审计/缓存失效副作用。"""
    trigger_hooks(EVENT_POST, ctx)
