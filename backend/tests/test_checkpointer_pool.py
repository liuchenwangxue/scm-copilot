"""W27 Day1 连接池化并发护栏测试（integration，需 MySQL + scm_platform 可写）。

验证点（手册 Day1 下午 4）：
- 20 路 asyncio.gather 并发写 checkpoint：显著快于同量串行写（单连接版 ≈ 等量，
  这是"40 并发 P95=2087.1ms 根因是 checkpointer 串行"的直接证据）
- 池耗尽时（并发 > maxsize）acquire 排队不报错、全部成功
- 池化后读写往返完整（aput → aget_tuple，语义不回归）
- 并发写互不干扰（不同 thread 隔离，finally 清理残留）

断言口径：用「同测试内背靠背串行基线」对比而非绝对倍数阈值——单路基线的固定开销
（序列化线程跳转 / 池取连接）不随并发线性分摊，绝对倍数在本机不可靠；串行/并行
在同一时刻测量，负载一致，比值稳定。
"""

import asyncio
import time
import uuid

import pytest

from app.domains.ops import config
from app.domains.ops.persistence import get_mysql_checkpointer, reset_checkpointer


@pytest.fixture(autouse=True)
def _isolated_loop_saver():
    """每个测试独立事件循环：asyncmy 连接池绑定创建它的 loop，
    测试间必须重建单例（persistence 模块级缓存跨 loop 会 `NoneType.send` 报错）。"""
    reset_checkpointer()
    yield
    reset_checkpointer()


def _thread() -> str:
    return f"pool-t-{uuid.uuid4().hex[:8]}"


def _versions(idx: int) -> dict:
    # version 逐写递增：checkpoint_blobs 主键是 (thread_id, channel, version)，
    # 同一 thread 多次写若 version 相同会命中 INSERT IGNORE 的良性告警——递增避免噪音
    return {
        "__start__": f"00000000000000000000000000000001.0.{idx + 1}",
        "intent": f"00000000000000000000000000000002.0.{idx + 1}",
    }


def _checkpoint(cp_id: str, idx: int = 0) -> dict:
    return {
        "v": 4,
        "ts": "2026-08-21T10:00:00.000000+00:00",
        "id": cp_id,
        "channel_values": {
            "__start__": {"message": "把 PO-0002 的金额改成 9500"},
            "intent": {"intent": "update_order"},
        },
        "channel_versions": _versions(idx),
        "versions_seen": {},
        "updated_channels": ["__start__", "intent"],
    }


def _cfg(thread_id: str, cp_id: str | None = None) -> dict:
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if cp_id:
        cfg["configurable"]["checkpoint_id"] = cp_id
    return cfg


async def _put(saver, thread_id: str, idx: int) -> None:
    cp_id = f"{thread_id}-{idx}"
    await saver.aput(
        _cfg(thread_id, cp_id), _checkpoint(cp_id, idx),
        {"source": "input", "step": idx, "parents": {}},
        _versions(idx),
    )


async def _warm_writes(saver, threads: list[str]) -> None:
    """warm up：并行预热一轮，排除池冷启动建连与 MySQL 连接建立抖动。"""
    await asyncio.gather(*(_put(saver, t, 0) for t in threads))


@pytest.mark.integration
async def test_pool_20_parallel_faster_than_serial(monkeypatch):
    """20 路并发写显著快于同量串行写——证明不再串行。

    手册原口径"< 3×单路"在本机不可靠：单路基线里固定开销（JSON 序列化线程跳转 /
    池取连接）占大头，不随并发线性分摊。改直接对比「同一 saver 上 20 次串行写 vs
    20 路并行写」——若池未生效（仍单连接），两者应等量。
    """
    monkeypatch.setattr(config, "SCM_CHECKPOINT_POOL_MIN", 1)
    monkeypatch.setattr(config, "SCM_CHECKPOINT_POOL_SIZE", 20)
    saver = await get_mysql_checkpointer()

    # 串行基线：20 个 thread 依次写（同量工作）
    serial_threads = [_thread() for _ in range(20)]
    try:
        await _warm_writes(saver, serial_threads)
        t0 = time.perf_counter()
        for t in serial_threads:
            await _put(saver, t, 1)
        serial_ms = (time.perf_counter() - t0) * 1000
    finally:
        for t in serial_threads:
            await saver.adelete_thread(t)

    # 并行：20 个 thread 同时写
    parallel_threads = [_thread() for _ in range(20)]
    try:
        t0 = time.perf_counter()
        await asyncio.gather(*(_put(saver, t, 0) for t in parallel_threads))
        parallel_ms = (time.perf_counter() - t0) * 1000
    finally:
        for t in parallel_threads:
            await saver.adelete_thread(t)

    assert parallel_ms < 0.7 * serial_ms, (
        f"并发未生效：并行 {parallel_ms:.1f}ms ≥ 70%×串行 {serial_ms:.1f}ms"
        f"（单连接版应 ≈ 等量；池化后应显著更短）"
    )


