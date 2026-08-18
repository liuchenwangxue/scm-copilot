"""数据分析域路由（W24 Day3 + Day5 演进）——POST /api/data/query（NL2SQL 全链路 API）。

对应《W24学习执行手册》Day3 上午 + Day5 下午 +《02》4 节 API 一览：
    POST /api/data/query        自然语言 → 表格 + SQL（JWT + data:nl2sql 权限）
    POST /api/data/query/{id}/feedback  SQL 纠错样本回流（feedback 表，W24 周产出）

响应契约（Day3 上午 + Day5 演进）：
    {
      "table": bool,        # 是否返回了表格（false = 拒答/降级）
      "sql": str,           # 实际执行的 SQL（100% 透出，可审计可纠错）
      "columns": [...],
      "rows": [...],
      "elapsed": float,     # 执行耗时 ms
      "rejected_reason": str | null,   # 被四道闸拒绝的原因
      "reply": str,         # 自然语言回复（摘要/拒答/降级话术）
      "question": str,      # 原始问题
      "resolved_question": str,        # ★ Day5：指代消解后的完整问题（无会话=原问题）
      "session_id": str | null,        # ★ Day5：多轮会话标识（body 传入）
      "repair_attempts": int,          # ★ Day5：错误自修复次数（0=未修复）
      "repair_log": [...],             # ★ Day5：修复轨迹（generate→error→repair 可回放）
    }

审计：executor/repair 事件 → 写 audit_logs（回调由 router 组装，data 域不直接 import platform；
      trace_id 取自 RequestIdMiddleware 写入的 scope）。

★ Day5：body 可选带 `session_id`——有则做多轮指代消解（session_ctx）再入图，
   查询成功后记录 {question, sql, tables} 作为下轮上下文（无状态化见 session_ctx docstring）；
   feedback 端点为 W25 纠错回流预留。
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

        detail = {
            "sql": event.get("sql"),
            "error": event.get("error"),
            "rows": event.get("rows"),
            "elapsed_ms": event.get("elapsed_ms"),
        }
        if event.get("repaired_sql"):
            detail["repaired_sql"] = event.get("repaired_sql")  # ★ Day5：修复轨迹可回放
        async with session_factory() as session:
            await write_audit(
                session,
                event=event.get("event", "data:nl2sql:execute"),
                actor=current.username,
                target="data:nl2sql",
                status=200 if event.get("status") in (None, "ok") else 500,
                detail=detail,
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
    """NL2SQL 查询：自然语言 →（多轮消解）→ SQL（四道闸 + 自修复）→ 只读沙箱 → 表格。

    需要权限 `data:nl2sql`（analyst/admin 角色）。body: {question, today?, session_id?}
    返回 {table, sql, columns, rows, elapsed, rejected_reason, reply, question,
          resolved_question, session_id, repair_attempts, repair_log}。
    """
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse(
            {"ok": False, "error": "question 不能为空"},
            status_code=400,
        )
    today = body.get("today") or DATA_BASE_DATE.isoformat()
    session_id = (body.get("session_id") or "").strip() or None

    # ---- ★ Day5：多轮指代消解（有会话才做；首轮/无会话原样返回） ----
    resolved = question
    session_ctx = None
    if session_id:
        from app.domains.data.session_ctx import get_session

        session_ctx = get_session(session_id)
        resolved = await session_ctx.resolve(question, today)

    result = await data_graph.ainvoke(
        {
            "question": resolved,
            "today": today,
            "audit_sink": _audit_sink(request, current),
        }
    )

    res = result.get("result") or {}
    sql = res.get("sql") or result.get("sql", "")
    columns = res.get("columns", [])

    # ---- ★ Day5：查询成功后把 {问题, SQL, 召回表} 记为下轮上下文 ----
    if session_ctx is not None and columns:
        from app.domains.data.schema_linker import linker

        session_ctx.record(resolved, sql, linker.link_prompt_tables(resolved))

    return {
        "ok": True,
        "question": question,
        "resolved_question": resolved,
        "session_id": session_id,
        "table": bool(columns),
        "sql": sql,
        "columns": columns,
        "rows": res.get("rows", []),
        "elapsed": res.get("elapsed_ms", 0.0),
        "truncated": res.get("truncated", False),
        "rejected_reason": res.get("rejected_reason"),
        "reply": result.get("reply", ""),
        "repair_attempts": result.get("repair_attempts", 0),
        "repair_log": result.get("repair_log", []),
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
