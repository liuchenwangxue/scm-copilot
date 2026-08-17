"""业务操作域路由（W23 Day4 由 stage3-b `main.py` 改造为平台模块化单体）。

迁移要点（对应手册 Day4）：
- FastAPI app → APIRouter，挂载前缀 `/api/ops`（main.py include_router）
- 认证统一走平台：`require_permission("ops:tool:execute")` / `require_permission("ops:approval:manage")`
  （原 main.py 的 /auth/* 登录、/health、/metrics 由平台基座接管，此处删除）
- 审计：HTTP 级由平台中间件统一落 audit_logs；业务事件（审批动作）保留本域
  AuditLogger（文件级 JSON lines，与 ApprovalService 配套）
- 图在请求 loop 内惰性编译（async 图 + AsyncSqliteSaver 绑定事件循环，沿用 stage3 设计）

SSE 事件契约（与 stage3 一致）：progress / approval_request / message / done / error。
HITL 流程：POST /api/ops/chat 跑到 approval_gate 中断 → 前端展示审批表单 →
POST /api/ops/approval 决策 → Command(resume) 恢复继续跑。
"""

import asyncio
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.types import Command

from app.domains.ops import config
from app.domains.ops.agent.graph import get_biz_graph
from app.domains.ops.security.approval import ApprovalService
from app.domains.ops.security.audit import AuditLogger
from app.domains.ops.tasks.queue import get_queue
from app.platform import rbac
from app.platform.models import User
from app.shared.obs import logger as obs_logger

router = APIRouter(prefix="/api/ops", tags=["ops"])

_log = obs_logger.get_logger("ops")

audit = AuditLogger(config.AUDIT_LOG)
approval_svc = ApprovalService(dsn=config.APPROVAL_DSN, audit=audit)  # ★ Day5：MySQL 平台库

# 图惰性编译（首次请求在运行 loop 内编译并缓存，graph.get_biz_graph 内部有锁）
_biz_graph = None


async def _get_graph():
    global _biz_graph
    if _biz_graph is None:
        _biz_graph = await get_biz_graph()
    return _biz_graph


