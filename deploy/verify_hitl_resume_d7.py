"""★ W28-D7 端到端验证：HITL resume 不再重复 create 审批单（Day2 观察项闭环）。

流程：
1. 用 API Key 触发高危操作（改金额）→ approval_gate interrupt → 拿到 approval_id A
2. 调用 approval 决策 approve（resume）→ 图恢复执行
3. 断言：approvals 表中同 session 只该有 1 条 update_order 单（幂等键复用，不再成对）
"""
from __future__ import annotations

import contextlib
import json

# 用法：SCM_API_KEY=sk-... python deploy/verify_hitl_resume_d7.py
# （key 需有 ops:approval:manage 权限，如 admin 角色的 key）
import os
import sys
import uuid

import httpx

KEY = os.environ.get("SCM_API_KEY", "")
BASE = os.environ.get("SCM_BASE_URL", "https://localhost:18443")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if not KEY:
        print("请设置 SCM_API_KEY 环境变量（需 ops:approval:manage 权限）")
        return 1
    c = httpx.Client(base_url=BASE, verify=False, timeout=30,
                     headers={"Authorization": f"Bearer {KEY}"})
    session_id = f"w28d7-hitl-{uuid.uuid4().hex[:8]}"
    print(f"session_id = {session_id}")

    # ---- 1. 触发高危操作审批 ----
    print("\n=== 1. 触发高危操作（改金额） ===")
    events: list[dict] = []
    # ★ 目标金额动态生成（修复不可重跑缺陷：固定 9600 在上次运行后 == 当前值，
    #   diff 为空导致"表单含 diff"恒失败）。基于时间戳取 [1000, 99999]，
    #   两次运行间隔 < 99000s 时值必不相同，diff 恒非空。
    import time as _time
    target_amount = 1000 + int(_time.time()) % 99000
    with c.stream("POST", "/api/v1/ops/chat",
                  json={"message": f"把订单 PO-0002 改金额为 {target_amount} 元",
                        "session_id": session_id}) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                with contextlib.suppress(Exception):
                    events.append(json.loads(line[6:]))
    check("chat http 200", r.status_code == 200, str(r.status_code))
    approval = next((e for e in events if e.get("type") == "approval_request"), None)
    aid1 = approval["approval_id"] if approval else None
    check("approval_request 事件到达", approval is not None,
          f"aid1={aid1}")
    if aid1 is None:
        print("  事件:", [e.get("type") for e in events])
        return 1
    form = approval.get("form") or {}
    diff = form.get("diff") or []
    check("表单含 diff", len(diff) >= 1, json.dumps(diff, ensure_ascii=False)[:100])

    # ---- 2. 库中当前同 session 单数（应 1） ----
    print("\n=== 2. 库中同 session 单数（首次应 1） ===")
    import pymysql
    from pymysql.cursors import DictCursor

    from app.domains.ops.security.approval import parse_mysql_dsn
    from app.platform.settings import settings

    def _count_by_session() -> list[dict]:
        conn2 = pymysql.connect(cursorclass=DictCursor, **parse_mysql_dsn(settings.platform_dsn))
        try:
            with conn2.cursor() as cur:
                cur.execute("SELECT approval_no, status, idem_key FROM approvals WHERE actor=%s", (session_id,))
                return cur.fetchall()
        finally:
            conn2.close()

    rows0 = _count_by_session()
    check("首次仅 1 条审批单", len(rows0) == 1, f"count={len(rows0)}")

    # ---- 3. approve（resume 恢复图） ----
    print("\n=== 3. approve 决策（HITL resume） ===")
    resp = c.post("/api/v1/ops/approval",
                  json={"session_id": session_id, "approval_id": aid1,
                        "decision": "approve", "reason": "w28d7 验证"})
    body = resp.json()
    check("approve http 200", resp.status_code == 200, str(resp.status_code))
    check("approve ok=True", body.get("ok") is True, json.dumps(body, ensure_ascii=False)[:200])

    # ---- 4. 库中同 session 单数（resume 后应仍 1 条，且为 approved） ----
    print("\n=== 4. resume 后库中单数（修复核心断言） ===")
    rows = _count_by_session()
    check("resume 后仍仅 1 条（不重复 create）", len(rows) == 1,
          f"count={len(rows)} ids={[r['approval_no'][:8] for r in rows]}")
    check("该单状态 approved（与前端展示的同一单）",
          len(rows) == 1 and rows[0]["status"] == "approved",
          f"status={rows[0]['status'] if rows else 'N/A'}")
    check("approval_id 一致（前端展示 = 实际决议）",
          len(rows) == 1 and rows[0]["approval_no"] == aid1,
          f"rows={[r['approval_no'][:8] for r in rows]} vs aid1={aid1[:8] if aid1 else 'N/A'}")
    c.close()

    print(f"\n=== W28-D7 HITL resume 验证结果: {PASS} passed / {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    # 基于脚本位置定位 backend（原相对路径 "backend" 依赖 CWD，换目录运行即 ModuleNotFoundError）
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    raise SystemExit(main())
