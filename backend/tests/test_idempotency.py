"""W27 Day5 覆盖率冲刺 I：分布式幂等独立测试（idempotency.py）。

覆盖手册 Day5：
- Redis 后端：SUCCESS 缓存 / RUNNING 不缓存 / FAILED 可重试 / TTL 300s
- sqlite 降级路径（Redis 挂 + risk=read）
- 写路径 fail-closed（Redis 挂 + risk=write → IdemUnavailableError）
- execute_idempotent：首次执行 / 幂等命中 / 失败标记后可重试
（D3 矩阵已覆盖后端解析主路径，本文件补行为细节 + 端到端包装）
"""
import pytest

from app.shared.reliability.idempotency import (
    DEFAULT_TTL,
    IdempotencyStore,
    IdemUnavailableError,
    execute_idempotent,
)


class FakeRedis:
    """内存版 Redis：实现幂等用到的 set_nx / set / get / ttl。"""

    def __init__(self, available: bool = True):
        self._store: dict[str, str] = {}
        self._ttl: dict[str, int | None] = {}
        self.available = available

    def set_nx(self, key: str, value: str, ex: int | None = None) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        if key in self._store:
            return False
        self._store[key] = value
        self._ttl[key] = ex
        return True

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        self._store[key] = value
        self._ttl[key] = ex
        return True

    def get(self, key: str) -> str | None:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.get(key)

    def ttl(self, key: str) -> int:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        v = self._ttl.get(key)
        return -2 if v is None else v


class TestRedisBackend:
    def _store(self, tmp_path, fake, **kw):
        return IdempotencyStore(tmp_path / "idem.db", backend="auto", redis_client=fake, **kw)

    def test_claim_first_is_true(self, tmp_path):
        store = self._store(tmp_path, FakeRedis())
        key = store.build_key("s1", "update_order", "PO-1")
        assert store.claim(key, "s1", "update_order", "PO-1") is True

    def test_claim_duplicate_is_false(self, tmp_path):
        store = self._store(tmp_path, FakeRedis())
        key = store.build_key("s1", "update_order", "PO-1")
        assert store.claim(key, "s1", "update_order", "PO-1") is True
        assert store.claim(key, "s1", "update_order", "PO-1") is False, "RUNNING 占用中不可重复"

    def test_complete_then_get_result(self, tmp_path):
        store = self._store(tmp_path, FakeRedis())
        key = store.build_key("s1", "update_order", "PO-1")
        store.claim(key, "s1", "update_order", "PO-1")
        store.complete(key, {"ok": True})
        assert store.get_result(key) == {"ok": True}
        assert store.status(key) == "SUCCESS"

    def test_running_not_cached(self, tmp_path):
        store = self._store(tmp_path, FakeRedis())
        key = store.build_key("s1", "update_order", "PO-1")
        store.claim(key, "s1", "update_order", "PO-1")
        assert store.get_result(key) is None, "RUNNING 不缓存结果 → 同 key 可安全等待/重试"

    def test_failed_can_retry(self, tmp_path):
        store = self._store(tmp_path, FakeRedis())
        key = store.build_key("s1", "update_order", "PO-1")
        store.claim(key, "s1", "update_order", "PO-1")
        store.mark_failed(key, "boom")
        assert store.status(key) == "FAILED"
        assert store.claim(key, "s1", "update_order", "PO-1") is True, "FAILED 可重置重试"

    def test_ttl_300s(self, tmp_path):
        fake = FakeRedis()
        store = self._store(tmp_path, fake)
        key = store.build_key("s1", "update_order", "PO-1")
        store.claim(key, "s1", "update_order", "PO-1")
        assert DEFAULT_TTL == 300
        assert fake._ttl[f"scm:idem:{key}"] == 300

    def test_build_key_deterministic(self, tmp_path):
        k1 = IdempotencyStore.build_key("s1", "op", "t")
        k2 = IdempotencyStore.build_key("s1", "op", "t")
        k3 = IdempotencyStore.build_key("s1", "op", "other")
        assert k1 == k2
        assert k1 != k3
        assert len(k1) == 64  # sha256 hex

    def test_redis_corrupt_payload_ignored(self, tmp_path):
        """Redis 值不是合法 JSON → get/status 视作无缓存（不炸，可安全重试）。"""
        fake = FakeRedis()
        store = self._store(tmp_path, fake)
        key = store.build_key("s1", "op", "t")
        fake._store[f"scm:idem:{key}"] = "not-json"
        assert store.get_result(key) is None
        assert store.status(key) is None