def _ss(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _progress_text(node: str, data: dict) -> str:
    """节点输出 → 前端进度区可读文本。"""
    if node == "intent":
        i = (data or {}).get("intent") or {}
        return f"识别意图：{i.get('intent')}（来源：{i.get('source')}）"
    if node == "approval_gate":
        a = (data or {}).get("approval") or {}
        status = a.get("status")
        if status == "not_required":
            return "审批门：低危操作，直接执行"
        return f"审批门：{status}"
    if node == "execute":
        r = (data or {}).get("tool_result") or {}
        return f"执行：{'成功' if r.get('success') else '失败'}（降级={r.get('degraded', False)}）"
    if node == "respond":
        return "生成回答"
    return node


@router.post("/chat")
async def chat(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
):
    """业务对话（SSE）：意图识别 → 审批门 → 执行 → 回答。需要权限 ops:tool:execute。

    高危操作在 approval_gate 中断并推送 approval_request 事件（HITL），
    前端展示审批表单后调 POST /api/ops/approval 恢复。
    """
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id") or str(uuid.uuid4())
    runtime_cfg = {"configurable": {"thread_id": session_id}}
    request_id = str(uuid.uuid4())[:12]

    async def event_gen():
        try:
            obs_logger.log_event(_log, "chat_started", request_id=request_id,
                                 session_id=session_id, msg_len=len(message))
            biz_graph = await _get_graph()
            async for event in biz_graph.astream(
                    {"message": message, "session_id": session_id},
                    runtime_cfg, stream_mode="updates"):
                for node, data in event.items():
                    if node == "__interrupt__":
                        for inter in data:
                            val = inter.value
                            if val.get("approval_request"):
                                obs_logger.log_event(_log, "approval_requested",
                                                     request_id=request_id,
                                                     session_id=session_id,
                                                     approval_id=val.get("approval_id"))
                                yield _ss({"type": "approval_request",
                                           "approval_id": val.get("approval_id"),
                                           "form": val.get("form"),
                                           "session_id": session_id})
                        yield _ss({"type": "done"})
                        return
                    yield _ss({"type": "progress", "node": node,
                               "data": {"result": _progress_text(node, data)}})

            # 非审批路径：从 checkpoint 取回复，打字机发送
            state = await biz_graph.aget_state(runtime_cfg)
            reply = state.values.get("reply", "")
            for i in range(0, len(reply), 12):
                yield _ss({"type": "message", "role": "assistant",
                           "content": reply[i:i + 12], "delta": True,
                           "session_id": session_id})
                await asyncio.sleep(0.03)
            yield _ss({"type": "message", "role": "assistant", "content": "",
                       "delta": False, "session_id": session_id})
            yield _ss({"type": "done"})
            obs_logger.log_event(_log, "chat_done", request_id=request_id,
                                 session_id=session_id, status=200)
        except Exception as e:
            obs_logger.log_event(_log, "chat_error", level="error",
                                 request_id=request_id, session_id=session_id,
                                 error=f"{type(e).__name__}: {str(e)[:120]}")
            yield _ss({"type": "error", "error": str(e)})
            yield _ss({"type": "done"})

    response = StreamingResponse(event_gen(), media_type="text/event-stream")
    response.headers["X-Session-Id"] = session_id
    return response


@router.post("/approval")
async def approval_action(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:approval:manage"))],
):
    """审批动作：批准/拒绝高危操作，Command(resume) 恢复图继续执行。

    ★ 平台化：原 stage3 的 admin/operator 角色限制升级为权限码 `ops:approval:manage`
      （仅 admin/operator 拥有，viewer/analyst 403）。
    """
    body = await request.json()
    session_id = body.get("session_id", "")
    approval_id = body.get("approval_id", "")
    decision = body.get("decision", "")
    reason = body.get("reason", "")
    runtime_cfg = {"configurable": {"thread_id": session_id}}

    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be approve|reject"}

    audit.log("approval_action", user=current.username, role=current.tenant_id,
              approval_id=approval_id, decision=decision, reason=reason[:100])
    try:
        biz_graph = await _get_graph()
        result = await biz_graph.ainvoke(
            Command(resume={"decision": decision, "reason": reason}),
            runtime_cfg)
        return {
            "ok": True,
            "approval_id": approval_id,
            "decision": decision,
            "reply": result.get("reply", ""),
            "degraded": result.get("degraded", False),
            "tool_result": result.get("tool_result"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/report")
async def report_async(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
):
    """异步报表：立即返回 {task_id}，worker 后台生成（削峰）。需要权限 ops:tool:execute。

    body: {report_type: inventory|reconciliation, from?, to?}
    返回: {"ok": true, "task_id": "...", "async": true}（队列不可用 → 同步降级返回 result）
    """
    body = await request.json()
    report_type = body.get("report_type", "inventory")
    from_date = body.get("from")
    to_date = body.get("to")
    if report_type not in ("inventory", "reconciliation"):
        return JSONResponse({"ok": False, "error": "report_type 必须是 inventory|reconciliation"},
                            status_code=400)

    audit.log("report_requested", user=current.username,
              report_type=report_type, from_date=from_date, to_date=to_date, mode="async")
    r = get_queue().enqueue_report(report_type, from_date, to_date)

    if r["async"]:
        obs_logger.log_event(_log, "report_enqueued", level="info",
                             report_type=report_type, task_id=r["task_id"])
        return {"ok": True, "task_id": r["task_id"], "async": True,
                "message": "报表生成已入队，轮询 GET /api/ops/report/{task_id}"}
    # 同步降级（队列不可用）
    return {"ok": r["result"].get("success", False), "task_id": None, "async": False,
            "sync": True, "result": r["result"],
            "message": "队列不可用，已同步生成"}


@router.post("/report/sync")
async def report_sync(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
):
    """同步报表（对比用）：直接生成返回（不削峰）。需要权限 ops:tool:execute。"""
    body = await request.json()
    report_type = body.get("report_type", "inventory")
    from_date = body.get("from")
    to_date = body.get("to")
    if report_type not in ("inventory", "reconciliation"):
        return JSONResponse({"ok": False, "error": "report_type 必须是 inventory|reconciliation"},
                            status_code=400)
    r = get_queue().sync_generate_report(report_type, from_date, to_date)
    return {"ok": r.get("success", False), "result": r}


@router.get("/report/{task_id}")
async def report_status(
    task_id: str,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
):
    """轮询异步报表结果：finished → result；未完成 → {ready: false, status}。"""
    r = get_queue().get_report_result(task_id)
    if r.get("ready"):
        obs_logger.log_event(_log, "report_finished", level="info", task_id=task_id)
        return {"ok": True, "ready": True, "task_id": task_id, **r["result"]}
    if r.get("status") == "failed":
        return {"ok": False, "ready": False, "task_id": task_id,
                "status": "failed", "error": r.get("error", "")}
    return {"ok": True, "ready": False, "task_id": task_id, "status": r.get("status", "unknown")}
