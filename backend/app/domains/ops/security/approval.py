"""★ 高危操作人工确认（W19 Day4 HITL 生产化，W2 升级）

设计要点（面试亮点）：
1. 审批表单 = {operation, target_order, before/after diff, reason}——**展示变更差异**
   （没有差异展示的审批是形式主义，面试会被追问）
2. 幂等键在**审批发起时**生成（手册坑：防重复审批），批准后执行带同一个 key
3. 审批单持久化到 sqlite（断点恢复载体）：进程重启 → list_pending() 从 pending 恢复
4. 状态机：pending → approved / rejected（单向，不可二次审批）

流程（Day5 接进 LangGraph 图的 approval_gate / wait_approval 节点）：
   高危操作命中 → create() 发起审批（SSE 返回"待确认"事件）→
   用户 approve()/reject() → 批准后带幂等键执行 → 审计全链留痕
"""
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.domains.ops.security.audit import AuditLogger

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class ApprovalRequest:
    approval_id: str
    session_id: str
    tool_name: str            # update_order / cancel_order
    operation: str            # 人类可读操作描述（审批表单展示）
    order_id: str
    before: dict              # 变更前状态（diff 基准）
    after: dict               # 变更后状态（目标）
    reason: str
    status: str
    idem_key: str             # ★ 审批发起时生成的幂等键
    created_at: str
    resolved_at: str | None = None

    @property
    def diff(self) -> list[dict]:
        """before/after 差异（审批表单核心展示）。"""
        return build_diff(self.before, self.after)

    def to_form(self) -> dict:
        """审批表单（SSE approval_request 事件载荷，Day5 用）。"""
        return {
            "approval_id": self.approval_id,
            "operation": self.operation,
            "target_order": self.order_id,
            "diff": self.diff,
            "reason": self.reason,
            "status": self.status,
        }


def build_diff(before: dict, after: dict) -> list[dict]:
    """计算 before/after 差异：仅列出"有变化的字段"。"""
    diffs = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            diffs.append({"field": k, "before": before.get(k), "after": after.get(k)})
    return diffs


class ApprovalService:
    """审批服务：审批单创建/批准/拒绝 + sqlite 持久化（断点恢复）。"""

    def __init__(self, db_path: str | Path, audit: AuditLogger | None = None):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    tool_name    TEXT NOT NULL,
                    operation    TEXT NOT NULL,
                    order_id     TEXT NOT NULL,
                    before_json  TEXT NOT NULL,
                    after_json   TEXT NOT NULL,
                    reason       TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    idem_key     TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    resolved_at  REAL
                )
            """)

    # ---- 发起审批 ----

    def create(self, tool_name: str, operation: str, order_id: str,
               before: dict, after: dict, reason: str, session_id: str = "default",
               idem_key: str | None = None) -> ApprovalRequest:
        """发起审批（幂等键在此刻生成——手册坑：审批发起时，不是执行时）。"""
        from app.shared.reliability.idempotency import IdempotencyStore
        if idem_key is None:
            idem_key = IdempotencyStore.build_key(session_id, tool_name, order_id)

        req = ApprovalRequest(
            approval_id=str(uuid.uuid4()),
            session_id=session_id,
            tool_name=tool_name,
            operation=operation,
            order_id=order_id,
            before=before,
            after=after,
            reason=reason,
            status=STATUS_PENDING,
            idem_key=idem_key,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals (approval_id, session_id, tool_name, operation, order_id, "
                "before_json, after_json, reason, status, idem_key, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (req.approval_id, session_id, tool_name, operation, order_id,
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 reason, STATUS_PENDING, idem_key, time.time()))
        if self.audit:
            self.audit.log("approval_requested", approval_id=req.approval_id,
                           operation=operation, target=order_id,
                           diff_count=len(req.diff), reason=reason)
        return req

    # ---- 审批动作（单向状态机） ----

    def approve(self, approval_id: str, approver: str = "admin") -> ApprovalRequest:
        req = self.get(approval_id)
        if req is None:
            raise ValueError(f"approval not found: {approval_id}")
        if req.status != STATUS_PENDING:
            raise ValueError(f"approval {approval_id} already {req.status} (单向状态机)")
        with self._connect() as conn:
            conn.execute("UPDATE approvals SET status=?, resolved_at=? WHERE approval_id=?",
                         (STATUS_APPROVED, time.time(), approval_id))
        req.status = STATUS_APPROVED
        req.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self.audit:
            self.audit.log("approval_approved", approval_id=approval_id,
                           target=req.order_id, approver=approver)
        return req

    def reject(self, approval_id: str, reject_reason: str,
               approver: str = "admin") -> ApprovalRequest:
        req = self.get(approval_id)
        if req is None:
            raise ValueError(f"approval not found: {approval_id}")
        if req.status != STATUS_PENDING:
            raise ValueError(f"approval {approval_id} already {req.status}")
        with self._connect() as conn:
            conn.execute("UPDATE approvals SET status=?, resolved_at=? WHERE approval_id=?",
                         (STATUS_REJECTED, time.time(), approval_id))
        req.status = STATUS_REJECTED
        req.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self.audit:
            self.audit.log("approval_rejected", approval_id=approval_id,
                           target=req.order_id, approver=approver, reason=reject_reason)
        return req

    # ---- 查询（断点恢复核心） ----

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?",
                               (approval_id,)).fetchone()
        return self._row_to_req(row) if row else None

    def list_pending(self) -> list[ApprovalRequest]:
        """待审批清单——★断点恢复：进程重启后从这里找回挂起状态。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status=? ORDER BY created_at", (STATUS_PENDING,)
            ).fetchall()
        return [self._row_to_req(r) for r in rows]

    def list_all(self) -> list[ApprovalRequest]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM approvals ORDER BY created_at").fetchall()
        return [self._row_to_req(r) for r in rows]

    @staticmethod
    def _row_to_req(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=row["approval_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            operation=row["operation"],
            order_id=row["order_id"],
            before=json.loads(row["before_json"]),
            after=json.loads(row["after_json"]),
            reason=row["reason"],
            status=row["status"],
            idem_key=row["idem_key"],
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                     time.localtime(row["created_at"])),
            resolved_at=(time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                       time.localtime(row["resolved_at"]))
                         if row["resolved_at"] else None),
        )
