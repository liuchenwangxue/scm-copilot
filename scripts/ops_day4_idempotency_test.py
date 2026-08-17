"""W19 Day4 幂等测试（B6）：同操作提交 3 次 → 接口只执行 1 次

验证点：
A. 幂等键确定性：同(会话+操作+目标) → 同 key；不同目标 → 不同 key
B. 同 key 提交 3 次：仅首次执行 func，后 2 次命中缓存返回首次结果
C. mock 侧双保险：同 Idempotency-Key 请求 mock 只执行一次（updated_at 一致）
D. 幂等命中审计记录：idempotency_hit × 2
E. 不同幂等键（不同目标）→ 正常执行（不误伤）

运行：python scripts\\day4_idempotency_test.py
"""
import contextlib
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
SERVER_MAIN = Path(__file__).resolve().parents[2] / "stage3-project-b" / "mock_biz_server" / "main.py"
sys.path.insert(0, str(BACKEND))

from app.domains.ops.agent.tools.order_tools import OrderTools
from app.domains.ops.security.audit import AuditLogger
from app.shared.reliability.idempotency import IdempotencyStore, execute_idempotent

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


def main():
    print(f"W19 Day4 幂等测试（B6）｜ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    tmp = Path(tempfile.mkdtemp(prefix="day4_idem_"))
    db = tmp / "idem.db"
    audit_path = tmp / "audit.log"
    audit = AuditLogger(audit_path)
    # ★ W21 Day3：默认走 Redis（auto），namespace 用 pid+时间戳隔离——
    #   Redis key 全局共享，固定 namespace 多次运行会残留 SUCCESS 误判
    store = IdempotencyStore(db, namespace=f"day4-{os.getpid()}-{time.strftime('%H%M%S')}")

    # ---- A. 幂等键确定性 ----
    k1 = IdempotencyStore.build_key("sess-1", "update_order", "PO-0002")
    k2 = IdempotencyStore.build_key("sess-1", "update_order", "PO-0002")
    k3 = IdempotencyStore.build_key("sess-1", "update_order", "PO-0003")
    check("同(会话+操作+目标) → 同 key", k1 == k2)
    check("不同目标 → 不同 key", k1 != k3)
    check("key 是 sha256 长度(64 hex)", len(k1) == 64)

    # ---- mock 服务 ----
    proc, base = start_server()
    try:
        tools = OrderTools(base, retries=1, base_delay=0.02)

        # ---- B+C. 同 key 提交 3 次：接口只执行 1 次 ----
        session = "sess-1"
        op = "update_order"
        target = "PO-0002"
        key = IdempotencyStore.build_key(session, op, target)

        call_count = {"n": 0}
        results = []
        hits = []

        def execute_with_same_key():
            # 业务执行函数：真实调用工具，幂等键一路传给 mock（双保险）
            call_count["n"] += 1
            r = tools.update_order(target, amount=9100.00, idempotency_key=key)
            return r.data

        for _ in range(3):
            result, hit = execute_idempotent(store, session, op, target,
                                             execute_with_same_key, audit)
            results.append(result)
            hits.append(hit)

        check("func 只被调 1 次（3 次提交 → 1 次执行）", call_count["n"] == 1,
              f"call_count={call_count['n']}")
        check("3 次返回结果一致（含首次结果）",
              results[0] == results[1] == results[2],
              f"amount={results[0]['amount']}")
        check("命中标记：首次 False，后 2 次 True", hits == [False, True, True], str(hits))
        check("mock 侧 updated_at 一致（幂等头双保险）",
              results[0]["updated_at"] == results[1]["updated_at"] == results[2]["updated_at"],
              f"updated_at={results[0]['updated_at']}")
        check("幂等状态 = SUCCESS", store.status(key) == "SUCCESS")
        # 再查一次缓存（纯读取路径）
        cached, hit4 = execute_idempotent(store, session, op, target,
                                          execute_with_same_key, audit)
        check("第 4 次仍命中缓存", hit4 is True and cached == results[0])

        # ---- D. 幂等命中审计 ----
        hits_audit = audit.filter("idempotency_hit")
        check("审计有 idempotency_hit 记录", len(hits_audit) >= 2, f"count={len(hits_audit)}")

        # ---- E. 不同幂等键正常执行 ----
        def execute_other():
            call_count["n"] += 1
            r = tools.update_order("PO-0003", amount=5000.00,
                                   idempotency_key=IdempotencyStore.build_key(
                                       session, op, "PO-0003"))
            return r.data

        result_other, hit_other = execute_idempotent(store, session, op, "PO-0003",
                                                     execute_other, audit)
        check("不同目标（不同 key）→ 正常执行", hit_other is False
              and result_other["order_id"] == "PO-0003"
              and result_other["amount"] == 5000.0)
        check("func 总调用 = 2（1 次同 key + 1 次不同 key）", call_count["n"] == 2,
              f"call_count={call_count['n']}")

        # ---- F. 失败不缓存，可重试 ----
        def execute_fail():
            call_count["n"] += 1
            raise ConnectionError("transient down")

        with contextlib.suppress(ConnectionError):
            execute_idempotent(store, "sess-2", "update_order", "PO-0009", execute_fail, audit)
        check("失败后状态 = FAILED（不缓存结果）", store.status(
            IdempotencyStore.build_key("sess-2", "update_order", "PO-0009")) == "FAILED")
        check("失败可重试（同 key 再次执行 func）", call_count["n"] == 3)
    finally:
        stop(proc)

    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n===== 汇总：{passed}/{total} PASS =====")
    for name, ok, d in _results:
        if not ok:
            print(f"  FAIL: {name} {d}")
    sys.exit(0 if passed == total else 1)


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


if __name__ == "__main__":
    main()
