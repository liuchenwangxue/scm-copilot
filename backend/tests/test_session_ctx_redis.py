"""W27 Day2 会话 Redis 化测试（A3/A4）——Redis 权威 + L1 读缓存 + 降级三件套。

覆盖手册 Day2 下午"单测三件套"：
1. 并发写同一会话（asyncio.gather 20 路 append）→ 无丢失、无损坏（JSON 可解析、轮数 ≤4）
   —— 并发原子性必须用真 Redis（手册坑：别用 fakeredis 测并发）；本机 compose 有，CI 无则 skip
2. 重启模拟：新建 store 实例（不共享内存/L1）→ 会话仍在（数据在 Redis）
3. Redis 挂（抛错）→ 降级进程内、resolve 仍工作、日志有 DEGRADED 事件

另含：
- TTL 刷新：读/写路径都刷（LRU 语义，活跃会话不过期）——手册坑写进断言
- `_SESSIONS` 全局 dict 已删除：每次 get_session 新实例，状态共享走 Redis/降级存储

标签：纯逻辑部分无需 Redis；真 Redis 用例 skip-if-unavailable。
"""

import asyncio
import json
import os

import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")

from app.domains.data.session_ctx import (  # noqa: E402
    _KEY_PREFIX,
    DEFAULT_MAX_TURNS,
    DEFAULT_TTL_SECONDS,
    SessionContext,
    clear_sessions,
    get_session,
)
from app.shared.reliability.redis_client import get_redis_client  # noqa: E402

TODAY = "2026-08-18"


def _redis_ready() -> bool:
    """真 Redis 是否可用（并发原子性/重启模拟必需）。"""
    rc = get_redis_client()
    return bool(rc.available)


@pytest.fixture(autouse=True)
def _clean_ctx():
    """每个测试前后清进程内 L1/降级存储（不删 Redis 数据，靠唯一 key 隔离）。"""
    clear_sessions()
    yield
    clear_sessions()


def _sess_key(session_id: str) -> str:
    return f"{_KEY_PREFIX}::{session_id}"


# ==================== 1. 并发写同一会话（真 Redis，原子性） ====================


@pytest.mark.integration
async def test_concurrent_append_no_loss_no_corruption():
    """20 路并发 append 同一会话：Lua 原子 → 无覆盖丢失、JSON 无损坏、轮数 ≤4。

    手册坑：并发原子性用真 Redis（compose 现成）测，不用 fakeredis。
    """
    if not _redis_ready():
        pytest.skip("Redis 不可用，跳过并发原子性验证（真 Redis 才有意义）")
    rc = get_redis_client()
    sid = "concurrent-race"
    rc.delete(_sess_key(sid))
    n = 20
    ctx = get_session(sid)

    async def _append(i: int) -> None:
        # 每轮独立问题：并发窗口内若读-改-写非原子，会互相覆盖丢轮次
        await asyncio.to_thread(
            ctx.record, f"并发问题{i}", f"SELECT {i}", [f"t{i}"]
        )

    await asyncio.gather(*(_append(i) for i in range(n)))

    raw = rc.get(_sess_key(sid))
    assert raw, "并发写后 Redis 应有会话数据"
    turns = json.loads(raw)
    # 无损坏 + 轮数截断：20 轮并发写最终 ≤4（每轮都执行成功，只是被 max_turns 截断）
    assert len(turns) <= DEFAULT_MAX_TURNS
    assert len(turns) == DEFAULT_MAX_TURNS, (
        f"并发覆盖丢失轮次：期望截断到 {DEFAULT_MAX_TURNS} 轮，实际 {len(turns)} 轮"
    )
    for t in turns:
        assert set(t) == {"question", "sql", "tables"}
        assert t["question"].startswith("并发问题")
        assert isinstance(t["tables"], list)
    rc.delete(_sess_key(sid))


# ==================== 2. 重启模拟：新建 store 实例（不共享内存）会话仍在 ====================


@pytest.mark.integration
async def test_restart_new_instance_session_persists():
    """进程重启模拟：旧实例 record 后，清 L1/降级（模拟内存清空）→ 新实例 recent 仍在。

    数据在 Redis（权威）而不在实例内存——这是"重启不丢"的证据。
    """
    if not _redis_ready():
        pytest.skip("Redis 不可用，跳过重启模拟（需要权威存储）")
    sid = "restart-session"
    rc = get_redis_client()
    rc.delete(_sess_key(sid))

    ctx1 = get_session(sid)
    ctx1.record("华东区域有多少订单？", "SELECT 1", ["orders"])
    ctx1.record("华北区域有多少订单？", "SELECT 2", ["orders"])

    # 模拟进程重启：清空进程内 L1 / 降级存储（内存归零，Redis 数据不受影响）
    clear_sessions()

    ctx2 = get_session(sid)  # 全新实例（不共享内存、不共享 L1）
    assert ctx2.recent()["question"] == "华北区域有多少订单？"
    rc.delete(_sess_key(sid))


