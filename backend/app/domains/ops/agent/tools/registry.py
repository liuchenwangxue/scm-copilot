"""工具注册表 + 危险等级声明 + 工具基类（W19 Day3）

- ToolSpec：工具契约（名称/schema/危险等级/审批/接口）——Day1 brief §3 的代码化
- ToolResult：工具统一返回结构（success/data/degraded/attempts/circuit_state）
  ——Day6 验收三指标（成功率/高危确认率/任务完成率）的统计基础
- ToolRegistry：注册表 + 高危清单查询
- BaseTool：熔断器 + 降级链 + 重试 + 只读快照的统一封装（每工具一个熔断器实例）

危险等级（Day1 brief §4）：
- low    : query_order（只读查询）
- high   : update_order / cancel_order（写操作，100% 人工确认）
- medium : generate_report（只读但数据量大）
"""
import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.shared.reliability.circuit_breaker import CircuitBreaker
from app.shared.reliability.retry_policy import degrade_chain, is_retryable_http

# ==================== 业务错误 ====================

class BizApiError(Exception):
    """业务接口错误：status_code 决定可重试性（429/5xx 瞬时，4xx 业务拒绝）。

    在降级链中：is_retryable_http 返回 False → 不重试不降级，直接透传；
    _invoke 捕获后转为 ToolResult(success=False) 如实告知用户（如"订单已关闭"）。
    """

    def __init__(self, status_code: int, message: str, detail=None):
        super().__init__(f"biz api {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.detail = detail


# ==================== 工具契约 ====================

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict
    returns_schema: dict
    risk_level: str            # low / medium / high
    requires_approval: bool    # 高危操作必须人工确认（Day4 HITL）
    endpoint: str              # 调用的业务接口（契约文档对应）


@dataclass
class ToolResult:
    success: bool
    data: dict | None = None
    error: str | None = None
    degraded: bool = False            # 是否走了降级（备用源/兜底）
    level: int = 0                    # 降级链级别（0=主源，1=第一备用，-1=熔断直接降级）
    attempts: int = 1                 # 总尝试次数（含重试，Day6 成功率"含重试"口径）
    circuit_state: str = "CLOSED"     # 工具熔断器当前状态（观测）
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转 dict（graph 状态 / SSE 事件用）。"""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "degraded": self.degraded,
            "level": self.level,
            "attempts": self.attempts,
            "circuit_state": self.circuit_state,
            "meta": self.meta,
        }


# ==================== 注册表 ====================

class ToolRegistry:
    """工具注册表：注册 + 查询 + 高危清单 + LLM function calling schema。"""

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs.keys())

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def high_risk(self) -> list[ToolSpec]:
        """高危清单：所有 requires_approval=True 的工具（Day4 approval_gate 依据）。"""
        return [s for s in self._specs.values() if s.requires_approval]

    def low_risk(self) -> list[ToolSpec]:
        return [s for s in self._specs.values() if not s.requires_approval]

    def describe_for_llm(self) -> list[dict]:
        """转 LLM function calling schema（Day5 意图识别/工具选择用）。"""
        out = []
        for s in self._specs.values():
            out.append({
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters_schema,
                },
            })
        return out


# 全局注册表（模块级单例，Day5 编排直接引用）
registry = ToolRegistry()


# ==================== 工具基类（熔断 + 降级链 + 快照） ====================

class BaseTool:
    """工具统一骨架：
    每次调用 = 熔断器（每工具一实例）→ 降级链（主源重试 → 备用只读快照重试 → 兜底）
    写操作还要求幂等键（Day3 临时 UUID，Day4 换正式幂等模块）。
    """

    name = "base"
    spec: ToolSpec | None = None

    def __init__(self, base_url: str, snapshot: dict | None = None,
                 retries: int = 2, base_delay: float = 0.5,
                 failure_threshold: int = 5, cooldown: float = 10.0):
        self.client = httpx.Client(base_url=base_url, timeout=10.0)
        self.retries = retries
        self.base_delay = base_delay
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        # ★ 每工具一个熔断器实例（懒创建）：update_order 熔断不影响 query_order
        self._breakers: dict[str, CircuitBreaker] = {}
        # 备用只读快照：主源失败时的降级数据源（"上次成功结果"）
        self.snapshot: dict = snapshot if snapshot is not None else {}

    def _get_breaker(self, name: str | None = None) -> CircuitBreaker:
        """按工具名取熔断器（懒创建，每工具一实例）。"""
        name = name or self.name
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name, failure_threshold=self.failure_threshold, cooldown=self.cooldown)
        return self._breakers[name]

    @property
    def breaker(self) -> CircuitBreaker:
        """默认熔断器（未指定工具名的场景）。"""
        return self._get_breaker(self.name)

    # ---- 只读快照 ----
    def _snapshot_set(self, key: str, value: dict):
        self.snapshot[key] = copy.deepcopy(value)

    def _snapshot_get(self, key: str):
        v = self.snapshot.get(key)
        return copy.deepcopy(v) if v is not None else None

    # ---- 兜底 ----
    def _unavailable(self, what: str) -> dict:
        return {"error": f"{what}服务暂不可用，请稍后重试"}

    # ---- 观测回调 ----
    def _log_retry(self, attempt: int, delay: float, exc: Exception):
        print(f"  [RETRY:{self.name}] 第{attempt}次失败 ({exc})，{delay:.2f}s 后重试")

    def _log_level(self, level: int, exc: Exception):
        print(f"  [DEGRADE:{self.name}] 级别{level}失败 ({exc})，切换下一级")

    # ---- 统一调用封装 ----
    def _invoke(self, primary, backups: tuple = (),
                fallback: Callable | None = None,
                breaker_name: str | None = None) -> ToolResult:
        """熔断器 + 降级链统一封装。返回 ToolResult。

        ★ 分层：熔断器保护**主源**（每工具一实例），降级链在熔断外层——
        - 熔断 CLOSED：主源正常调（失败计数）；连续失败达阈值 → OPEN
        - 熔断 OPEN：主源快速失败（CircuitOpenError，不重试）→ 降级链切备用/兜底
        - 业务错误（400/404/409）：不重试不降级，透传为 ToolResult(success=False)
        - 快照写入由各工具方法在"主源成功"后显式调用（只有读操作才有快照）
        """
        breaker = self._get_breaker(breaker_name)

        def guarded_primary():
            return breaker.call(primary)

        try:
            result, meta = degrade_chain(
                guarded_primary, backups=backups, fallback=fallback,
                retries=self.retries, base_delay=self.base_delay,
                retryable=is_retryable_http,
                on_retry=self._log_retry, on_level=self._log_level)
        except BizApiError as e:
            # 业务错误（404/409/400）：不重试不降级，如实透传
            return ToolResult(success=False, error=e.message, attempts=1,
                              circuit_state=breaker.state,
                              meta={"biz_error": True, "status_code": e.status_code})
        except Exception as e:
            # 极端兜底：所有降级级别全部失败（正常流程 fallback 会接住）
            return ToolResult(success=False, error=f"工具调用失败: {e}",
                              circuit_state=breaker.state,
                              meta={"last_error": str(e)})

        meta.setdefault("circuit_open", False)
        return self._result(result, meta, breaker)

    def _result(self, data: dict, meta: dict, breaker: CircuitBreaker) -> ToolResult:
        """data → ToolResult。约定：data 含 error 键视为失败（兜底占位）。"""
        return ToolResult(
            success=meta.get("fallback_used") is False and "error" not in data,
            data=None if "error" in data else data,
            error=data.get("error") if "error" in data else None,
            degraded=meta.get("degraded", False),
            level=meta.get("level", 0),
            attempts=meta.get("attempts", 1),
            circuit_state=breaker.state,
            meta=meta,
        )

    # ---- 写操作幂等键（Day3 临时，Day4 换正式 idempotency.py） ----
    @staticmethod
    def new_idempotency_key() -> str:
        return str(uuid.uuid4())
