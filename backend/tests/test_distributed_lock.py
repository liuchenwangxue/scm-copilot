"""W27 Day5 覆盖率冲刺 I：分布式锁独立测试（distributed_lock.py）。

覆盖手册 Day5：
- SETNX 成功/失败路径、owner 校验释放（非 owner 删不掉）、TTL 过期重抢、
  重试窗口内锁释放后抢到
- redis 挂 → 本地兜底（A6：同 key 进程内 threading.Lock 互斥 + 释放后可再抢）
（D3 矩阵已覆盖主路径，本文件补细节分支 + 独立回归）
"""
import pytest

from app.shared.reliability.distributed_lock import DistributedLock, reset_local_locks


class FakeRedis:
    """内存版 Redis：实现锁用的 set_nx / delete_if_equals / get / delete。"""

    def __init__(self, available: bool = True):
        self._store: dict[str, str] = {}
        self.available = available

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

    def get(self, key: str) -> str | None:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.get(key)

    def delete(self, key: str) -> bool:
        if not self.available:
            raise ConnectionError("redis down (simulated)")
        return self._store.pop(key, None) is not None


@pytest.fixture(autouse=True)
def _clean_local_locks():
    """每个测试前后清空本地兜底锁注册表（避免跨用例持锁污染）。"""
    reset_local_locks()
    yield
    reset_local_locks()


class TestAcquire:
    def test_setnx_success(self):
        fake = FakeRedis()
        lk = DistributedLock("order-1", ttl=30, redis_client=fake)
        assert lk.acquire() is True
        assert fake.get("lock:order-1") == lk._owner
        lk.release()

    def test_second_lock_mutual_exclusion(self):
        fake = FakeRedis()
        a = DistributedLock("order-2", ttl=30, redis_client=fake)
        b = DistributedLock("order-2", ttl=30, redis_client=fake)
        assert a.acquire() is True
        assert b.acquire() is False, "同 key 并发只有 1 个成功（SETNX 互斥）"
        a.release()

    def test_release_then_reacquire(self):
        fake = FakeRedis()
        a = DistributedLock("order-3", ttl=30, redis_client=fake)
        b = DistributedLock("order-3", ttl=30, redis_client=fake)
        assert a.acquire() is True
        a.release()
        assert b.acquire() is True

    def test_ttl_expired_reacquire(self):
        fake = FakeRedis()
        a = DistributedLock("order-4", ttl=30, redis_client=fake)
        assert a.acquire() is True
        # TTL 过期 = 键消失（模拟 Redis 自动过期）→ 可重抢，不死锁
        fake.delete("lock:order-4")
        b = DistributedLock("order-4", ttl=30, redis_client=fake)
        assert b.acquire() is True

    def test_retry_eventually_succeeds(self):
        class _ReleaseRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self._calls = 0

            def set_nx(self, key, value, ex=None):
                self._calls += 1
                if self._calls <= 1:
                    return False  # 第一次失败（他人持锁）
                return super().set_nx(key, value, ex=ex)

        fake = _ReleaseRedis()
        lk = DistributedLock("order-5", ttl=30, redis_client=fake, retry_times=2, retry_delay=0)
        assert lk.acquire() is True, "重试窗口内锁释放后应抢到"


class TestReleaseOwnerCheck:
    def test_release_not_holding_does_nothing(self):
        fake = FakeRedis()
        a = DistributedLock("order-6", ttl=30, redis_client=fake)
        b = DistributedLock("order-6", ttl=30, redis_client=fake)
        b.acquire()
        a.release()  # a 未持有 → 直接返回，不删 b 的锁
        assert fake.get("lock:order-6") == b._owner

    def test_stale_owner_release_no_delete(self):
        """旧 owner 释放时校验不匹配 → 不误删新 owner 的锁（TOCTOU 防护）。"""
        fake = FakeRedis()
        a = DistributedLock("order-7", ttl=30, redis_client=fake)
        b = DistributedLock("order-7", ttl=30, redis_client=fake)
        a.acquire()
        fake.delete("lock:order-7")  # a 的锁 TTL 过期
        b.acquire()  # b 重抢成功（键的 owner 变成 b）
        a.release()  # a 用旧 owner 校验 → 不匹配 → 不删 b 的锁
        assert fake.get("lock:order-7") == b._owner

    def test_with_statement_auto_release(self):
        fake = FakeRedis()
        with DistributedLock("order-8", ttl=30, redis_client=fake) as lk:
            assert lk.acquired is True
        assert fake.get("lock:order-8") is None


class TestLocalFallback:
    def test_local_fallback_acquire(self, caplog):
        fake = FakeRedis(available=False)
        lk = DistributedLock("order-9", ttl=30, redis_client=fake)
        assert lk.acquire() is True
        assert lk._local_lock is not None
        assert "lock_local_fallback" in caplog.text, "应有 DEGRADED 日志事件"
        lk.release()

    def test_local_fallback_mutual_exclusion_same_process(self):
        fake = FakeRedis(available=False)
        a = DistributedLock("order-10", ttl=30, redis_client=fake)
        b = DistributedLock("order-10", ttl=30, redis_client=fake)
        assert a.acquire() is True
        assert b.acquire() is False, "Redis 挂时同 key 同进程仍互斥（threading.Lock 兜底）"
        a.release()
        assert b.acquire() is True, "本地锁释放后可再抢"

    def test_local_fallback_release_clears_state(self):
        fake = FakeRedis(available=False)
        lk = DistributedLock("order-11", ttl=30, redis_client=fake)
        lk.acquire()
        lk.release()
        assert lk.acquired is False
        assert lk._local_lock is None
