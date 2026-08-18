"""SDK 数据结构（dataclass 轻量封装，不做全量 pydantic——薄封装原则）。

- `ChatEvent`：SSE 事件（type + 原始 payload；`delta` 便捷取打字机增量）
- `Nl2SqlResult`：nl2sql 查询结果（表格 / SQL / 洞察 / 会话）
- `ApprovalItem`：审批列表项（含 HITL 恢复上下文 session_id）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatEvent:
    """SSE 流式事件（kb/ops/chat 通用协议）。

    `type` ∈ {progress, message, citations, data_table, approval_request, done, error}。
    `data` 保留原始 payload——不同事件字段不同，调用方按 type 分支读取。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def delta(self) -> str:
        """message 事件的增量文本（打字机效果；非 message 事件返回空串）。"""
        if self.type == "message":
            return str(self.data.get("content") or "")
        return ""

    @property
    def error(self) -> str:
        """error 事件的错误信息。"""
        return str(self.data.get("error") or "") if self.type == "error" else ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ChatEvent:
        """从 SSE `data:` JSON 行构造（缺 type 兜底 unknown，不抛）。"""
        return cls(type=str(payload.get("type") or "unknown"), data=payload)


@dataclass
class Nl2SqlResult:
    """NL2SQL 查询结果（对应后端 Nl2SqlOut 契约）。"""

    ok: bool = True
    question: str = ""
    table: bool = False
    sql: str = ""
    columns: list[Any] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)
    reply: str = ""
    elapsed: float = 0.0
    truncated: bool = False
    rejected_reason: str | None = None
    insights: list[Any] = field(default_factory=list)
    session_id: str | None = None
    resolved_question: str | None = None
    repair_attempts: int = 0
    repair_log: list[Any] = field(default_factory=list)
    # 可选：as_dataframe=True 时填充（pandas DataFrame 或 None）
    df: Any | None = field(default=None, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Nl2SqlResult:
        return cls(
            ok=bool(payload.get("ok", True)),
            question=str(payload.get("question") or ""),
            table=bool(payload.get("table")),
            sql=str(payload.get("sql") or ""),
            columns=list(payload.get("columns") or []),
            rows=list(payload.get("rows") or []),
            reply=str(payload.get("reply") or ""),
            elapsed=float(payload.get("elapsed") or 0.0),
            truncated=bool(payload.get("truncated") or False),
            rejected_reason=payload.get("rejected_reason"),
            insights=list(payload.get("insights") or []),
            session_id=payload.get("session_id"),
            resolved_question=payload.get("resolved_question"),
            repair_attempts=int(payload.get("repair_attempts") or 0),
            repair_log=list(payload.get("repair_log") or []),
        )


@dataclass
class ApprovalItem:
    """审批列表项（对应后端 ApprovalListItemOut 契约）。

    `session_id` 是 HITL 恢复上下文：`decide(id, action)` 时回传即可 resume LangGraph 图。
    """

    approval_id: str
    session_id: str
    operation: str
    order_id: str
    diff: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    status: str = "pending"
    created_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ApprovalItem:
        return cls(
            approval_id=str(payload.get("approval_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            operation=str(payload.get("operation") or ""),
            order_id=str(payload.get("order_id") or ""),
            diff=list(payload.get("diff") or []),
            reason=str(payload.get("reason") or ""),
            status=str(payload.get("status") or "pending"),
            created_at=payload.get("created_at"),
        )
