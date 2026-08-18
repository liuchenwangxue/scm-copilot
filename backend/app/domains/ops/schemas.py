"""业务操作域 API 契约（★ W25 Day4 OpenAPI 规范化）。

SSE 事件协议（ops/chat 流式端点，openapi_extra 描述 + 本模块注释）：
    - progress:          {type, node, data:{result}}——intent / approval_gate / execute / respond 节点进展
    - approval_request:  {type, approval_id, form, session_id}——HITL 审批门中断，前端展示审批表单
    - message:           {type, role, content, delta, session_id}——打字机增量
    - done / error:      流结束 / 链路异常
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OpsChatIn(BaseModel):
    """业务对话请求体（SSE 流式）。"""

    message: str = Field(..., description="业务指令（查单/改单/报表等）")
    session_id: str | None = Field(None, description="会话标识；缺省服务端生成")


class ApprovalIn(BaseModel):
    """HITL 审批决策请求体。"""

    session_id: str = Field(..., description="HITL 会话标识")
    approval_id: str = Field(..., description="审批单 ID（approval_request 事件下发）")
    decision: Literal["approve", "reject"] = Field(..., description="批准 / 拒绝")
    reason: str = Field("", description="审批意见")


class ApprovalOut(BaseModel):
    """审批决策响应（resume LangGraph 图后的执行结果）。"""

    ok: bool
    approval_id: str | None = None
    decision: str | None = None
    reply: str = Field("", description="执行后的自然语言回复")
    degraded: bool = False
    tool_result: Any | None = None
    error: str | None = None


class ReportIn(BaseModel):
    """报表请求体（async / sync 共用）。"""

    report_type: Literal["inventory", "reconciliation"] = "inventory"
    from_: str | None = Field(None, alias="from", description="起始日期 YYYY-MM-DD")
    to: str | None = Field(None, description="截止日期 YYYY-MM-DD")

    model_config = ConfigDict(populate_by_name=True)


class ReportEnqueueOut(BaseModel):
    """异步报表入队响应（队列不可用时同步降级返回 result）。"""

    ok: bool
    task_id: str | None = None
    async_: bool | None = Field(None, alias="async", description="是否异步入队")
    sync: bool | None = None
    result: dict[str, Any] | None = None
    message: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ReportSyncOut(BaseModel):
    """同步报表响应。"""

    ok: bool
    result: dict[str, Any] | None = None


class ReportStatusOut(BaseModel):
    """异步报表轮询响应（finished 时展开 result 字段，extra 放行）。"""

    ok: bool = True
    ready: bool = False
    task_id: str | None = None
    status: str | None = None
    error: str | None = None

    model_config = ConfigDict(extra="allow")
