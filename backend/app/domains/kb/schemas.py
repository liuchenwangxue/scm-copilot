"""知识问答域 API 契约（★ W25 Day4 OpenAPI 规范化）。

SSE 事件协议（流式端点 response_model 无法表达，由 openapi_extra 描述 + 本模块模型注释）：
    - progress:     {type, node, data:{result}}——链路节点进展
    - message:      {type, role, content, delta, session_id}——打字机增量（delta=true）/整段结束（delta=false）
    - citations:    {type, citations, retrieved_docs, validation, source?, session_id}——引用溯源 + 校验结果
    - data_table:   {type, columns, rows, sql, insights, elapsed, truncated, rejected_reason, reply, session_id}
                    ★ W24 Day6 语义路由 data 分支：查数结果以表格事件下发
    - done:         {type}——流结束
    - error:        {type, error}——链路异常（SSE 语义内上报，HTTP 仍为 200）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class KbChatIn(BaseModel):
    """知识问答请求体（SSE 流式）。

    长度限制由服务端显式校验（空消息/超 2000 字符 → 400），不在模型层强制
    （保持与既有行为一致；模型层只做结构契约）。
    """

    message: str = Field(description="用户问题（服务端限制 ≤2000 字符）")
    session_id: str | None = Field(None, description="会话标识；缺省服务端生成")


class KbFeedbackIn(BaseModel):
    """引用纠错反馈请求体（点赞/纠错 → 待审核 → 回流评测集 v2）。"""

    question: str = Field(..., description="原始问题")
    action: Literal["like", "dislike", "correct"] = Field("like", description="反馈动作")
    original_answer: str = Field("", description="原回答")
    corrected_answer: str = Field("", description="纠正后的回答")
    correct_doc_ids: list[str] = Field(default_factory=list, description="正确引用文档 id")
    qa_id: str = Field("", description="关联问答 id")


class KbFeedbackOut(BaseModel):
    """反馈提交响应。"""

    ok: bool
    feedback_id: str
    status: str


class KbSseEvent(BaseModel):
    """KB 域 SSE 事件类型枚举（文档/自查用；流式端点的实际协议见模块 docstring）。"""

    type: Literal["progress", "message", "citations", "data_table", "done", "error"]
