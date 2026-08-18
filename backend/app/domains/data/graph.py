"""★ NL2SQL 子图（W24 Day3 + Day5 演进）——generate → validate → execute →（repair 循环）→ format。

流程（对应《W24学习执行手册》Day3 上午 + Day5 上午）：
    generate（LLM/mock 生成 SQL）
      → validate（sqlglot 四道闸）
        → 拒绝：
            - 安全类拒绝（write-op/not-select/multi-statement/dangerous-func/for-update）
              → reject_node（拒答 + 改写建议，不硬答）【永不修复，安全不豁免】
            - 可修复类拒绝（parse-error 语法残 / unknown-table 表名错）
              → repair_node（Day5 新增）
        → 通过 → execute（只读沙箱，3s 超时/行数上限）
          → 成功 → format（结果表格化 + SQL 透出）
          → 报错 → repair_node（Day5 新增：错误自修复，循环 ≤2 次）
              → 修复后 SQL 重新过四道闸再执行（安全不豁免）
              → 仍失败且次数耗尽 → degrade_node（降级话术，不硬答）

设计要点：
- 与 kb/ops 图同构（LangGraph StateGraph），节点职责单一；
- 安全边界不依赖模型"听话"：validate 节点是确定性 sqlglot 四道闸；**修复产物同样过闸**；
- mock 全链路验证 / real 测效果——两个数字分开记（mock 生成器见 mock_sql.py / mock_repair.py）；
- LLM 调用经 shared.llm 模型池（mock/real 双路径 + 额度耗尽自动切换）；
- 审计：execute/repair 后写 audit_logs（注入回调，域间解耦，与 Day2 executor 一致）；
- 修复循环记 repair_log（面试演示素材：generate→error→repair→validate→execute 全链路可见）。

State 字段：
    question / today：输入
    initial_sql：可选测试/复核入口（非 None 则跳过生成，直接走闸+执行，Day5 修复评测用）
    sql：LLM 生成的原始 SQL（修复循环中为"当前待验证 SQL"）
    validated_sql：过闸后的 SQL
    rejected_reason：拒绝原因（None = 通过）
    error：执行错误信息（None = 成功）
    result：{columns, rows, truncated, elapsed_ms}
    reply：最终回复（表格摘要 / 拒答 / 降级话术）
    repair_attempts：已修复次数（0 = 未修复）
    repair_exhausted：修复次数耗尽（→ degrade）
    repair_log：[{attempt, failed_sql, failure, repaired_sql}] 修复轨迹
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domains.data.executor import ExecutionError, QueryTimeoutError, execute_sql
from app.domains.data.mock_sql import MockSQLGenerator
from app.domains.data.prompts import DATA_BASE_DATE, build_nl2sql_messages
from app.domains.data.repair import (
    MAX_REPAIR_ATTEMPTS,
    REPAIRABLE_REASONS,
    clean_sql,
    repair_sql,
)
from app.domains.data.sql_validator import SQLRejected, validate_sql
from app.shared.llm import get_provider

# 审计回调：{event, sql, status, error, elapsed_ms, rows} → 由调用方注入写 audit_logs
_AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


class DataState(TypedDict, total=False):
    question: str
    today: str
    initial_sql: str  # 测试/复核入口：非空则跳过生成直接走闸（Day5 修复评测）
    sql: str
    validated_sql: str
    rejected_reason: str | None
    error: str | None
    result: dict[str, Any]
    reply: str
    repair_attempts: int  # 已修复次数（0 = 未修复；上限 MAX_REPAIR_ATTEMPTS）
    repair_exhausted: bool  # 修复次数耗尽 → degrade
    repair_log: list[dict[str, Any]]  # 修复轨迹（LangFuse/审计可回放）
    # 由调用方注入的审计回调（写 audit_logs；域间解耦，不直接 import platform）
    audit_sink: _AuditSink | None


def _make_sql_generator() -> MockSQLGenerator:
    return MockSQLGenerator()


# ================= 节点 =================


async def generate_node(state: DataState) -> dict[str, Any]:
    """生成 SQL：real 走 LLM 模型池；mock 走评测集确定性生成（只验链路）。

    `initial_sql` 非空时跳过生成（修复评测/复核入口：把"故意坏 SQL"直接送进闸+执行）。
    """
    if state.get("initial_sql"):
        return {
            "sql": state["initial_sql"],
            "question": state.get("question", ""),
            "today": state.get("today") or DATA_BASE_DATE.isoformat(),
        }
    question = state["question"]
    today = state.get("today") or DATA_BASE_DATE.isoformat()
    provider = get_provider()

    if provider.name == "mock":
        # mock 只测链路：从评测集按问题精确匹配 gold SQL（效果数字不算数）
        sql = _make_sql_generator().generate(question)
    else:
        messages = build_nl2sql_messages(question, today)
        raw = await provider.generate(messages, max_tokens=1024, temperature=0.0)
        sql = clean_sql(raw)

    return {"sql": sql, "question": question, "today": today}


def validate_node(state: DataState) -> dict[str, Any]:
    """sqlglot 四道闸确定性校验；拒绝落 reason（供审计/前端展示）。"""
    sql = state["sql"]
    try:
        validated = validate_sql(sql)
        return {"validated_sql": validated, "rejected_reason": None}
    except SQLRejected as exc:
        return {"validated_sql": "", "rejected_reason": exc.reason}


def route_after_validate(state: DataState) -> str:
    """拒绝分两类（★ Day5）：
    - 可修复类（parse-error / unknown-table，模型"诚实犯错"）→ repair；
    - 安全类（write-op/not-select/...）→ 首次 reject（拒答）；修复循环中出现 → degrade（安全不豁免）。
    """
    reason = state.get("rejected_reason")
    if reason is None:
        return "execute"
    if reason in REPAIRABLE_REASONS:
        return "repair"
    return "degrade" if state.get("repair_attempts", 0) > 0 else "reject"


def reject_node(state: DataState) -> dict[str, Any]:
    """拒答 + 改写建议（不硬答）：错误答案的代价远高于拒答。"""
    reason = state.get("rejected_reason") or "unknown"
    sql = state.get("sql", "")
    tips = {
        "multi-statement": "问题只需一条查询，请改为单条 SELECT。",
        "not-select": "只支持查询（SELECT），不支持写入/修改操作。",
        "write-op": "检测到写入类操作，只支持只读查询。",
        "dangerous-func": "查询中使用了禁止的危险函数。",
        "unknown-table": "查询涉及非业务表，请只查询业务库 scm_biz 的六张表。",
        "for-update": "不支持锁读（FOR UPDATE）。",
        "parse-error": "SQL 无法解析，请重新表述问题。",
    }
    return {
        "reply": (
            f"无法执行该查询：{tips.get(reason, reason)}。"
            "可尝试改为：查询订单数量 / 某区域订单金额 / 延迟发货统计等只读分析问题。"
        ),
        "result": {
            "columns": [],
            "rows": [],
            "truncated": False,
            "elapsed_ms": 0.0,
            "rejected_reason": reason,
            "sql": sql,
        },
    }


async def execute_node(state: DataState) -> dict[str, Any]:
    """只读沙箱执行（nl2sql_ro + 3s 超时 + 行数上限）；出错信息留给 repair/format。"""
    audit: _AuditSink | None = state.get("audit_sink")
    try:
        result = await execute_sql(state["validated_sql"], audit=audit)
        return {"result": result, "error": None}
    except (QueryTimeoutError, ExecutionError) as exc:
        return {
            "result": {"columns": [], "rows": [], "truncated": False, "elapsed_ms": 0.0,
                       "error": str(exc), "sql": state["validated_sql"]},
            "error": str(exc),
        }


def route_after_execute(state: DataState) -> str:
    """执行失败 → repair（Day5）；成功 → format。"""
    return "repair" if state.get("error") else "format"


async def repair_node(state: DataState) -> dict[str, Any]:
    """★ W24 Day5：错误自修复——带报错上下文重写 SQL（≤2 次）。

    - 失败信息 = 执行报错（error）或可修复闸拒（rejected_reason）；
    - 修复产物**仍必须重新过四道闸**（安全不豁免，由 validate 节点保证）；
    - 次数耗尽（repair_exhausted=True）→ degrade_node（降级话术，不硬答）；
    - 修复轨迹追加 repair_log（审计/演示可回放 generate→error→repair 链路）。
    """
    attempts = state.get("repair_attempts", 0) + 1
    if attempts > MAX_REPAIR_ATTEMPTS:
        return {"repair_attempts": attempts, "repair_exhausted": True}

    question = state.get("question", "")
    today = state.get("today") or DATA_BASE_DATE.isoformat()
    failed_sql = state.get("sql", "")
    failure = state.get("error") or state.get("rejected_reason") or "unknown"

    repaired = await repair_sql(question, failed_sql, failure, today)

    log = list(state.get("repair_log") or [])
    log.append(
        {
            "attempt": attempts,
            "failed_sql": failed_sql,
            "failure": failure,
            "repaired_sql": repaired,
        }
    )

    # 修复审计（取证可回放：谁在什么错上修复成了什么）
    audit: _AuditSink | None = state.get("audit_sink")
    if audit is not None:
        with contextlib.suppress(Exception):  # 审计失败不阻断主流程（与 executor 同纪律）
            await audit(
                {
                    "event": "data:nl2sql:repair",
                    "sql": failed_sql,
                    "status": "ok",
                    "error": failure,
                    "elapsed_ms": 0.0,
                    "rows": 0,
                    "repaired_sql": repaired,
                }
            )

    return {"repair_attempts": attempts, "sql": repaired, "repair_log": log}


def route_after_repair(state: DataState) -> str:
    """修复次数耗尽 → degrade；否则重新过闸（validate）。"""
    return "degrade" if state.get("repair_exhausted") else "validate"


def degrade_node(state: DataState) -> dict[str, Any]:
    """★ W24 Day5：降级话术——不硬答。给出改写建议（错误答案代价远高于拒答）。"""
    question = state.get("question", "")
    return {
        "reply": f"暂时无法生成有效查询。建议：{_rewrite_suggestion(question)}",
        "repair_exhausted": True,
        "result": {
            "columns": [],
            "rows": [],
            "truncated": False,
            "elapsed_ms": 0.0,
            "error": state.get("error") or state.get("rejected_reason"),
            "sql": state.get("sql", ""),
        },
    }


def format_node(state: DataState) -> dict[str, Any]:
    """结果格式化：成功 → 表格摘要 + SQL 透出；防御性保留报错分支。"""
    result = state.get("result") or {}
    error = state.get("error")
    if error:
        return {
            "reply": (
                "暂时无法生成有效查询。"
                f"执行报错：{error[:120]}。建议：{_rewrite_suggestion(state.get('question', ''))}"
            )
        }

    rows = result.get("rows", [])
    columns = result.get("columns", [])
    truncated = result.get("truncated", False)
    reply = f"查询成功：共 {len(rows)} 行结果（{', '.join(columns)}）"
    if truncated:
        reply += "（已截断，仅显示前 200 行）"
    return {"reply": reply, "error": None}


def _rewrite_suggestion(question: str) -> str:
    """报错时的改写建议（简单规则 + 兜底）。"""
    if "延迟" in question:
        return "可尝试改为：SELECT COUNT(*) FROM shipments WHERE delay_days > 0"
    if "订单" in question and ("金额" in question or "多少" in question):
        return "可尝试改为：SELECT region, SUM(amount) FROM orders GROUP BY region"
    return "可尝试更简单的问法，例如'华东区域有多少订单'"


# ================= 图编译 =================

builder = StateGraph(DataState)
builder.add_node("generate", generate_node)
builder.add_node("validate", validate_node)
builder.add_node("reject", reject_node)
builder.add_node("execute", execute_node)
builder.add_node("repair", repair_node)
builder.add_node("degrade", degrade_node)
builder.add_node("format", format_node)
builder.add_edge(START, "generate")
builder.add_edge("generate", "validate")
builder.add_conditional_edges(
    "validate",
    route_after_validate,
    {"reject": "reject", "execute": "execute", "repair": "repair", "degrade": "degrade"},
)
builder.add_edge("reject", END)
builder.add_conditional_edges(
    "execute", route_after_execute, {"format": "format", "repair": "repair"}
)
builder.add_conditional_edges(
    "repair", route_after_repair, {"validate": "validate", "degrade": "degrade"}
)
builder.add_edge("degrade", END)
builder.add_edge("format", END)

# 无 checkpointer（Day3 单轮；多轮会话上下文 W24-D5 由 session_ctx 在图上处理），
# 无跨事件循环问题 → 模块级编译
data_graph = builder.compile()
