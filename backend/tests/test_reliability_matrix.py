"""W27 Day3 可靠性四组件 redis-down 行为矩阵（16 格）——面试防御核心证据表。

4 组件 × {Redis 正常, Redis 挂} × {读, 写} = 16 格逐一断言。

覆盖手册 Day3（A5–A8）：
- A5 熔断器状态 Redis 共享：OPEN 写 `cb:{name}`、1s stale 缓存查共享态、半开成功删键广播恢复
- A6 分布式锁 fail-open 兜底：Redis 挂 → 同 key 进程内互斥 + metrics + DEGRADED 日志
- A7 幂等写路径 fail-closed：risk="write" 时 Redis 挂 → IdemUnavailableError（IDEM_UNAVAILABLE）
- A8 成本预算 Redis 化：INCRBYFLOAT 跨实例累计；Redis 挂 → 本地近似 + DEGRADED 日志

验收证据：
- 16 格矩阵全绿
- ops 高危工具在 Redis 挂时实测被拒（execute_node + fake idem_store）
- metrics 里能看到 fallback 计数（scm_lock_local_fallback_total / scm_idem_fail_closed_total 等）
"""

import contextlib

import pytest

from app.shared.obs import metrics as m
from app.shared.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.shared.reliability.cost_budget import BudgetExceeded, SessionBudget
from app.shared.reliability.distributed_lock import DistributedLock, reset_local_locks
from app.shared.reliability.idempotency import IdempotencyStore, IdemUnavailableError


class FakeRedis:
    """内存版 Redis 客户端（矩阵测试用）：实现 A5/A6/A7/A8 用到的原语。

    available 可切换（False = 模拟 Redis 挂，操作抛 ConnectionError）。
    """

    def __init__(self, available: bool = True):
        self._store: dict[str, str] = {}
        self.available = available

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        self._store[key] = value
        return True

    def get(self, key: str) -> str | None:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.get(key)

    def delete(self, key: str) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.pop(key, None) is not None

    def ttl(self, key: str) -> int:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return 10 if key in self._store else -2   # -2=键不存在

    def set_nx(self, key: str, value: str, ex: int | None = None) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        if key in self._store:
            return False
        self._store[key] = value
        return True

    def delete_if_equals(self, key: str, expected: str) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        if self._store.get(key) == expected:
            del self._store[key]
            return True
        return False

    def incrbyfloat(self, key: str, amount: float) -> float:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        v = float(self._store.get(key, "0")) + amount
        self._store[key] = repr(v)
        return v


class DownRedis(FakeRedis):
    """Redis 挂：available=False（所有操作抛 ConnectionError）。"""

    def __init__(self):
        super().__init__(available=False)


@pytest.fixture(autouse=True)
def _clean_state():
    """每个测试前后清本地兜底锁 + metrics（避免跨用例计数/持锁污染）。"""
    reset_local_locks()
    m.clear()
    yield
    reset_local_locks()
    m.clear()


# =====================================================================
# A5 熔断器（4 格）
# =====================================================================

def test_cb_redis_ok_read_remote_open_fast_fails():
    """Redis 正常 + 读：本地 CLOSED 但共享键 OPEN → fast-fail（不调 func）。"""
    fake = FakeRedis()
    cb = CircuitBreaker("query_order", failure_threshold=5, cooldown=60,
                        redis_client=fake)
    fake.set("cb:query_order", "OPEN", ex=60)   # 另一实例已熔断
    called = {"n": 0}

    with pytest.raises(CircuitOpenError):
        cb.call(lambda: called.__setitem__("n", called["n"] + 1))
    assert called["n"] == 0, "共享 OPEN 时应快速失败，不调下游"


