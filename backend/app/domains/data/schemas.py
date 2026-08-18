"""数据分析域 API 契约（★ W25 Day4 OpenAPI 规范化）——请求/响应模型 100% 类型注解。

契约（对齐 W24 Day6 统一返回结构，见 `service.run_nl2sql_query` docstring）：
    Nl2SqlOut: {ok, question, resolved_question, session_id, table, sql, columns,
                rows, elapsed, truncated, rejected_reason, reply, insights,
                repair_attempts, repair_log}
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Nl2SqlIn(BaseModel):
    """NL2SQL 查询请求体。

    空 question 由服务端显式校验 → 400（模型层不做长度强制，保持既有行为）。
    """

    question: str = Field(description="自然语言问题")
    today: str | None = Field(
        None, description="基准日期（YYYY-MM-DD）；缺省用评测基日，跨月/跨年交给 MySQL"
    )
    session_id: str | None = Field(None, description="多轮会话标识；携带则做指代消解")


class Nl2SqlOut(BaseModel):
    """NL2SQL 统一响应（表格 + SQL + 洞察；100% 透出 SQL 可审计可纠错）。"""

    ok: bool = True
    question: str = Field(description="原始问题")
    resolved_question: str = Field(description="指代消解后的完整问题（无会话 = 原问题）")
    session_id: str | None = Field(None, description="多轮会话标识")
    table: bool = Field(description="是否返回表格（false = 拒答/降级）")
    sql: str = Field(description="实际执行的 SQL（100% 透出）")
    columns: list[Any] = Field(default_factory=list, description="列名")
    rows: list[Any] = Field(default_factory=list, description="行数据（已规范化类型）")
    elapsed: float = Field(description="执行耗时 ms")
    truncated: bool = Field(description="是否因行数/字节上限被截断")
    rejected_reason: str | None = Field(None, description="被四道闸拒绝的原因")
    reply: str = Field(description="自然语言回复（摘要/拒答/降级话术）")
    insights: list[str] = Field(default_factory=list, description="结果洞察摘要（≤3 条）")
    repair_attempts: int = Field(0, description="错误自修复次数（0 = 未修复）")
    repair_log: list[dict[str, Any]] = Field(
        default_factory=list, description="修复轨迹（generate→error→repair 可回放）"
    )


class DataFeedbackIn(BaseModel):
    """SQL 纠错样本回流请求体。"""

    sql: str = Field(..., description="实际执行/待纠错的 SQL")
    question: str | None = Field(None, description="对应自然语言问题")
    correction: str | None = Field(None, description="人工纠正说明")
    is_correct: bool | None = Field(None, description="SQL 是否正确")


class DataFeedbackOut(BaseModel):
    """反馈回流响应（落库失败返回 ok=false + error，不阻断主链路）。"""

    ok: bool
    feedback_id: int | None = None
    error: str | None = None
