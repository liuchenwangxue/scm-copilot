"""approvals：审批接口（列待审批 + 决策）。

对应后端：
- `GET /api/v1/ops/approvals`：待审批列表（含 HITL 恢复上下文 session_id）
- `POST /api/v1/ops/approval`：审批决策（approve/reject，resume LangGraph 图）

十行示例：
    pending = client.approvals.list_pending()
    if pending:
        item = pending[0]
        client.approvals.decide(item.approval_id, "approve", reason="平台放行", session_id=item.session_id)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scm_client.models import ApprovalItem

if TYPE_CHECKING:
    from scm_client import ScmCopilot


class Approvals:
    """审批子资源（经 `client.approvals` 访问）。"""

    def __init__(self, client: ScmCopilot):
        self._client = client

    def list_pending(self) -> list[ApprovalItem]:
        """列出待审批（pending 优先；进程重启后可从此找回挂起状态）。"""
        payload = self._client._request("GET", "/api/v1/ops/approvals").json()
        return [
            ApprovalItem.from_payload(item)
            for item in (payload.get("approvals") or [])
        ]

    def decide(
        self,
        approval_id: str,
        action: str,
        reason: str = "",
        session_id: str | None = None,
    ) -> dict:
        """审批决策：action ∈ {approve, reject}。

        session_id 来自 list_pending() 返回项的同一字段（HITL 恢复上下文，
        resume LangGraph 图继续执行）；未传时后端要求非空（从列表项回传）。
        """
        if action not in ("approve", "reject"):
            raise ValueError(f"action 必须是 'approve' 或 'reject'，收到 {action!r}")
        body = {
            "approval_id": approval_id,
            "decision": action,
            "reason": reason,
            "session_id": session_id or "",
        }
        resp = self._client._request("POST", "/api/v1/ops/approval", json=body)
        return resp.json()
