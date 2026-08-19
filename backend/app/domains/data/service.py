"""★ NL2SQL 编排服务（W24 Day6）——多轮消解 → 子图 → 洞察 → 统一返回结构。

设计（对应《W24学习执行手册》Day6 + ADR-01"域间只经内部 API 通信"）：
- 把 router 里"多轮消解 → 子图 → 洞察 → 组装"抽成可复用编排函数 `run_nl2sql_query`：
    · router（POST /api/data/query）→ 权限 + 参数 → 调本服务；
    · 对话入口（kb/chat 语义路由 data 分支）→ 权限检查后调本服务，SSE 流式返回表格事件；
  两处复用同一实现，保证结果结构一致（域间解耦：kb 只依赖本服务，不 import 内部模块）。
- 结果结构（统一契约）：
    {ok, question, resolved_question, session_id, table, sql, columns, rows,
     elapsed, truncated, rejected_reason, reply, insights, repair_attempts, repair_log}
  ★ Day6 新增 `insights`：洞察摘要（≤3 条，禁止编造数字——insight.py 双保险）。
- 审计：`audit_sink` 可选回调（executor/repair 事件 → 调用方写 audit_logs，域间解耦）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.domains.data.graph import data_graph
from app.domains.data.prompts import DATA_BASE_DATE

# 审计回调契约（与 executor.py 一致，域间解耦）
_AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_nl2sql_query(
    question: str,
    today: str | None = None,
    session_id: str | None = None,
    audit_sink: _AuditSink | None = None,
    *,
    with_insights: bool = True,
) -> dict[str, Any]:
    """NL2SQL 完整编排：多轮消解（有会话）→ 子图（四道闸+自修复）→ 洞察摘要。

    - session_id 非空且会话有历史 → 先做指代消解（"那华南呢？"补全省份）；
    - 查询成功（有 columns）→ 记会话上下文（下轮消解来源）+ 生成洞察摘要；
    - 返回统一契约结构（见模块 docstring），供 router / 对话入口复用。
    """
    from app.domains.data.insight import generate_insights
    from app.domains.data.schema_linker import linker
    from app.domains.data.session_ctx import get_session

    today = today or DATA_BASE_DATE.isoformat()

    # ---- 多轮指代消解（有会话才做；首轮/无会话原样） ----
    resolved = question
    session_ctx = None
    if session_id:
        session_ctx = get_session(session_id)
        resolved = await session_ctx.resolve(question, today)

    result = await data_graph.ainvoke(
        {
            "question": resolved,
            "today": today,
            "audit_sink": audit_sink,
        }
    )

    res = result.get("result") or {}
    sql = res.get("sql") or result.get("sql", "")
    columns = res.get("columns", [])
    rows = res.get("rows", [])

    # ---- 查询成功 → 记会话上下文（下轮消解来源）+ 洞察摘要（★ Day6） ----
    insights: list[str] = []
    if session_ctx is not None and columns:
        try:
            tables = linker.link_prompt_tables(resolved)
        except Exception:  # noqa: BLE001
            # ★ W27-D2 修复：容器内无 embedding 模型（sentence_transformers 未装，
            #   Dockerfile 设计"模型推理不在容器内做"）→ 降级空表。
            #   tables 只是会话元数据（消解只用 prev question/sql），缺失不影响链路；
            #   真实模型环境照常召回（fail-open 原则，不因元数据挂主链路）。
            tables = []
        session_ctx.record(resolved, sql, tables)
    if with_insights and columns:
        insights = await generate_insights(resolved, columns, rows, sql)

    return {
        "ok": True,
        "question": question,
        "resolved_question": resolved,
        "session_id": session_id,
        "table": bool(columns),
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "elapsed": res.get("elapsed_ms", 0.0),
        "truncated": res.get("truncated", False),
        "rejected_reason": res.get("rejected_reason"),
        "reply": result.get("reply", ""),
        "insights": insights,
        "repair_attempts": result.get("repair_attempts", 0),
        "repair_log": result.get("repair_log", []),
    }
