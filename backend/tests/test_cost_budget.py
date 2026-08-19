"""W27 Day5 覆盖率冲刺 I：成本预算独立测试（cost_budget.py）。

覆盖手册 Day5：
- INCRBYFLOAT 跨实例累计（Redis 权威值，status 从 Redis 读）
- 超限抛 BudgetExceeded（raise_on_exceed=True；读路径默认降级不抛）
- Redis 挂 → 本地近似 + DEGRADED 日志 + metrics 计数
- estimate_tokens_from_messages / get_session_budget 懒创建复用
（D3 矩阵已覆盖 4 格主路径，本文件补行为细节分支）
"""
import pytest

from app.shared.obs import metrics as m
from app.shared.reliability.cost_budget import (
    BudgetExceeded,
    SessionBudget,
    estimate_tokens_from_messages,
    get_session_budget,
    reset_budgets,
)


class FakeRedis:
    """内存版 Redis：实现预算用到的 incrbyfloat / get。"""

    def __init__(self, available: bool = True):
        self._store: dict[str, str] = {}
        self.available = available

    def incrbyfloat(self, key: str, amount: float) -> float:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        v = float(self._store.get(key, "0")) + amount
        self._store[key] = repr(v)
        return v

    def get(self, key: str) -> str | None:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.get(key)


@pytest.fixture(autouse=True)
def _clean_metrics():
    m.clear()
    yield
    m.clear()


class TestRedisAccumulation:
    def test_incrbyfloat_accumulates(self):
        fake = FakeRedis()
        b = SessionBudget(budget_yuan=100.0, session_id="s1", redis_client=fake)
        b.add_usage(1000, 200)
        b.add_usage(500, 100)
        assert float(fake.get("cost:s1:input_tokens")) == 1500.0
        assert float(fake.get("cost:s1:output_tokens")) == 300.0
        st = b.status()
        assert st["total_input_tokens"] == 1500.0
        assert st["cost_yuan"] == pytest.approx((1500 * 2 + 300 * 8) / 1_000_000)

    def test_status_reads_redis_authoritative(self):
        fake = FakeRedis()
        b = SessionBudget(budget_yuan=100.0, session_id="s2", redis_client=fake)
        fake._store["cost:s2:input_tokens"] = "500"
        fake._store["cost:s2:output_tokens"] = "100"
        st = b.status()
        assert st["total_input_tokens"] == 500.0
        assert st["cost_yuan"] == pytest.approx((500 * 2 + 100 * 8) / 1_000_000)

    def test_is_over_budget(self):
        fake = FakeRedis()
        b = SessionBudget(budget_yuan=0.0001, session_id="s3", redis_client=fake)
        b.add_usage(100, 100)
        assert b.is_over_budget() is True


class TestBudgetExceeded:
    def test_raise_on_exceed(self):
        fake = FakeRedis()
        b = SessionBudget(budget_yuan=0.0001, session_id="s4", redis_client=fake)
        with pytest.raises(BudgetExceeded):
            b.add_usage(100, 100, raise_on_exceed=True)

    def test_read_path_degrades_not_raise(self):
        fake = FakeRedis()
        b = SessionBudget(budget_yuan=0.0001, session_id="s5", redis_client=fake)
        b.add_usage(100, 100)  # 不 raise_on_exceed → 只打粘滞降级标记
        assert b.degraded is True
        assert b.degraded_at is not None

    def test_budget_exceeded_is_exception(self):
        assert issubclass(BudgetExceeded, Exception)


class TestRedisDown:
    def test_local_approx_when_down(self, caplog):
        fake = FakeRedis(available=False)
        b = SessionBudget(budget_yuan=100.0, session_id="s6", redis_client=fake)
        b.add_usage(1000, 200)
        assert b.status()["total_input_tokens"] == 1000.0
        assert "budget_redis_down" in caplog.text, "应有 DEGRADED 日志事件"
        assert "scm_budget_redis_down_total" in m.render(), "应有降级 metrics 计数"

    def test_local_approx_without_redis(self):
        b = SessionBudget(budget_yuan=100.0, session_id="s7", redis_client=None)
        b.add_usage(100, 50)
        st = b.status()
        assert st["total_input_tokens"] == 100.0
        assert st["total_output_tokens"] == 50.0

    def test_redis_operation_error_falls_back(self, caplog):
        """available=True 但 INCRBYFLOAT 抛错（Redis 刚挂）→ 落本地近似。"""

        class _Flaky(FakeRedis):
            def __init__(self):
                super().__init__(available=True)
                self._calls = 0

            def incrbyfloat(self, key, amount):
                self._calls += 1
                if self._calls <= 1:
                    raise ConnectionError("down now")
                return super().incrbyfloat(key, amount)

        fake = _Flaky()
        b = SessionBudget(budget_yuan=100.0, session_id="s8", redis_client=fake)
        b.add_usage(100, 50)
        assert b.status()["total_input_tokens"] == 100.0, "操作异常落本地近似"
        assert "budget_redis_down" in caplog.text


class TestHelpers:
    def test_estimate_tokens_from_messages(self):
        msgs = [{"role": "user", "content": "一二三四五"}, {"role": "assistant", "content": ""}]
        assert estimate_tokens_from_messages(msgs) == 5

    def test_get_session_budget_lazy_and_reuse(self):
        reset_budgets()
        fake = FakeRedis()
        b1 = get_session_budget("s9", budget_yuan=1.0, redis_client=fake)
        b2 = get_session_budget("s9")  # 已存在 → 复用实例
        assert b1 is b2
        reset_budgets()
