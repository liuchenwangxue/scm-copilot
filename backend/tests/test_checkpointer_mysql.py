"""W23 Day5 LangGraph checkpointer MySQL 化集成测试（integration，需 MySQL + Redis 无关）。

验证点：
- AsyncMySaver 可创建（collation 修复后无 1267 报错）
- aput / aget_tuple 读写往返正常（模拟 HITL 断点持久化）
- thread 隔离：不同 thread_id 互不干扰；删除后不可读
"""

import uuid

import pytest

from app.domains.ops.persistence import get_mysql_checkpointer, reset_checkpointer


@pytest.fixture(autouse=True)
def _isolated_loop_saver():
    """每个测试独立事件循环：AsyncMySaver 连接绑定创建它的 loop，
    测试间必须重建单例（persistence 模块级缓存跨 loop 会 `NoneType.send` 报错）。"""
    reset_checkpointer()
    yield
    reset_checkpointer()


def _thread() -> str:
    return f"test-cp-{uuid.uuid4().hex[:8]}"


def _checkpoint(cp_id: str) -> dict:
    return {
        "v": 4,
        "ts": "2026-08-21T10:00:00.000000+00:00",
        "id": cp_id,
        "channel_values": {
            "__start__": {"message": "把 PO-0002 的金额改成 9500"},
            "intent": {"intent": "update_order"},
        },
        "channel_versions": {
            "__start__": "00000000000000000000000000000001.0.1",
            "intent": "00000000000000000000000000000002.0.2",
        },
        "versions_seen": {},
        "updated_channels": ["__start__", "intent"],
    }


@pytest.mark.integration
async def test_mysql_checkpointer_roundtrip():
    saver = await get_mysql_checkpointer()
    thread_id = _thread()
    cp_id = f"{thread_id}-cp1"
    cfg = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": cp_id}
    }
    await saver.aput(cfg, _checkpoint(cp_id), {"source": "input", "step": -1, "parents": {}},
                     _checkpoint(cp_id)["channel_versions"])
    try:
        t = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        assert t is not None, "写入后可读取"
        assert t.checkpoint["id"] == cp_id
        assert t.checkpoint["channel_values"]["intent"]["intent"] == "update_order"
        # 非原始类型（dict）经 checkpoint_blobs 恢复
        assert t.checkpoint["channel_values"]["__start__"]["message"] == "把 PO-0002 的金额改成 9500"
    finally:
        await saver.adelete_thread(thread_id)


@pytest.mark.integration
async def test_mysql_checkpointer_thread_isolation():
    saver = await get_mysql_checkpointer()
    t1, t2 = _thread(), _thread()
    try:
        await saver.aput(
            {"configurable": {"thread_id": t1, "checkpoint_ns": "", "checkpoint_id": f"{t1}-a"}},
            _checkpoint(f"{t1}-a"), {}, _checkpoint(f"{t1}-a")["channel_versions"])
        assert await saver.aget_tuple({"configurable": {"thread_id": t1}}) is not None
        assert await saver.aget_tuple({"configurable": {"thread_id": t2}}) is None, "thread 隔离"
    finally:
        await saver.adelete_thread(t1)
        await saver.adelete_thread(t2)
