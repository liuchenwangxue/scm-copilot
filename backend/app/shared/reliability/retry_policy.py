"""★ A2 降级链 + 重试叠加（W19 Day3 欠账落地）

- RetryPolicy：单级调用内部重试（指数退避 + jitter + 可重试异常过滤）
- degrade_chain：多级降级链，**每级内部再套 RetryPolicy**（W10 P1 "降级链+重试叠加"）

语义分层（面试核心）：
- 重试处理"瞬时故障"（429/5xx/连接错误）——每级内部重试 N 次
- 降级处理"持续故障"（重试 N 次仍失败）——切下一级备用源
- 每降一级功能降一档，但服务不断——"可用性优先于功能完整性"

示例：
    result, meta = degrade_chain(
        primary=lambda: get_order_real(order_id),        # 主源：实时接口
        backups=(lambda: get_order_snapshot(order_id),), # 备用：只读快照
        fallback=lambda: {"error": "服务暂不可用，请稍后"}, # 兜底
        retries=2, base_delay=0.5)
"""
import random
import time
from collections.abc import Callable

from app.shared.reliability.circuit_breaker import CircuitOpenError

# ---- 哪些异常值得重试 ----

def is_retryable_http(exc: Exception) -> bool:
    """重试过滤：瞬时错误重试；业务错误（400/404/409）与熔断快速失败不重试。

    返回值语义（degrade_chain 使用）：
    - True  → 重试（本次失败可能是瞬时故障）
    - False → 不重试；再区分：CircuitOpenError 走降级，业务错误透传
    """
    if isinstance(exc, CircuitOpenError):
        return False
    text = str(exc).lower()
    if any(k in text for k in ("429", "500", "502", "503", "504", "timeout",
                               "timed out", "connection", "remote closed")):
        return True
    # 业务错误码（4xx 除 429 限流外）明确不重试
    return not any(k in text for k in ("400", "404", "409", "422"))


# ---- 重试：指数退避 + jitter ----

class RetryPolicy:
    """单级调用重试：尝试 1 + max_retries 次，指数退避 base * 2^n + jitter。

    run() 返回 (result, attempts, recovered)：
    - attempts：实际尝试次数（1 = 一次成功，无重试）
    - recovered：attempts > 1（被重试救回）——Day6 成功率统计"含重试"就用它
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 0.5,
                 retryable: Callable[[Exception], bool] | None = None,
                 on_retry: Callable | None = None):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.retryable = retryable
        self.on_retry = on_retry

    def run(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                return result, attempt + 1, attempt > 0
            except Exception as e:
                last_exc = e
                if attempt >= self.max_retries:
                    break
                if self.retryable and not self.retryable(e):
                    #熔断或业务出错都不会重试，直接透传(抛回上一级)
                    raise
                delay = self.base_delay * (2 ** attempt) + random.uniform(0, self.base_delay)
                if self.on_retry:
                    self.on_retry(attempt + 1, delay, e)
                time.sleep(delay)
        raise RuntimeError(f"重试 {self.max_retries} 次后仍失败: {last_exc}")


# ---- 降级链：每级内部套重试 ----

def degrade_chain(primary: Callable, backups: tuple = (),
                  fallback: Callable | None = None,
                  retries: int = 2, base_delay: float = 0.5,
                  retryable: Callable[[Exception], bool] | None = None,
                  on_retry: Callable | None = None,
                  on_level: Callable | None = None):
    """主源重试 retries 次 → 备用1 重试 retries 次 → ... → fallback（最终兜底）。

    Args:
        primary: 主源函数（质量最高）
        backups: 备用源函数元组（质量次之，如只读快照/降级数据源）
        fallback: 最终兜底（如返回"服务暂不可用"占位），可不传则抛异常
        retries: **每级内部重试次数**（降级链+重试叠加的核心参数）
        retryable: 可重试异常过滤（默认 None = 全部异常都重试）
        on_retry / on_level: 观测回调（打日志/span）

    Returns:
        (result, meta)；meta = {"level": 用到的级别(0=主源,1=第一备用...),
                                "attempts": 总尝试次数, "degraded": 是否降级,
                                "fallback_used": 是否落到兜底, "last_error": 最后异常}
    """
    levels = [primary, *backups]
    attempts_total = 0
    last_error: BaseException | None = None
    for i, level in enumerate(levels):
        rp = RetryPolicy(max_retries=retries, base_delay=base_delay,
                         retryable=retryable, on_retry=on_retry)
        try:
            result, attempts, _ = rp.run(level)
            return result, {"level": i, "attempts": attempts_total + attempts,
                            "degraded": i > 0, "fallback_used": False, "last_error": None}
        except Exception as e:
            last_error = e
            # ★ 分层关键（两种不可重试异常，两种处理）：
            # - 熔断快速失败（CircuitOpenError）：不重试但**继续降级**——熔断=主源不可用，切备用/兜底
            # - 业务错误（400/404/409）：不重试不降级，直接透传——业务拒绝应如实告诉用户
            if retryable is not None and not retryable(e):
                if isinstance(e, CircuitOpenError):
                    attempts_total += 1
                    if on_level:
                        on_level(i, e)
                else:
                    raise
                continue
            attempts_total += 1 + retries
            if on_level:
                on_level(i, e)

    if fallback is not None:
        try:
            result = fallback()
            return result, {"level": len(levels), "attempts": attempts_total,
                            "degraded": True, "fallback_used": True, "last_error": None}
        except Exception as e:
            last_error = e

    raise RuntimeError(f"所有降级级别均失败: {last_error}")