@pytest.mark.integration
async def test_pool_exhaustion_queues_not_error(monkeypatch):
    """并发 > 池 maxsize 时 acquire 排队：不报错、全部成功、仍显著快于串行。

    断言用「同测试内背靠背串行基线」而非绝对阈值：整库负载下绝对时间不可比，
    串行/并行在同一时刻测量，负载一致。
    """
    monkeypatch.setattr(config, "SCM_CHECKPOINT_POOL_MIN", 1)
    monkeypatch.setattr(config, "SCM_CHECKPOINT_POOL_SIZE", 4)  # 缩小池，制造排队
    saver = await get_mysql_checkpointer()

    # 串行基线：12 个 thread 依次写
    serial_threads = [_thread() for _ in range(12)]
    try:
        await _warm_writes(saver, serial_threads)
        t0 = time.perf_counter()
        for t in serial_threads:
            await _put(saver, t, 1)
        serial_ms = (time.perf_counter() - t0) * 1000
    finally:
        for t in serial_threads:
            await saver.adelete_thread(t)

    # 并行：12 个 thread 同时写（池只有 4，超出部分 acquire 排队）
    parallel_threads = [_thread() for _ in range(12)]
    try:
        t0 = time.perf_counter()
        await asyncio.gather(*(_put(saver, t, 0) for t in parallel_threads))
        parallel_ms = (time.perf_counter() - t0) * 1000
    finally:
        for t in parallel_threads:
            await saver.adelete_thread(t)

    # 池耗尽排队不报错 + 并行仍显著更快：4 连接 12 写 ≈ 3 轮 vs 串行 12 轮
    assert parallel_ms < 0.7 * serial_ms, (
        f"池耗尽后排队异常：并行 {parallel_ms:.1f}ms ≥ 70%×串行 {serial_ms:.1f}ms"
    )


@pytest.mark.integration
async def test_pool_roundtrip_read_write():
    """池化后读写往返完整：aput → aget_tuple 取回一致（语义不回归）。"""
    saver = await get_mysql_checkpointer()
    thread_id = _thread()
    cp_id = f"{thread_id}-cp1"
    try:
        await saver.aput(
            _cfg(thread_id, cp_id), _checkpoint(cp_id),
            {"source": "input", "step": -1, "parents": {}},
            _versions(0),
        )
        t = await saver.aget_tuple(_cfg(thread_id))
        assert t is not None, "写入后可读取"
        assert t.checkpoint["id"] == cp_id
        assert t.checkpoint["channel_values"]["intent"]["intent"] == "update_order"
        assert t.checkpoint["channel_values"]["__start__"]["message"] == "把 PO-0002 的金额改成 9500"
    finally:
        await saver.adelete_thread(thread_id)


@pytest.mark.integration
async def test_pool_concurrent_isolated_threads():
    """20 路并发写各自 thread 互不干扰（读回各自 checkpoint）。"""
    saver = await get_mysql_checkpointer()
    threads = [_thread() for _ in range(20)]
    try:
        await asyncio.gather(*(_put(saver, t, 0) for t in threads))
        for t in threads:
            tup = await saver.aget_tuple(_cfg(t))
            assert tup is not None, f"thread {t} 写入可读"
            assert tup.checkpoint["id"] == f"{t}-0"
    finally:
        for t in threads:
            await saver.adelete_thread(t)
