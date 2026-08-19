"""数据分析域路由（W24 Day3 + Day5 + Day6 演进）——POST /api/data/query（NL2SQL 全链路 API）。

对应《W24学习执行手册》Day3 上午 + Day5 下午 + Day6 +《02》4 节 API 一览：
    POST /api/data/query        自然语言 → 表格 + SQL + 洞察（JWT + data:nl2sql 权限）
    POST /api/data/query/{id}/feedback  SQL 纠错样本回流（feedback 表，W24 周产出）

响应契约（Day3 上午 + Day5 + ★ Day6 演进）：
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
      "insights": [...],               # ★ Day6：结果洞察摘要（≤3 条，禁止编造数字）
      "repair_attempts": int,          # ★ Day5：错误自修复次数（0=未修复）
      "repair_log": [...],             # ★ Day5：修复轨迹（generate→error→repair 可回放）
    }

审计：executor/repair 事件 → 写 audit_logs（回调由 router 组装，data 域不直接 import platform；
      trace_id 取自 RequestIdMiddleware 写入的 scope）。

★ Day5：body 可选带 `session_id`——有则做多轮指代消解（session_ctx）再入图，
   查询成功后记录 {question, sql, tables} 作为下轮上下文（无状态化见 session_ctx docstring）；
   feedback 端点为 W25 纠错回流预留。
★ Day6：业务编排收口到 `service.run_nl2sql_query`（多轮消解→子图→洞察→统一返回结构），
   本 router 只做权限 + 参数 + 审计回调注入；对话入口（kb/chat 语义路由 data 分支）复用同一服务。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.domains.data.graph import data_graph
from app.domains.data.prompts import DATA_BASE_DATE
from app.domains.data.schemas import DataFeedbackIn, DataFeedbackOut, Nl2SqlIn, Nl2SqlOut
from app.platform import rbac
from app.platform.models import User

router = APIRouter(prefix="/api/v1/data", tags=["data"])


def _audit_sink(request: Request, current: User) -> Any:
    """构造审计回调：把 executor 事件写入 audit_logs（事件含 SQL 原文，取证可回放）。

    域间解耦：data 域不直接 import platform 内部模块，只依赖 request.app.state 的
    session_factory（平台装配层注入）——与 Day2 executor 回调契约一致。
    """
    session_factory = request.app.state.session_factory

    async def sink(event: dict[str, Any]) -> None:
        from app.platform.audit import write_audit

        detail: dict[str, Any] = {
            "error": event.get("error"),
            "rows": event.get("rows"),
            "elapsed_ms": event.get("elapsed_ms"),
        }
        # ★ W27-D6 (B13)：sql 只在携带的事件里写（execute 事件有；repair 事件已去重，
        #   只带 repaired_sql）——避免 audit_logs detail 里 sql=null 的冗余噪音
        if event.get("sql") is not None:
            detail["sql"] = event.get("sql")
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


@router.post(
    "/query",
    response_model=Nl2SqlOut,
    summary="NL2SQL 查询",
    description=(
        "自然语言 →（多轮消解）→ SQL（sqlglot 四道闸 + 错误自修复）→ 只读沙箱执行 → 表格 + 洞察。"
        "需要权限 data:nl2sql。返回契约见响应模型（含 insights 洞察摘要、repair_log 修复轨迹）。"
    ),
)
async def data_query(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("data:nl2sql"))],
    body: Nl2SqlIn,
) -> Nl2SqlOut:
    """NL2SQL 查询：自然语言 →（多轮消解）→ SQL（四道闸 + 自修复）→ 只读沙箱 → 表格 + 洞察。"""
    from app.domains.data.service import run_nl2sql_query

    question = body.question.strip()
    if not question:
        # ★ W25 Day4：业务校验错误走统一 Err 契约（BAD_REQUEST_400）
        raise HTTPException(status_code=400, detail="question 不能为空")
    today = body.today or DATA_BASE_DATE.isoformat()
    session_id = (body.session_id or "").strip() or None

    result = await run_nl2sql_query(
        question=question,
        today=today,
        session_id=session_id,
        audit_sink=_audit_sink(request, current),
    )
    return Nl2SqlOut.model_validate(result)


@router.post(
    "/query/{query_id}/feedback",
    response_model=DataFeedbackOut,
    summary="SQL 纠错样本回流",
    description="SQL 纠错样本回流（feedback 表 fb_type='sql'）——W25 eval_nightly 回流评测集。",
)
async def data_feedback(
    query_id: str,
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("data:nl2sql"))],
    body: DataFeedbackIn,
) -> DataFeedbackOut:
    """SQL 纠错样本回流（feedback 表，fb_type='sql'）——W25 eval_nightly 回流评测集。"""
    session_factory = request.app.state.session_factory
    from app.platform.models import Feedback

    try:
        async with session_factory() as session:
            fb = Feedback(
                fb_type="sql",
                conversation_id=query_id,
                content=(body.sql or "")[:5000],
                correction=(body.correction or body.question or "")[:5000],
                status="open",
                created_by=current.username,
            )
            session.add(fb)
            await session.commit()
            return DataFeedbackOut(ok=True, feedback_id=fb.id)
    except Exception as exc:  # noqa: BLE001  # 反馈落库失败不影响主链路
        return DataFeedbackOut(ok=False, error=str(exc))
