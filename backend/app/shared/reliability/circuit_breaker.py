"""★ A1 熔断器（W19 Day3 欠账落地）：三态状态机 CLOSED → OPEN → HALF_OPEN

- CLOSED（正常）：每次失败计数；连续失败 ≥ failure_threshold → OPEN
- OPEN（熔断）：快速失败（不调下游接口），冷却 cooldown 秒后 → HALF_OPEN
- HALF_OPEN（半开探测）：放行探测请求；成功 → CLOSED（复位计数），失败 → OPEN（重计冷却）
- 每个工具一个实例：update_order 熔断不影响 query_order

面试要点："熔断不是玄学，是状态机"——三态 + 两个转换条件（连续失败数 / 冷却时间 + 探测结果）。
生产 async 版只需把 time.sleep 换 asyncio.sleep（W10 同款理由：事件循环阻塞）。
"""
import time


class CircuitOpenError(Exception):
    """熔断器 OPEN 时快速失败抛出（调用方决定降级行为）。"""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5,
                 cooldown: float = 10.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._state = "CLOSED"
        self._consecutive_failures = 0  # 连续失败次数
        self._opened_at: float | None = None

    # ---- 观测 ----
    @property
    def state(self) -> str:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _transition(self, new_state: str, reason: str):
        """状态转移（打印日志）。"""
        print(f"[CIRCUIT:{self.name}] {self._state} → {new_state} ｜ {reason}")
        self._state = new_state

    # ---- 核心调用 ----
    def call(self, func, *args, **kwargs):
        """执行受保护调用。熔断 OPEN 时抛 CircuitOpenError（快速失败，不调 func）。"""
        if self._state == "OPEN":
            if time.monotonic() - self._opened_at >= self.cooldown:
                self._transition("HALF_OPEN", "冷却期结束，放行半开探测")
            else:
                raise CircuitOpenError(
                    f"circuit '{self.name}' is OPEN, fast-fail (剩余冷却 "
                    f"{max(0.0, self.cooldown - (time.monotonic() - self._opened_at)):.1f}s)")

        try:
            result = func(*args, **kwargs)
        except Exception as e:
            self._on_failure(e)
            raise
        self._on_success()
        return result

    # ---- 状态转移 ----
    def _on_success(self):
        if self._state == "HALF_OPEN":
            self._transition("CLOSED", "半开探测成功，熔断恢复")
        self._consecutive_failures = 0

    def _on_failure(self, exc: Exception):
        if self._state == "HALF_OPEN":
            # 探测失败 → 立即回 OPEN，重新计时
            self._transition("OPEN", f"半开探测失败: {exc}")
            self._opened_at = time.monotonic()
            self._consecutive_failures = 1
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._transition("OPEN", f"连续失败 {self._consecutive_failures} ≥ "
                                     f"{self.failure_threshold}")
            self._opened_at = time.monotonic()

    def reset(self):
        """手动复位（测试/运维用）。"""
        self._state = "CLOSED"
        self._consecutive_failures = 0
        self._opened_at = None