class TestSqliteFallback:
    def test_redis_down_read_falls_back_to_sqlite(self, tmp_path):
        fake = FakeRedis(available=False)
        store = IdempotencyStore(tmp_path / "s.db", backend="auto", redis_client=fake)
        assert store.resolve_backend(risk="read") is store._sqlite

    def test_sqlite_full_flow(self, tmp_path):
        store = IdempotencyStore(tmp_path / "s.db", backend="sqlite")
        key = store.build_key("s1", "op", "t")
        assert store.claim(key, "s1", "op", "t") is True
        assert store.claim(key, "s1", "op", "t") is False
        store.complete(key, {"done": 1})
        assert store.get_result(key) == {"done": 1}

    def test_sqlite_failed_retry(self, tmp_path):
        store = IdempotencyStore(tmp_path / "f.db", backend="sqlite")
        key = store.build_key("s1", "op", "t")
        assert store.claim(key, "s1", "op", "t") is True
        store.mark_failed(key, "err")
        assert store.claim(key, "s1", "op", "t") is True, "FAILED 可重置重试"

    def test_write_fail_closed(self, tmp_path):
        fake = FakeRedis(available=False)
        store = IdempotencyStore(tmp_path / "w.db", backend="auto", redis_client=fake)
        with pytest.raises(IdemUnavailableError) as ei:
            store.resolve_backend(risk="write")
        assert ei.value.error_code == "IDEM_UNAVAILABLE"

    def test_forced_redis_backend_down_fails_open(self, tmp_path, capsys):
        """backend="redis" 强制 Redis + 挂 + 读 → 仍 fail-open 降级 sqlite（打日志）。"""
        fake = FakeRedis(available=False)
        store = IdempotencyStore(tmp_path / "r.db", backend="redis", redis_client=fake)
        assert store.resolve_backend(risk="read") is store._sqlite
        assert "fail-open" in capsys.readouterr().out


class TestExecuteIdempotent:
    def test_first_execute_runs(self, tmp_path):
        store = IdempotencyStore(tmp_path / "e.db", backend="sqlite")
        calls = {"n": 0}

        def _op():
            calls["n"] += 1
            return {"ok": True}

        result, hit = execute_idempotent(store, "s1", "op", "t", _op)
        assert result == {"ok": True} and hit is False
        assert calls["n"] == 1

    def test_repeat_returns_cached(self, tmp_path):
        store = IdempotencyStore(tmp_path / "e.db", backend="sqlite")
        calls = {"n": 0}

        def _op():
            calls["n"] += 1
            return {"ok": True}

        execute_idempotent(store, "s1", "op", "t", _op)
        result, hit = execute_idempotent(store, "s1", "op", "t", _op)
        assert result == {"ok": True} and hit is True
        assert calls["n"] == 1, "幂等命中不得二次执行"

    def test_failure_marks_failed_and_can_retry(self, tmp_path):
        store = IdempotencyStore(tmp_path / "e.db", backend="sqlite")
        calls = {"n": 0}

        def _op():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        with pytest.raises(RuntimeError):
            execute_idempotent(store, "s1", "op", "t", _op)
        assert store.status(store.build_key("s1", "op", "t")) == "FAILED"
        result, hit = execute_idempotent(store, "s1", "op", "t", _op)
        assert result == {"ok": True} and hit is False, "FAILED 后可重试并成功"

    def test_write_risk_rejected_when_redis_down(self, tmp_path):
        fake = FakeRedis(available=False)
        store = IdempotencyStore(tmp_path / "e.db", backend="auto", redis_client=fake)
        with pytest.raises(IdemUnavailableError):
            execute_idempotent(store, "s1", "op", "t", lambda: {"ok": True})

    def test_claim_failed_spin_times_out(self, tmp_path):
        """claim 被占用且自旋取不到结果 → 抛错（不无限等待/不重复执行）。"""
        store = IdempotencyStore(tmp_path / "x.db", backend="sqlite")
        key = store.build_key("s1", "op", "t")
        store.claim(key, "s1", "op", "t")  # RUNNING 占用中
        with pytest.raises(RuntimeError):
            execute_idempotent(store, "s1", "op", "t", lambda: {"ok": True})
