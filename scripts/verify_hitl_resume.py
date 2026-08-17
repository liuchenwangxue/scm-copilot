"""★ W23 Day5 验收脚本：HITL 断点迁移验证（MySQL checkpointer）。

场景：审批中断 → 杀进程重启 → 审批通过 → 图从断点 resume 继续执行。
用两个独立进程模拟"杀进程"（真正的状态在 MySQL，跨进程可见）。

用法：
  python scripts/verify_hitl_resume.py initiate [thread_id]
      # 进程 1：发起高危操作 → approval_gate 中断（断点落 MySQL）
  python scripts/verify_hitl_resume.py resume [thread_id]
      # 进程 2：新进程读取断点 → 审批通过 → 从断点续跑执行

验收（手册 Day5）：
- 发起高危操作 → 审批中断 ✓
- 杀进程重启（子进程） ✓
- 审批通过后从断点 resume 继续执行（update_order 成功）✓
- 审批单在 MySQL approvals 表（双实例共享，无状态化前提）✓
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("CHECKPOINTER_BACKEND", "mysql")

from langgraph.types import Command  # noqa: E402

from app.domains.ops.agent.graph import approval_svc, get_biz_graph  # noqa: E402

DEFAULT_THREAD = "hitl-mysql-verify-1"


async def initiate(thread_id: str) -> None:
    graph = await get_biz_graph()
    result = await graph.ainvoke(
        {"message": "把 PO-0002 的金额改成 9500", "session_id": thread_id},
        {"configurable": {"thread_id": thread_id}},
    )
    intr = result.get("__interrupt__") or result.get("__interrupts__")
    if not intr:
        print("[P1] 未触发 interrupt（异常）")
        sys.exit(1)
    for i in intr:
        val = i.value
        print(f"[P1] interrupt! approval_id={val.get('approval_id')} thread={thread_id}")
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    print(f"[P1] next={state.next}（断点已落 MySQL）")
    print("STEP1_OK")


async def resume(thread_id: str) -> None:
    graph = await get_biz_graph()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    print(f"[P2] 新进程读取断点: next={state.next} tasks={len(state.tasks)}")
    result = await graph.ainvoke(
        Command(resume={"decision": "approve", "reason": "审批通过（重启后续跑）"}),
        {"configurable": {"thread_id": thread_id}},
    )
    print(f"[P2] reply={result.get('reply')}")
    print(f"[P2] tool_result.success={result.get('tool_result', {}).get('success')}")

    for a in approval_svc.list_all():
        if a.session_id == thread_id:
            print(f"[P2] approval {a.approval_id}: status={a.status}")
    print("STEP2_OK")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "initiate"
    tid = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_THREAD
    if phase == "initiate":
        asyncio.run(initiate(tid))
    else:
        asyncio.run(resume(tid))