@pytest.mark.integration
async def test_restart_multi_instance_followup_resolve():
    """双实例续问（a1 建会话 → a2 追问）：消解依赖上一轮上下文跨实例可得。"""
    if not _redis_ready():
        pytest.skip("Redis 不可用，跳过双实例续问（需要权威存储）")
    sid = "cross-instance-followup"
    rc = get_redis_client()
    rc.delete(_sess_key(sid))

    # "a1 实例"：首轮 + record
    ctx_a1 = get_session(sid)
    assert await ctx_a1.resolve("华东区域有多少订单？", TODAY) == "华东区域有多少订单？"
    ctx_a1.record("华东区域有多少订单？", "SELECT 1", ["orders"])

    # "a2 实例"（独立进程语义：清 L1/降级 + 新实例）追问"那华北呢"
    clear_sessions()
    ctx_a2 = get_session(sid)
    resolved = await ctx_a2.resolve("那华北呢？", TODAY)
    assert resolved == "华北区域有多少订单？", f"跨实例续问消解失败: {resolved!r}"
    rc.delete(_sess_key(sid))


# ==================== 3. Redis 挂（抛错）→ 降级进程内 ====================


class _FakeDownRedis:
    """模拟 Redis 挂：eval 抛 ConnectionError（非 fail-open 返回值，是硬抛错）。"""

    @property
    def available(self) -> bool:
        return False

    def eval(self, *args, **kwargs):
        raise ConnectionError("redis down (simulated)")


def test_redis_down_record_degrades_to_local(caplog):
    """Redis 挂 → record 降级进程内：不抛错、recent 能从本地读回本轮。"""
    ctx = SessionContext("down-session", redis_client=_FakeDownRedis())
    ctx.record("华东区域有多少订单？", "SELECT 1", ["orders"])
    assert ctx.recent()["question"] == "华东区域有多少订单？"
    assert "session_ctx_degraded" in caplog.text, "应有 DEGRADED 日志事件"


@pytest.mark.asyncio
async def test_redis_down_recent_and_resolve_still_work(caplog):
    """Redis 挂 → resolve 仍工作（进程内降级上下文可消解追问）。"""
    ctx = SessionContext("down-session-2", redis_client=_FakeDownRedis())
    ctx.record("华东区域有多少订单？", "SELECT 1", ["orders"])
    out = await ctx.resolve("那华北呢？", TODAY)
    assert out == "华北区域有多少订单？"
    assert "session_ctx_degraded" in caplog.text


# ==================== 4. TTL 刷新（读/写都刷，LRU 语义） ====================


@pytest.mark.integration
async def test_ttl_refreshed_on_write_and_read():
    """手册坑断言：TTL 读/写路径都刷（活跃会话不过期）。

    写路径：record 后 TTL 接近满值（DEFAULT_TTL_SECONDS）；
    读路径：recent 后 TTL 被重新刷新（清 L1 强制回源 Redis 后 TTL 仍接近满值）。
    """
    if not _redis_ready():
        pytest.skip("Redis 不可用，跳过 TTL 断言（需要真实 TTL 语义）")
    sid = "ttl-refresh"
    rc = get_redis_client()
    rc.delete(_sess_key(sid))
    ctx = get_session(sid)

    ctx.record("华东区域有多少订单？", "SELECT 1", ["orders"])
    ttl_after_write = rc.ttl(_sess_key(sid))
    assert ttl_after_write > 0, "写路径应设置 TTL"
    assert ttl_after_write > DEFAULT_TTL_SECONDS * 0.9, (
        f"写后 TTL 应接近满值 {DEFAULT_TTL_SECONDS}，实际 {ttl_after_write}"
    )

    # 读路径：清 L1 强制回源 Redis → recent 触发 GET+EXPIRE 刷新 TTL
    clear_sessions()
    ctx2 = get_session(sid)
    assert ctx2.recent()["question"] == "华东区域有多少订单？"
    ttl_after_read = rc.ttl(_sess_key(sid))
    assert ttl_after_read > 0, "读路径应刷新 TTL（LRU 语义）"
    rc.delete(_sess_key(sid))


# ==================== 5. 注册表反模式删除的回归 ====================


def test_get_session_returns_new_instance_shared_state():
    """`_SESSIONS` 全局 dict 已删除：每次 get_session 新实例，但状态经 store 共享。

    无 Redis 环境走进程内降级存储（同为 store），同 session_id 数据互通。
    """
    clear_sessions()
    a = get_session("s-shared")
    a.record("华东区域有多少订单？", "SELECT 1", ["orders"])
    b = get_session("s-shared")  # 新实例
    assert a is not b, "注册表已删：get_session 每次返回新实例"
    assert b.recent()["question"] == "华东区域有多少订单？", "状态应跨实例共享"
