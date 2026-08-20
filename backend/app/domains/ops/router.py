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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.domains.ops import config
from app.domains.ops.agent.graph import get_biz_graph
from app.domains.ops.schemas import (
    ApprovalIn,
    ApprovalListItemOut,
    ApprovalOut,
    ApprovalsOut,
    OpsChatIn,
    ReportEnqueueOut,
    ReportIn,
    ReportStatusOut,
    ReportSyncOut,
)
from app.domains.ops.security.approval import ApprovalService
from app.domains.ops.security.audit import AuditLogger
from app.domains.ops.tasks.queue import get_queue
from app.platform import rbac
from app.platform.models import User
from app.shared.obs import logger as obs_logger

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

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


@router.post(
    "/chat",
    response_class=StreamingResponse,
    summary="业务对话（SSE 流式）",
    description=(
        "意图识别 → 审批门 → 执行 → 回答。高危操作在 approval_gate 中断并推送 "
        "approval_request 事件（HITL），前端展示审批表单后调 POST /api/v1/ops/approval 恢复。"
        "事件协议：progress / approval_request / message / done / error。需要权限 ops:tool:execute。"
    ),
    responses={
        200: {
            "description": (
                "SSE 流（text/event-stream）：\n"
                "- progress:         {type, node, data:{result}}——intent/approval_gate/execute/respond 节点\n"
                "- approval_request: {type, approval_id, form, session_id}——HITL 审批表单\n"
                "- message:          {type, role, content, delta, session_id}——打字机增量\n"
                "- done / error:     流结束 / 链路异常"
            ),
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": (
                        'data: {"type":"progress","node":"intent",'
                        '"data":{"result":"识别意图：改单"}}\n\n'
                    ),
                }
            },
        }
    },
)
async def chat(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
    body: OpsChatIn,
):
    """业务对话（SSE）：意图识别 → 审批门 → 执行 → 回答。需要权限 ops:tool:execute。

    高危操作在 approval_gate 中断并推送 approval_request 事件（HITL），
    前端展示审批表单后调 POST /api/ops/approval 恢复。
    """
    message = body.message
    session_id = body.session_id or str(uuid.uuid4())
    runtime_cfg = {"configurable": {"thread_id": session_id}}
    request_id = str(uuid.uuid4())[:12]

    async def event_gen():
        try:
            obs_logger.log_event(_log, "chat_started", request_id=request_id,
                                 session_id=session_id, msg_len=len(message))
            biz_graph = await _get_graph()
            # ★ W27 D7：durability="exit"——checkpoint 合并写（LangGraph 每步 aput 的写放大）。
            #   一次图执行只在退出时写 1 次 checkpoint（默认 async 每 super-step 写 1 次），
            #   40 并发压测的 MySQL 写压力显著下降；interrupt（HITL 审批）在挂起时强制写
            #   checkpoint（源码 _suppress_interrupt 已确认），恢复语义不变。
            async for event in biz_graph.astream(
                    {"message": message, "session_id": session_id},
                    runtime_cfg, stream_mode="updates", durability="exit"):
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


@router.get(
    "/approvals",
    response_model=ApprovalsOut,
    summary="审批列表（待审批）",
    description=(
        "列出待审批（含 HITL 断点恢复上下文 session_id）。★ W25 Day5：SDK "
        "approvals.list_pending() 的数据源——进程重启后集成方从此找回挂起状态。"
        "需要权限 ops:approval:manage。"
    ),
)
async def list_approvals(
    _: Annotated[User, Depends(rbac.require_permission("ops:approval:manage"))],
) -> ApprovalsOut:
    """审批列表：待审批优先（断点恢复：进程重启后从 approvals 表找回挂起状态）。

    session_id = 审批发起时的 actor（LangGraph thread_id），decide 时回传即可 resume。

    ★ W26 Day2 故障演练修复：MySQL 不可用 → 503 明确提示（审批暂停，不雪崩）。
    """
    try:
        pending = approval_svc.list_pending()
    except Exception as e:  # noqa: BLE001  # 存储故障 → 明确 503
        raise HTTPException(
            status_code=503,
            detail=f"审批服务暂不可用（审批存储依赖故障），请稍后重试：{type(e).__name__}",
        ) from e
    items = [
        ApprovalListItemOut(
            approval_id=r.approval_id,
            session_id=r.session_id,
            operation=r.operation,
            order_id=r.order_id,
            diff=r.diff,
            reason=r.reason,
            status=r.status,
            created_at=r.created_at,
        )
        for r in pending
    ]
    return ApprovalsOut(approvals=items, total=len(items))


