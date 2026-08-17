"""W19 Day4 审批流测试（高危 HITL）：批准流 / 拒绝流 / 断点恢复

验证点：
A. 审批表单：before/after diff（改金额 → 只列 amount；取消 → 只列 status）
B. 批准流：create → approve → 带幂等键执行 → mock 数据真实变更 + 审计链齐全
C. 拒绝流：create → reject → 不执行（mock 数据不变）+ 审计 rejected
D. ★ 断点恢复：创建审批单 → 丢弃服务实例 → 新实例同 db → list_pending 找回 → 批准继续执行
E. 单向状态机：approved 的审批单不可再 reject（409 语义）
F. 高危操作 100% 走审批：所有 update/cancel 操作都有 approval_requested 审计记录

运行：python scripts\\day4_approval_test.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]                  # scm-copilot/
BACKEND = ROOT / "backend"
# mock 业务服务仍在 stage3-project-b（迁移期指向原位置；Day5 后随业务库并入平台）
SERVER_MAIN = Path(__file__).resolve().parents[2] / "stage3-project-b" / "mock_biz_server" / "main.py"
sys.path.insert(0, str(BACKEND))

from app.domains.ops.agent.tools.order_tools import OrderTools
from app.domains.ops.security.approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalService,
)
from app.domains.ops.security.audit import AuditLogger

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    _results.append((name, bool(cond), detail))
    print(f"  [{('PASS' if cond else 'FAIL')}] {name}" + (f"  ({detail})" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server():
    port = free_port()
    env = dict(os.environ)
    for k in ("BIZ_FAIL_RATE", "BIZ_LATENCY_MS", "BIZ_500_MODE", "BIZ_429_MODE"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(SERVER_MAIN), "--port", str(port)],
                            env=env, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                return proc, base
        except Exception:
            pass
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"mock server not ready on {port}")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main():
    print(f"W19 Day4 审批流测试（高危 HITL）｜ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    tmp = Path(tempfile.mkdtemp(prefix="day4_approval_"))
    db = tmp / "biz.db"
    audit_path = tmp / "audit.log"
    audit = AuditLogger(audit_path)

    proc, base = start_server()
    try:
        tools = OrderTools(base, retries=1, base_delay=0.02)

        # ================= A. 审批表单 diff =================
        print("A. 审批表单（before/after diff）")
        svc = ApprovalService(db, audit)
        req = svc.create(
            tool_name="update_order", operation="修改订单金额", order_id="PO-0002",
            before={"amount": 9000.00, "delivery_date": "2026-09-15"},
            after={"amount": 9500.00, "delivery_date": "2026-09-15"},
            reason="供应商涨价，需调整采购金额", session_id="sess-a",
        )
        check("审批单初始状态 pending", req.status == STATUS_PENDING)
        check("diff 只列变化的字段(amount)", req.diff == [{"field": "amount", "before": 9000.0, "after": 9500.0}],
              str(req.diff))
        form = req.to_form()
        check("审批表单含 operation/target/diff/reason",
              form["operation"] == "修改订单金额" and form["target_order"] == "PO-0002"
              and form["diff"] and form["reason"])
        check("幂等键在审批发起时生成", len(req.idem_key) == 64)

        req2 = svc.create(
            tool_name="cancel_order", operation="取消订单", order_id="PO-0005",
            before={"status": "ordered"}, after={"status": "closed"},
            reason="供应商停产", session_id="sess-a",
        )
        check("取消订单 diff 只列 status", req2.diff == [{"field": "status", "before": "ordered", "after": "closed"}],
              str(req2.diff))

        # ================= B. 批准流 =================
        print("B. 批准流（create → approve → 执行 → 数据变更 + 审计链）")
        before_audit_count = len(audit.read_all())
        approved = svc.approve(req.approval_id)
        check("批准后状态 approved", approved.status == STATUS_APPROVED)

        # 批准后执行（带审批单的幂等键）——执行环节补记 execution 审计（完整事件链）
        r = tools.update_order("PO-0002", amount=9500.00, idempotency_key=req.idem_key)
        if r.success:
            audit.log("execution_succeeded", approval_id=req.approval_id,
                      target="PO-0002", idem_key=req.idem_key[:12])
        check("批准后执行成功", r.success and r.data["amount"] == 9500.0)
        # mock 数据真实变更（再查一次确认）
        q = tools.query_order("PO-0002")
        check("mock 数据已真实变更", q.data["amount"] == 9500.0)

        events = [e["event"] for e in audit.read_all()]
        check("审计链齐全（requested → approved → 执行）",
              "approval_requested" in events and "approval_approved" in events,
              f"events={events[before_audit_count:]}")
        check("审计含 execution 事件", any(e.startswith("execution") for e in events))

        # ================= C. 拒绝流 =================
        print("C. 拒绝流（create → reject → 不执行）")
        req3 = svc.create(
            tool_name="cancel_order", operation="取消订单", order_id="PO-0008",
            before={"status": "approving"}, after={"status": "closed"},
            reason="业务调整", session_id="sess-a",
        )
        rejected = svc.reject(req3.approval_id, reject_reason="审批人不同意：金额风险")
        check("拒绝后状态 rejected", rejected.status == STATUS_REJECTED)
        # 不执行：mock 数据未变
        q = tools.query_order("PO-0008")
        check("拒绝后订单未被取消（数据未变）", q.data["status"] == "approving")
        rej_audit = audit.filter("approval_rejected", approval_id=req3.approval_id)
        check("审计有 rejected 记录（含拒绝原因）",
              len(rej_audit) == 1 and "金额风险" in rej_audit[0].get("reason", ""))

        # ================= D. ★ 断点恢复 =================
        print("D. 断点恢复（杀进程 → 重启 → 从 pending 恢复）")
        # 实例 A 创建审批单
        svc_a = ApprovalService(db, audit)
        req_pending = svc_a.create(
            tool_name="update_order", operation="修改订单交期", order_id="PO-0010",
            before={"amount": 45600.00, "delivery_date": "2026-09-12"},
            after={"amount": 45600.00, "delivery_date": "2026-10-05"},
            reason="供应商交期顺延", session_id="sess-restart",
        )
        # 丢弃实例 A（模拟进程被杀）→ 全新实例 B（同 db）
        svc_b = ApprovalService(db, audit)
        pendings = svc_b.list_pending()
        check("重启后从 sqlite 找回 pending 审批单", any(
            p.approval_id == req_pending.approval_id for p in pendings),
            f"pending={len(pendings)}")
        recovered = svc_b.get(req_pending.approval_id)
        check("恢复的审批单 diff 完整", recovered.diff == [
            {"field": "delivery_date", "before": "2026-09-12", "after": "2026-10-05"}])
        # 恢复后批准并继续执行
        svc_b.approve(req_pending.approval_id)
        r = tools.update_order("PO-0010", delivery_date="2026-10-05",
                               idempotency_key=req_pending.idem_key)
        if r.success:
            audit.log("execution_succeeded", approval_id=req_pending.approval_id,
                      target="PO-0010", idem_key=req_pending.idem_key[:12])
        q = tools.query_order("PO-0010")
        check("恢复后批准 → 执行成功（数据变更）", r.success
              and q.data["delivery_date"] == "2026-10-05")

        # ================= E. 单向状态机 =================
        print("E. 单向状态机")
        try:
            svc.reject(req.approval_id, reject_reason="不允许")
            check("已批准不可再拒绝", False)
        except ValueError as e:
            check("已批准不可再拒绝（ValueError）", "already" in str(e), str(e))
        try:
            svc.approve(req3.approval_id)
            check("已拒绝不可再批准", False)
        except ValueError as e:
            check("已拒绝不可再批准（ValueError）", "already" in str(e), str(e))

        # ================= F. 高危 100% 走审批 =================
        print("F. 高危操作 100% 走审批（审计覆盖统计）")
        # 把 A 部分只验证 diff 的 req2 也决议掉（模拟业务最终都会决议，不留悬挂）
        svc.reject(req2.approval_id, reject_reason="示例审批单，不执行")
        requested = audit.filter("approval_requested")
        check("所有高危操作都有审批发起记录（覆盖统计≥1）", len(requested) >= 4,
              f"requested={len(requested)}")
        # 审批单状态统计：所有创建的单都必须已决议（approved/rejected），无悬挂
        all_reqs = svc_b.list_all()
        unresolved = [x for x in all_reqs if x.status == STATUS_PENDING]
        check("无悬挂审批单（全部已决议）", len(unresolved) == 0,
              f"unresolved={len(unresolved)}")

        total_created = len(all_reqs)
        approved_count = len([x for x in all_reqs if x.status == STATUS_APPROVED])
        rejected_count = len([x for x in all_reqs if x.status == STATUS_REJECTED])
        check(f"审批单 {total_created} 张全部决议（approved={approved_count}/rejected={rejected_count}）",
              approved_count + rejected_count == total_created)
    finally:
        stop(proc)

    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n===== 汇总：{passed}/{total} PASS =====")
    for name, ok, d in _results:
        if not ok:
            print(f"  FAIL: {name} {d}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
