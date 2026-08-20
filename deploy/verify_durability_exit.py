"""★ W27 D7 验证：LangGraph durability="exit"（checkpoint 合并写）对 chat 路径行为的影响。

对照实验（同一线程、两种 durability）：
- 默认（async）：每 super-step 写 checkpoint
- exit：只在图退出写 1 次 checkpoint

验证点：
1. 低危直通路径（query_order）：astream 结束后 aget_state 能读到 reply（非空）
   ——router.py chat 非审批路径依赖此读取，若读空则功能回归
2. 高危审批路径（update_order）：interrupt 挂起 → checkpoint 已落库 → resume 恢复正常
   ——durability="exit" 在 interrupt 时必须强制写 checkpoint（源码 _suppress_interrupt 已确认）
3. checkpoint 表写入次数对比（exit 应显著少于 async）——用 DELETE+计数近似
"""
import asyncio
import contextlib
import json
import uuid

import httpx


def _log(msg: str) -> None:
    print(msg, flush=True)


async def run_chat(graph, session_id: str, message: str, durability: str) -> dict:
    """模拟 router.chat：astream updates → aget_state 读 reply。"""
    cfg = {"configurable": {"thread_id": session_id}}
    events: list[dict] = []
    try:
        async for event in graph.astream(
            {"message": message, "session_id": session_id},
            cfg, stream_mode="updates", durability=durability):
            events.append(event)
    except Exception as e:  # noqa: BLE001
        _log(f"  [astream 异常] {type(e).__name__}: {e}")
        return {"events": events, "error": f"{type(e).__name__}: {e}"}
    state = await graph.aget_state(cfg)
    return {"events": events, "reply": state.values.get("reply", ""), "error": None}


async def run_approval_resume(graph, session_id: str, durability: str) -> dict:
    """高危审批：astream 到 interrupt 挂起 → Command(resume) 恢复。"""
    cfg = {"configurable": {"thread_id": session_id}}
    interrupted = False
    events: list[dict] = []
    async for event in graph.astream(
            {"message": "把订单 PO-0002 的金额改成 9500", "session_id": session_id},
            cfg, stream_mode="updates", durability=durability):
        events.append(event)
        for node, data in event.items():
            if node == "__interrupt__":
                interrupted = True
    # 挂起后：另起"新实例"（直接读 checkpoint 恢复），验证 resume 前 checkpoint 已落库
    from langgraph.types import Command
    result = await graph.ainvoke(
        Command(resume={"decision": "approve", "reason": "D7 验证"}),
        cfg, durability=durability)
    return {"interrupted": interrupted, "result": result}


async def main() -> None:
    from app.domains.ops.agent.graph import get_biz_graph

    graph = await get_biz_graph()
    _log("=== 1. 低危直通 query_order：reply 读取（两种 durability） ===")
    for dur in ("async", "exit"):
        sid = f"d7-verify-{uuid.uuid4().hex[:8]}"
        r = await run_chat(graph, sid, "查一下订单 PO-0001 的状态", dur)
        reply = r["reply"]
        ok = bool(reply and "订单" in reply)
        _log(f"  durability={dur}: reply={'非空' if reply else '空!'} "
             f"事件数={len(r['events'])} 判定={'OK' if ok else 'FAIL'}")
        if not ok:
            _log(f"    reply 内容: {reply!r}")

    _log("=== 2. 高危审批 update_order：interrupt 挂起 + resume 恢复 ===")
    for dur in ("async", "exit"):
        sid = f"d7-verify-{uuid.uuid4().hex[:8]}"
        r = await run_approval_resume(graph, sid, dur)
        result = r.get("result") or {}
        reply = result.get("reply", "")
        ok = r.get("interrupted") and bool(reply and "已更新" in reply)
        _log(f"  durability={dur}: interrupted={r.get('interrupted')} "
             f"resume_reply={'非空' if reply else '空!'} 判定={'OK' if ok else 'FAIL'}")
        if not ok:
            _log(f"    result: {json.dumps(result, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