@router.post(
    "/approval",
    response_model=ApprovalOut,
    summary="审批决策（HITL 恢复）",
    description=(
        "批准/拒绝高危操作，Command(resume) 恢复 LangGraph 图继续执行。需要权限 ops:approval:manage。"
    ),
)
async def approval_action(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:approval:manage"))],
    body: ApprovalIn,
) -> ApprovalOut:
    """审批动作：批准/拒绝高危操作，Command(resume) 恢复图继续执行。

    ★ 平台化：原 stage3 的 admin/operator 角色限制升级为权限码 `ops:approval:manage`
      （仅 admin/operator 拥有，viewer/analyst 403）。
    """
    session_id = body.session_id
    approval_id = body.approval_id
    decision = body.decision
    reason = body.reason
    runtime_cfg = {"configurable": {"thread_id": session_id}}

    audit.log("approval_action", user=current.username, role=current.tenant_id,
              approval_id=approval_id, decision=decision, reason=reason[:100])
    try:
        # 审批动作由 graph 内 approval_gate 统一处理（approve/reject 落库 + HITL resume），
        # 避免路由层重复调用造成"单向状态机 already"错误。
        biz_graph = await _get_graph()
        # ★ W27 D7：与 chat 路径同款 durability="exit"（checkpoint 合并写）。
        result = await biz_graph.ainvoke(
            Command(resume={"decision": decision, "reason": reason}),
            runtime_cfg, durability="exit")
        return ApprovalOut(
            ok=True,
            approval_id=approval_id,
            decision=decision,
            reply=result.get("reply", ""),
            degraded=result.get("degraded", False),
            tool_result=result.get("tool_result"),
        )
    except HTTPException:
        raise
    except Exception as e:
        # ★ W26 Day2 故障演练修复：MySQL 不可用时 approve/reject 抛存储异常 →
        #   503 明确提示（审批暂停不雪崩）；业务错误（如单已审批）仍 200 ok=False
        from pymysql import MySQLError

        if isinstance(e, MySQLError) or "refused" in str(e).lower() or "closed" in str(e).lower():
            raise HTTPException(
                status_code=503,
                detail=f"审批存储暂不可用，请稍后重试：{type(e).__name__}",
            ) from e
        return ApprovalOut(ok=False, error=str(e), reply="")


@router.post(
    "/report",
    response_model=ReportEnqueueOut,
    summary="异步报表（入队削峰）",
    description="立即返回 task_id，worker 后台生成；队列不可用 → 同步降级返回 result。需要权限 ops:tool:execute。",
)
async def report_async(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
    body: ReportIn,
) -> ReportEnqueueOut:
    """异步报表：立即返回 {task_id}，worker 后台生成（削峰）。需要权限 ops:tool:execute。"""
    report_type = body.report_type
    from_date = body.from_
    to_date = body.to

    audit.log("report_requested", user=current.username,
              report_type=report_type, from_date=from_date, to_date=to_date, mode="async")
    r = get_queue().enqueue_report(report_type, from_date, to_date)

    if r["async"]:
        obs_logger.log_event(_log, "report_enqueued", level="info",
                             report_type=report_type, task_id=r["task_id"])
        return ReportEnqueueOut.model_validate(
            {
                "ok": True, "task_id": r["task_id"], "async": True,
                "message": "报表生成已入队，轮询 GET /api/v1/ops/report/{task_id}",
            }
        )
    # 同步降级（队列不可用）
    return ReportEnqueueOut.model_validate(
        {
            "ok": r["result"].get("success", False), "task_id": None, "async": False,
            "sync": True, "result": r["result"], "message": "队列不可用，已同步生成",
        }
    )


@router.post(
    "/report/sync",
    response_model=ReportSyncOut,
    summary="同步报表",
    description="直接生成返回（不削峰，对比/调试用）。需要权限 ops:tool:execute。",
)
async def report_sync(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
    body: ReportIn,
) -> ReportSyncOut:
    """同步报表（对比用）：直接生成返回（不削峰）。需要权限 ops:tool:execute。"""
    report_type = body.report_type
    from_date = body.from_
    to_date = body.to
    r = get_queue().sync_generate_report(report_type, from_date, to_date)
    return ReportSyncOut(ok=r.get("success", False), result=r)


@router.get(
    "/report/{task_id}",
    response_model=ReportStatusOut,
    summary="轮询异步报表结果",
    description="finished → result（展开进顶层）；未完成 → {ready: false, status}。",
)
async def report_status(
    task_id: str,
    current: Annotated[User, Depends(rbac.require_permission("ops:tool:execute"))],
) -> ReportStatusOut:
    """轮询异步报表结果：finished → result；未完成 → {ready: false, status}。"""
    r = get_queue().get_report_result(task_id)
    if r.get("ready"):
        obs_logger.log_event(_log, "report_finished", level="info", task_id=task_id)
        return ReportStatusOut(ok=True, ready=True, task_id=task_id, **r["result"])
    if r.get("status") == "failed":
        return ReportStatusOut(ok=False, ready=False, task_id=task_id,
                               status="failed", error=r.get("error", ""))
    return ReportStatusOut(ok=True, ready=False, task_id=task_id, status=r.get("status", "unknown"))
