"""★ 高危操作人工确认（W19 Day4 HITL 生产化，★ W23 Day5 存储迁 MySQL）

设计要点（面试亮点）：
1. 审批表单 = {operation, target_order, before/after diff, reason}——**展示变更差异**
   （没有差异展示的审批是形式主义，面试会被追问）
2. 幂等键在**审批发起时**生成（手册坑：防重复审批），批准后执行带同一个 key
3. 审批单持久化到 MySQL 平台库 approvals 表（★ Day5：从 sqlite 文件迁 MySQL，
   双实例共享审批状态——无状态化核销清单"审批单 SQLite→MySQL"落项）；
   历史 SQLite 数据由 `scripts/migrate_sqlite_to_mysql.py` 无损迁移
4. 状态机：pending → approved / rejected（单向，不可二次审批）

实现说明：
- 图节点 approval_gate 是**同步函数**，故 ApprovalService 保持同步接口，
  存储层用 pymysql（与 asyncmy 同 MySQL 协议，Python 同步驱动）直连平台库。
- 平台 approvals 表字段映射：
  approval_no=approval_id · action=tool_name · operation · target_type=order ·
  target_id=order_id · actor=session_id · diff_before/diff_after=before/after ·
  reason · idem_key · status · decided_by=approver · decided_at=resolved_at
- 幂等：approval_no 唯一索引 + INSERT IGNORE（重跑不重复）

流程（Day5 接进 LangGraph 图的 approval_gate / wait_approval 节点）：
   高危操作命中 → create() 发起审批（SSE 返回"待确认"事件）→
   用户 approve()/reject() → 批准后带幂等键执行 → 审计全链留痕
"""
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pymysql
from pymysql.cursors import DictCursor

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


def parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """`mysql+asyncmy://user:pwd@host:port/db?charset=...` → pymysql connect 参数。"""
    p = urlparse(dsn)
    scheme, _, _ = p.scheme.partition("+")
    if scheme not in ("mysql", "mariadb"):
        raise ValueError(f"不支持的 DSN scheme: {p.scheme}")
    query = parse_qs(p.query)
    return {
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 3306,
        "user": p.username or "root",
        "password": p.password or "",
        "database": p.path.lstrip("/"),
        "charset": query.get("charset", ["utf8mb4"])[0],
        "autocommit": True,
    }


class ApprovalService:
    """审批服务：审批单创建/批准/拒绝 + MySQL 平台库持久化（断点恢复）。"""

    def __init__(self, dsn: str | None = None, audit: AuditLogger | None = None):
        from app.platform.settings import settings

        self.dsn = dsn or settings.platform_dsn
        self.audit = audit

    def _connect(self) -> pymysql.Connection:
        return pymysql.connect(cursorclass=DictCursor, **parse_mysql_dsn(self.dsn))

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
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO approvals (approval_no, action, operation, target_type, "
                "target_id, actor, diff_before, diff_after, reason, idem_key, status, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (req.approval_id, tool_name, operation, "order", order_id, session_id,
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 reason, idem_key, STATUS_PENDING, now))
        if self.audit:
            self.audit.log("approval_requested", approval_id=req.approval_id,
                           operation=operation, target=order_id,
                           diff_count=len(req.diff), reason=reason)
        # ★ W28 Day5 (C6/B6)：审批 IM 推送（尽力而为，webhook 挂不影响审批主流程）。
        #   只发摘要（审批 id+工具+字段名，不发敏感值）；SCM_WEBHOOK_URL 空 = 关闭。
        from app.domains.ops.notify.webhook import notify_approval_requested_async

        notify_approval_requested_async(
            req.approval_id, tool_name, operation, order_id,
            diff=req.diff, reason=reason,
        )
        return req

    # ---- 审批动作（单向状态机） ----

    def approve(self, approval_id: str, approver: str = "admin") -> ApprovalRequest:
        req = self.get(approval_id)
        if req is None:
            raise ValueError(f"approval not found: {approval_id}")
        if req.status != STATUS_PENDING:
            raise ValueError(f"approval {approval_id} already {req.status} (单向状态机)")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, decided_at=%s "
                "WHERE approval_no=%s AND status=%s",
                (STATUS_APPROVED, approver, now, approval_id, STATUS_PENDING))
        req.status = STATUS_APPROVED
        req.resolved_at = now
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, decided_at=%s "
                "WHERE approval_no=%s AND status=%s",
                (STATUS_REJECTED, approver, now, approval_id, STATUS_PENDING))
        req.status = STATUS_REJECTED
        req.resolved_at = now
        if self.audit:
            self.audit.log("approval_rejected", approval_id=approval_id,
                           target=req.order_id, approver=approver, reason=reject_reason)
        return req

    # ---- 查询（断点恢复核心） ----

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM approvals WHERE approval_no=%s", (approval_id,))
            row = cur.fetchone()
        return self._row_to_req(row) if row else None

    def list_pending(self) -> list[ApprovalRequest]:
        """待审批清单——★断点恢复：进程重启后从这里找回挂起状态。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM approvals WHERE status=%s ORDER BY created_at", (STATUS_PENDING,))
            rows = cur.fetchall()
        return [self._row_to_req(r) for r in rows]

    def list_all(self) -> list[ApprovalRequest]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM approvals ORDER BY created_at")
            rows = cur.fetchall()
        return [self._row_to_req(r) for r in rows]

    @staticmethod
    def _row_to_req(row: dict[str, Any]) -> ApprovalRequest:
        before = json.loads(row["diff_before"]) if row.get("diff_before") else {}
        after = json.loads(row["diff_after"]) if row.get("diff_after") else {}
        return ApprovalRequest(
            approval_id=row["approval_no"],
            session_id=row["actor"] or "default",
            tool_name=row["action"],
            operation=row.get("operation") or row["action"],
            order_id=row["target_id"],
            before=before,
            after=after,
            reason=row.get("reason") or "",
            status=row["status"],
            idem_key=row.get("idem_key") or "",
            created_at=time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(row["created_at"].timestamp()),
            ),
            resolved_at=(
                time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z",
                    time.localtime(row["decided_at"].timestamp()),
                )
                if row.get("decided_at")
                else None
            ),
        )
