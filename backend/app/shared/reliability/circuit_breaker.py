"""★ A1 熔断器（W19 Day3 欠账落地）：三态状态机 CLOSED → OPEN → HALF_OPEN
★ W27 Day3 A5：OPEN 状态 Redis 共享（双实例各熔各的 → 秒级收敛）

- CLOSED（正常）：每次失败计数；连续失败 ≥ failure_threshold → OPEN
- OPEN（熔断）：快速失败（不调下游接口），冷却 cooldown 秒后 → HALF_OPEN
- HALF_OPEN（半开探测）：放行探测请求；成功 → CLOSED（复位计数），失败 → OPEN（重计冷却）
- 每个工具一个实例：update_order 熔断不影响 query_order

★ A5 Redis 共享设计（面试防御："熔断状态为什么不全放 Redis？"）：
- 本地保留 CLOSED 快路径（failure 计数在进程内）——阈值触发时才写 Redis `cb:{name}`（TTL=cooldown）
- 每请求先查本地缓存（1s stale 内不查 Redis）——避免每请求一次 RTT 的延迟税
- OPEN 过期后任一实例探测成功 → 删 Redis 键广播恢复（秒级收敛，一致性代价可接受）
- Redis 挂（fail-open）：本地状态机照常工作，不因 Redis 抖动误熔断

面试要点："熔断不是玄学，是状态机"——三态 + 两个转换条件（连续失败数 / 冷却时间 + 探测结果）。
生产 async 版只需把 time.sleep 换 asyncio.sleep（W10 同款理由：事件循环阻塞）。
"""
import time

# A5：共享状态本地缓存 TTL（1s stale 内不查 Redis——延迟税 vs 收敛速度的权衡点）
_REMOTE_CACHE_TTL = 1.0


class CircuitOpenError(Exception):
    """熔断器 OPEN 时快速失败抛出（调用方决定降级行为）。"""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5,
                 cooldown: float = 10.0, redis_client=None):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._state = "CLOSED"
        self._consecutive_failures = 0  # 连续失败次数
        self._opened_at: float | None = None
        # ★ A5：Redis 共享熔断状态（None = 不启用共享，单机语义；生产传 get_redis_client()）
        self._redis = redis_client
        self._redis_key = f"cb:{name}"     # 共享 OPEN 标记键
        self._remote_cache: tuple[float, bool] | None = None  # (monotonic_ts, is_open)

    # ---- 观测 ----
    @property
    def state(self) -> str:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _transition(self, new_state: str, reason: str):
        """状态转移（打印日志）。OPEN/CLOSED 时同步广播到 Redis（A5）。"""
        print(f"[CIRCUIT:{self.name}] {self._state} → {new_state} ｜ {reason}")
        self._state = new_state
        if new_state == "OPEN":
            self._open_remote()
        elif new_state == "CLOSED":
            self._close_remote()

    # ---- A5：Redis 共享状态 ----

    def _remote_open(self) -> bool:
        """查询共享熔断状态：Redis `cb:{name}` TTL>0 → 其他实例已 OPEN → fast-fail。

        ★ 延迟税权衡（面试题）：不全放 Redis 因为每请求一次 RTT；本地 1s stale
        缓存 + 状态转变广播 = 秒级收敛。Redis 挂 → 返回 False（本地状态机照常）。
        """
        if self._redis is None:
            return False
        now = time.monotonic()
        if self._remote_cache is not None and now - self._remote_cache[0] < _REMOTE_CACHE_TTL:
            return self._remote_cache[1]
        state = False
        if self._redis.available:   # 5s 冷却内不试连（fail-open 快速判定）
            try:
                # TTL 语义（手册坑）：-2 键不存在 / -1 无过期 → 仅 >0 视为 OPEN
                state = self._redis.ttl(self._redis_key) > 0
            except Exception:
                state = False       # Redis 抖动 → 不误熔断（fail-open）
        self._remote_cache = (now, state)
        return state

    def _open_remote(self):
        """本地熔断 OPEN → 写共享键（TTL=cooldown，其他实例秒级感知）。"""
        if self._redis is None:
            return
        try:
            if self._redis.available:
                self._redis.set(self._redis_key, "OPEN",
                                ex=int(max(1, self.cooldown)))
                self._remote_cache = (time.monotonic(), True)
        except Exception:
            pass                    # Redis 挂 → 本地状态机照常（fail-open）

    def _close_remote(self):
        """半开探测成功 → 删共享键（广播恢复，所有实例回 CLOSED）。"""
        if self._redis is None:
            return
        try:
            if self._redis.available:
                self._redis.delete(self._redis_key)
                self._remote_cache = (time.monotonic(), False)
        except Exception:
            pass

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
        elif self._remote_open():
            # ★ A5：其他实例已熔断（本地 CLOSED 也快速失败）——双实例共享语义
            raise CircuitOpenError(
                f"circuit '{self.name}' is OPEN remotely (shared state), fast-fail")

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
        self._close_remote()
