"""W25 Day1 调度基座单测：leader 锁互斥 / owner 校验 / TTL 重抢（纯逻辑，无 DB）。

覆盖手册 Day1 下午任务 4：
- 两实例并发触发同一任务（模拟）→ 一个执行一个 Skip
- owner 校验：非 owner 释放无效（防误删他人刚抢到的锁）
- TTL 过期后可重抢（持有者崩溃未释放 → 锁超时自动让出）

用内存 FakeRedis 模拟 Redis SETNX/校验删除/TTL 过期，不依赖外部服务（CI 可跑）。
"""

import asyncio
import time

import pytest

from app.platform.scheduler.leader import SkipResult, leader_lock


class FakeRedis:
    """内存版 Redis 客户端（仅实现 leader_lock 用到的原语）。

    - set_nx: SET key value NX EX ttl（key 不存在才设置；含过期时间）
    - delete_if_equals: GET 校验 owner 一致才 DEL（Lua 原子语义）
    - available: 恒 True（fail-open 分支单独测）
    - 支持 TTL 过期：操作前惰性清除过期键
    """

    def __init__(self):
        self._store: dict[str, tuple[str, float | None]] = {}
        self.available = True
        self._now = time.time

    def _expire(self, key: str) -> None:
        item = self._store.get(key)
        if item and item[1] is not None and self._now() > item[1]:
            del self._store[key]

    def set_nx(self, key: str, value: str, ex: int | None = None) -> bool:
        self._expire(key)
        if key in self._store:
            return False
        self._store[key] = (value, self._now() + ex if ex is not None else None)
        return True

    def delete_if_equals(self, key: str, expected: str) -> bool:
        self._expire(key)
        item = self._store.get(key)
        if item and item[0] == expected:
            del self._store[key]
            return True
        return False

    def get(self, key: str) -> str | None:
        self._expire(key)
        item = self._store.get(key)
        return item[0] if item else None


@pytest.mark.asyncio
async def test_two_instances_concurrent_only_one_runs():
    """两实例并发触发同一任务 → 恰一个执行、一个 Skip（互斥核心语义）。"""
    fake = FakeRedis()
    executed: list[str] = []

    @leader_lock("daily_brief", ttl=30, redis_client=fake)
    async def task():
        executed.append("run")
        await asyncio.sleep(0.05)  # 模拟任务耗时，给第二个实例抢锁窗口
        return {"ok": True}

    results = await asyncio.gather(task(), task())
    assert len(executed) == 1, f"两实例都执行了：{executed}"
    assert [type(r) for r in results].count(SkipResult) == 1
    skip = next(r for r in results if isinstance(r, SkipResult))
    assert "another instance" in skip.reason
    # 锁已释放：store 里没有残留
    assert fake.get("lock:job:daily_brief") is None


def test_owner_release_only_own_lock():
    """owner 校验原语：非 owner 的 DELETE 无效（Lua delete_if_equals 语义）。"""
    fake = FakeRedis()
    assert fake.set_nx("lock:job:kb_increment_sync", "owner-a", ex=30)
    # 错误 owner 释放 → 无效，锁还在
    assert fake.delete_if_equals("lock:job:kb_increment_sync", "wrong-owner") is False
    assert fake.get("lock:job:kb_increment_sync") == "owner-a"
    # 正确 owner 释放 → 有效，锁删除
    assert fake.delete_if_equals("lock:job:kb_increment_sync", "owner-a") is True
    assert fake.get("lock:job:kb_increment_sync") is None


@pytest.mark.asyncio
async def test_ttl_expiry_allows_reacquire_after_crash():
    """持有者崩溃未释放（锁被占用）→ TTL 过期后可重抢（防死锁）。"""
    fake = FakeRedis()
    executed: list[str] = []

    @leader_lock("eval_nightly", ttl=1, redis_client=fake)
    async def task():
        executed.append("run")
        return "ok"

    # 模拟"另一实例崩溃"：锁被占用且不会被释放（进程没了，finally 跑不到）
    fake.set_nx("lock:job:eval_nightly", "crashed-owner", ex=1)
    # 锁未过期前，本实例抢不到
    assert isinstance(await task(), SkipResult)
    assert len(executed) == 0
    # 等 TTL 过期（锁自动消失）后，本实例可以抢到执行
    await asyncio.sleep(1.1)
    assert await task() == "ok"
    assert len(executed) == 1
    # 执行完正常释放，无残留
    assert fake.get("lock:job:eval_nightly") is None


@pytest.mark.asyncio
async def test_fail_open_when_redis_unavailable():
    """Redis 不可用 → fail-open 放行（锁退化为无锁，任务幂等兜底）。"""
    fake = FakeRedis()
    fake.available = False
    executed: list[str] = []

    @leader_lock("audit_archive", ttl=30, redis_client=fake)
    async def task():
        executed.append("run")
        return "ok"

    # 并发两个实例：Redis 挂了也应全部执行（宁可多跑，不可卡死）
    results = await asyncio.gather(task(), task())
    assert results == ["ok", "ok"]
    assert len(executed) == 2
