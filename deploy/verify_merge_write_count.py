"""★ W27 D7：量化 durability="exit" 合并写效果——同线程 async vs exit 的 checkpoint 写入行数。

方法：直接查 MySQL checkpoints/checkpoint_writes 表，对比一次图执行（低危直通 4 节点）
在两种 durability 下的行数增量。证明"每 super-step 写 1 次 → 每次执行写 1 次"。
"""
import asyncio
import contextlib
import uuid

import asyncmy

from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver


async def _count(pool, sql: str) -> int:
    async with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            await cur.execute(sql)
            row = await cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            await cur.close()


async def main() -> None:
    from app.platform.settings import settings
    parsed = AsyncMySaver.parse_conn_string(settings.platform_dsn)
    pool = await asyncmy.create_pool(**parsed, autocommit=True)

    from app.domains.ops.agent.graph import get_biz_graph
    graph = await get_biz_graph()

    for dur in ("async", "exit"):
        sid = f"d7-cnt-{uuid.uuid4().hex[:6]}"
        cfg = {"configurable": {"thread_id": sid}}
        c0 = await _count(pool, "SELECT COUNT(*) FROM scm_platform.checkpoints")
        w0 = await _count(pool, "SELECT COUNT(*) FROM scm_platform.checkpoint_writes")
        async for _ in graph.astream(
                {"message": "查一下订单 PO-0001 的状态", "session_id": sid},
                cfg, stream_mode="updates", durability=dur):
            pass
        c1 = await _count(pool, "SELECT COUNT(*) FROM scm_platform.checkpoints")
        w1 = await _count(pool, "SELECT COUNT(*) FROM scm_platform.checkpoint_writes")
        print(f"durability={dur}: checkpoints +{c1-c0}, writes +{w1-w0}", flush=True)
        # 清理该线程
        with contextlib.suppress(Exception):
            await graph.adelete_thread(sid)

    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