def test_cb_redis_ok_write_open_propagates_to_redis():
    """Redis 正常 + 写：本地达阈值 → OPEN → 广播写 `cb:{name}`。"""
    fake = FakeRedis()
    cb = CircuitBreaker("update_order", failure_threshold=2, cooldown=60,
                        redis_client=fake)
    for _ in range(2):
        with contextlib.suppress(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "OPEN"
    assert fake.get("cb:update_order") == "OPEN", "熔断 OPEN 应写共享键（其他实例秒级感知）"


def test_cb_redis_down_read_local_state_works():
    """Redis 挂 + 读：本地 CLOSED 正常放行（不因 Redis 抖动误熔断，fail-open）。"""
    fake = DownRedis()
    cb = CircuitBreaker("query_order", failure_threshold=3, cooldown=60,
                        redis_client=fake)
    assert cb.call(lambda: "ok") == "ok"


def test_cb_redis_down_write_local_open_no_error():
    """Redis 挂 + 写：本地熔断 OPEN 照常（写 Redis 失败静默，不抛错不影响状态机）。"""
    fake = DownRedis()
    cb = CircuitBreaker("cancel_order", failure_threshold=2, cooldown=60,
                        redis_client=fake)
    for _ in range(2):
        with contextlib.suppress(ConnectionError):
            cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "OPEN"


def test_cb_half_open_success_removes_redis_key():
    """半开探测成功 → CLOSED → 删共享键（广播恢复，所有实例回 CLOSED）。"""
    fake = FakeRedis()
    cb = CircuitBreaker("query_order", failure_threshold=1, cooldown=0,
                        redis_client=fake)
    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert cb.state == "OPEN" and fake.get("cb:query_order") == "OPEN"

    cb.call(lambda: "recovered")                 # 半开探测成功
    assert cb.state == "CLOSED"
    assert fake.get("cb:query_order") is None, "恢复后应删共享键（广播恢复）"


# =====================================================================
# A6 分布式锁（4 格）
# =====================================================================

def test_lock_redis_ok_read_acquire_setnx():
    """Redis 正常 + 读：acquire 走 SETNX 占位（分布式语义）。"""
    fake = FakeRedis()
    lk = DistributedLock("report", ttl=30, redis_client=fake)
    assert lk.acquire() is True
    assert fake.get("lock:report") is not None
    lk.release()


def test_lock_redis_ok_write_release_owner_checked():
    """Redis 正常 + 写：互斥（第二个拿不到）+ owner 校验释放（释放后可再抢）。"""
    fake = FakeRedis()
    a = DistributedLock("report2", ttl=30, redis_client=fake)
    b = DistributedLock("report2", ttl=30, redis_client=fake)
    assert a.acquire() and not b.acquire()       # 互斥
    a.release()
    assert b.acquire(), "释放后其他实例可再抢"


def test_lock_redis_down_read_local_fallback(caplog):
    """Redis 挂 + 读：本地互斥兜底成功 + metrics + DEGRADED 日志。"""
    fake = DownRedis()
    lk = DistributedLock("report3", ttl=30, redis_client=fake)
    assert lk.acquire() is True                  # 本地兜底成功
    assert "lock_local_fallback" in caplog.text, "应有 DEGRADED 日志事件"
    assert 'scm_lock_local_fallback_total{component="lock"} 1.0' in m.render(), \
        "metrics 里应能看到 fallback 计数"


def test_lock_redis_down_write_release_local():
    """Redis 挂 + 写：本地锁释放不抛错，释放后可再抢（同 key 互斥语义保持）。"""
    fake = DownRedis()
    lk = DistributedLock("report4", ttl=30, redis_client=fake)
    assert lk.acquire() is True
    lk.release()                                 # 本地释放
    lk2 = DistributedLock("report4", ttl=30, redis_client=fake)
    assert lk2.acquire() is True, "本地锁释放后可再抢"


# =====================================================================
# A7 幂等（4 格）
# =====================================================================

def test_idem_redis_ok_read_resolves_redis(tmp_path):
    """Redis 正常 + 读（risk=read）：后端解析为 Redis。"""
    fake = FakeRedis()
    store = IdempotencyStore(tmp_path / "a.db", backend="auto", redis_client=fake)
    assert store.resolve_backend(risk="read") is store._redis


def test_idem_redis_ok_write_resolves_redis(tmp_path):
    """Redis 正常 + 写（risk=write）：后端解析为 Redis（不抛）。"""
    fake = FakeRedis()
    store = IdempotencyStore(tmp_path / "b.db", backend="auto", redis_client=fake)
    assert store.resolve_backend(risk="write") is store._redis


def test_idem_redis_down_read_fail_open_sqlite(tmp_path):
    """Redis 挂 + 读（risk=read）：fail-open 降级 sqlite（读类请求允许降级）。"""
    fake = DownRedis()
    store = IdempotencyStore(tmp_path / "c.db", backend="auto", redis_client=fake)
    assert store.resolve_backend(risk="read") is store._sqlite


def test_idem_redis_down_write_fail_closed_rejects(tmp_path):
    """Redis 挂 + 写（risk=write）：fail-closed 拒绝（错误码 IDEM_UNAVAILABLE）+ metrics。"""
    fake = DownRedis()
    store = IdempotencyStore(tmp_path / "d.db", backend="auto", redis_client=fake)
    with pytest.raises(IdemUnavailableError) as ei:
        store.resolve_backend(risk="write")
    assert ei.value.error_code == "IDEM_UNAVAILABLE"
    assert "scm_idem_fail_closed_total" in m.render(), "fail-closed 拒绝应有 metrics 计数"


# =====================================================================
# A8 成本预算（4 格）
# =====================================================================

def test_budget_redis_ok_write_incrbyfloat_accumulates():
    """Redis 正常 + 写：add_usage 用 INCRBYFLOAT 跨实例累计。"""
    fake = FakeRedis()
    b = SessionBudget(budget_yuan=100.0, session_id="s-b1", redis_client=fake)
    b.add_usage(1000, 200)
    b.add_usage(500, 100)
    assert float(fake.get("cost:s-b1:input_tokens")) == 1500.0
    assert float(fake.get("cost:s-b1:output_tokens")) == 300.0
    assert b.status()["cost_yuan"] == pytest.approx((1500 * 2 + 300 * 8) / 1_000_000)


def test_budget_redis_ok_read_status_from_redis():
    """Redis 正常 + 读：status 读 Redis 权威计数（跨实例准确）。"""
    fake = FakeRedis()
    b = SessionBudget(budget_yuan=100.0, session_id="s-b2", redis_client=fake)
    fake.set("cost:s-b2:input_tokens", "500")
    fake.set("cost:s-b2:output_tokens", "100")
    st = b.status()
    assert st["total_input_tokens"] == 500
    assert st["cost_yuan"] > 0


def test_budget_redis_down_read_local_approx(caplog):
    """Redis 挂 + 读：本地近似值可用（软限制 fail-open，不抛错）。"""
    fake = DownRedis()
    b = SessionBudget(budget_yuan=100.0, session_id="s-b3", redis_client=fake)
    b.add_usage(1000, 200)
    assert b.status()["total_input_tokens"] == 1000
    assert "budget_redis_down" in caplog.text, "应有 DEGRADED 日志事件"


def test_budget_redis_down_write_local_and_metrics(caplog):
    """Redis 挂 + 写：本地累计 + DEGRADED 日志 + metrics 计数。"""
    fake = DownRedis()
    b = SessionBudget(budget_yuan=100.0, session_id="s-b4", redis_client=fake)
    b.add_usage(1000, 200)
    assert b.status()["total_input_tokens"] == 1000
    assert "budget_redis_down" in caplog.text
    assert "scm_budget_redis_down_total" in m.render()


def test_budget_exceeded_raises_when_configured():
    """raise_on_exceed=True → 超限抛 BudgetExceeded（写路径硬限制；读路径默认降级不抛）。"""
    fake = FakeRedis()
    b = SessionBudget(budget_yuan=0.0001, session_id="s-b5", redis_client=fake)
    with pytest.raises(BudgetExceeded):
        b.add_usage(100, 100, raise_on_exceed=True)


# =====================================================================
# 验收补充：ops 高危工具在 Redis 挂时实测被拒（A7 接入 execute_node）
# =====================================================================

def test_ops_write_tool_rejected_when_idem_unavailable(monkeypatch, tmp_path):
    """A7 端到端：幂等保护不可用（Redis 挂 + risk=write）→ 高危写被拒。

    复用真实 execute_node（hooks 参数校验放行），仅注入 FailingIdemStore——
    与手册验收"ops 高危工具在 Redis 挂时实测被拒"对齐。
    """
    from app.domains.ops.agent import graph as g
    from app.domains.ops.security.audit import AuditLogger
    from app.platform import hooks

    class FailingIdemStore:
        def resolve_backend(self, risk: str = "read"):
            if risk == "write":
                raise IdemUnavailableError()
            return object()

    monkeypatch.setattr(g, "idem_store", FailingIdemStore())
    monkeypatch.setattr(g, "audit", AuditLogger(tmp_path / "graph-audit.log"))
    monkeypatch.setattr(hooks, "_audit_logger", AuditLogger(tmp_path / "hooks-audit.log"))

    state = {
        "tool_name": "update_order",
        "tool_params": {"order_id": "PO-0001", "amount": 9500.0},
        "approval": {"status": "approved", "approval_id": "a1", "idem_key": "k1"},
        "session_id": "s-write-reject",
    }
    out = g.execute_node(state)
    tr = out["tool_result"]
    assert tr["success"] is False
    assert "IDEM_UNAVAILABLE" in tr["error"], "拒绝消息应带明确错误码（区别于通用 500）"
