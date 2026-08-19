"""★ W26 Day3 全量验收补测：熔断器三态状态机 + 每工具独立熔断 + 降级链配合。

Day3 端到端场景清单（ops 域）要求"熔断"场景有 pytest 级证据——
原 W19 时代的独立脚本（scripts/ops_day3_tools_test.py）未同步平台化签名，
故以纯逻辑 pytest 形式在 CI 内固化状态机语义（不依赖 mock server）。

覆盖：
- CLOSED：连续失败计数，达阈值 → OPEN
- OPEN：快速失败（不调 func），冷却后 → HALF_OPEN
- HALF_OPEN：探测成功 → CLOSED；探测失败 → OPEN（重计冷却）
- 每工具独立熔断：query_order OPEN 不影响 cancel_order
- 与 degrade_chain 配合：熔断 OPEN 时不重试直接降级备用
"""
import contextlib

import pytest

from app.shared.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.shared.reliability.retry_policy import degrade_chain

# ==================== 三态状态机 ====================

def test_closed_tracks_consecutive_failures():
    """CLOSED：连续失败计数；未达阈值不 OPEN；成功复位。"""
    cb = CircuitBreaker("t", failure_threshold=3, cooldown=10)

    for _ in range(2):
        with contextlib.suppress(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "CLOSED" and cb.consecutive_failures == 2

    # 成功 → 计数复位
    cb.call(lambda: "ok")
    assert cb.state == "CLOSED" and cb.consecutive_failures == 0


def test_open_after_threshold_and_fast_fail():
    """达到阈值 → OPEN；OPEN 期快速失败（不调 func）。"""
    cb = CircuitBreaker("t", failure_threshold=3, cooldown=60)
    called = {"n": 0}

    def flaky():
        called["n"] += 1
        raise ConnectionError("boom")

    for _ in range(3):
        with contextlib.suppress(ConnectionError):
            cb.call(flaky)
    assert cb.state == "OPEN"

    # OPEN：快速失败，func 不再被调
    with pytest.raises(CircuitOpenError):
        cb.call(flaky)
    assert called["n"] == 3


def test_half_open_probe_success_closes():
    """冷却结束 → HALF_OPEN；探测成功 → CLOSED。"""
    cb = CircuitBreaker("t", failure_threshold=1, cooldown=0)  # cooldown=0 立即半开
    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "OPEN"

    cb.call(lambda: "recovered")  # 半开探测成功
    assert cb.state == "CLOSED"


def test_half_open_probe_failure_reopens():
    """半开探测失败 → 立即回 OPEN 并重计冷却。"""
    cb = CircuitBreaker("t", failure_threshold=1, cooldown=0)
    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))

    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("still down")))
    assert cb.state == "OPEN"


# ==================== 每工具独立熔断 ====================

def test_per_tool_breakers_are_independent():
    """每个工具一个熔断器实例：一个 OPEN 不影响另一个（registry._get_breaker 懒创建）。"""
    from app.domains.ops.agent.tools.registry import BaseTool

    class FakeTool(BaseTool):
        name = "fake_tool"

    tool = FakeTool(base_url="http://127.0.0.1:9", retries=0, base_delay=0,
                    failure_threshold=2, cooldown=60)
    qb = tool._get_breaker("query_order")
    cb = tool._get_breaker("cancel_order")

    # 只打爆 query_order
    for _ in range(2):
        with contextlib.suppress(Exception):
            qb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert qb.state == "OPEN"
    assert cb.state == "CLOSED"  # cancel_order 不受影响


# ==================== 与降级链配合 ====================

def test_breaker_open_skips_retries_and_degrades():
    """熔断 OPEN → degrade_chain 不重试主源，直接降级备用（level=1）。"""
    cb = CircuitBreaker("chain", failure_threshold=1, cooldown=60)
    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "OPEN"

    backup = lambda: {"source": "backup"}  # noqa: E731
    result, meta = degrade_chain(
        lambda: cb.call(lambda: "never"),
        backups=(backup,), retries=3, base_delay=0.01)
    assert meta["level"] == 1 and meta["degraded"]
    assert result["source"] == "backup"
