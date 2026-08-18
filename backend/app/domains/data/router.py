"""数据分析域路由（W24 Day3）——POST /api/data/query（NL2SQL 全链路 API）。

对应《W24学习执行手册》Day3 上午 +《02》4 节 API 一览：
    POST /api/data/query        自然语言 → 表格 + SQL（JWT + data:nl2sql 权限）
    POST /api/data/query/{id}/feedback  SQL 纠错样本回流（feedback 表，W24 周产出）

响应契约（Day3 上午）：
    {
      "table": bool,        # 是否返回了表格（false = 拒答/降级）
      "sql": str,           # 实际执行的 SQL（100% 透出，可审计可纠错）
      "columns": [...],
      "rows": [...],
      "elapsed": float,     # 执行耗时 ms
      "rejected_reason": str | null,   # 被四道闸拒绝的原因
      "reply": str,         # 自然语言回复（摘要/拒答话术）
      "question": str,
    }

审计：executor 事件 → 写 audit_logs（回调由 router 组装，data 域不直接 import platform；
      trace_id 取自 RequestIdMiddleware 写入的 scope）。

Day3 多轮会话历史暂不落库（W24-D5 补 session_ctx）；feedback 端点为 W25 纠错回流预留。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.domains.data.graph import data_graph
from app.domains.data.prompts import DATA_BASE_DATE
from app.platform import rbac
from app.platform.models import User

router = APIRouter(prefix="/api/data", tags=["data"])


def _audit_sink(request: Request, current: User) -> Any:
    """构造审计回调：把 executor 事件写入 audit_logs（事件含 SQL 原文，取证可回放）。

    域间解耦：data 域不直接 import platform 内部模块，只依赖 request.app.state 的
    session_factory（平台装配层注入）——与 Day2 executor 回调契约一致。
    """
    session_factory = request.app.state.session_factory

    async def sink(event: dict[str, Any]) -> None:
        from app.platform.audit import write_audit

        async with session_factory() as session:
            await write_audit(
                session,
                event=event.get("event", "data:nl2sql:execute"),
                actor=current.username,
                target="data:nl2sql",
                status=200 if event.get("status") in (None, "ok") else 500,
                detail={
                    "sql": event.get("sql"),
                    "error": event.get("error"),
                    "rows": event.get("rows"),
                    "elapsed_ms": event.get("elapsed_ms"),
                },
                trace_id=request.scope.get("request_id"),
            )
            await session.commit()

    return sink


@router.post("/query")
async def data_query(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("data:nl2sql"))],
    body: dict,
) -> Any:
    """NL2SQL 查询：自然语言 → SQL（四道闸）→ 只读沙箱执行 → 表格。

    需要权限 `data:nl2sql`（analyst/admin 角色）。body: {question, today?}
    返回 {table, sql, columns, rows, elapsed, rejected_reason, reply, question}。
    """
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse(
            {"ok": False, "error": "question 不能为空"},
            status_code=400,
        )
    today = body.get("today") or DATA_BASE_DATE.isoformat()

    result = await data_graph.ainvoke(
        {
            "question": question,
            "today": today,
            "audit_sink": _audit_sink(request, current),
        }
    )

    res = result.get("result") or {}
    return {
        "ok": True,
        "question": question,
        "table": bool(res.get("columns")),
        "sql": res.get("sql") or result.get("sql", ""),
        "columns": res.get("columns", []),
        "rows": res.get("rows", []),
        "elapsed": res.get("elapsed_ms", 0.0),
        "truncated": res.get("truncated", False),
        "rejected_reason": res.get("rejected_reason"),
        "reply": result.get("reply", ""),
    }


@router.post("/query/{query_id}/feedback")
async def data_feedback(
    query_id: str,
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("data:nl2sql"))],
    body: dict,
) -> dict:
    """SQL 纠错样本回流（feedback 表，fb_type='sql'）——W25 eval_nightly 回流评测集。

    body: {sql, question, correction, is_correct}
    返回 {ok: true, feedback_id}（落库失败则返回 ok=false 但不抛错——反馈尽力而为）。
    """
    session_factory = request.app.state.session_factory
    from app.platform.models import Feedback

    try:
        async with session_factory() as session:
            fb = Feedback(
                fb_type="sql",
                conversation_id=query_id,
                content=(body.get("sql") or "")[:5000],
                correction=(body.get("correction") or body.get("question") or "")[:5000],
                status="open",
                created_by=current.username,
            )
            session.add(fb)
            await session.commit()
            return {"ok": True, "feedback_id": fb.id}
    except Exception as exc:  # noqa: BLE001  # 反馈落库失败不影响主链路
        return {"ok": False, "error": str(exc)}
