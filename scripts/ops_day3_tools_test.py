"""W19 Day3 工具层 + 可靠层测试：注册表 / 熔断器 / 降级链 / 工具集成

四层测试：
A. 注册表单测（无服务）：4 工具 + 危险等级 + 高危清单
B. 熔断器单测（无服务）：三态状态机（触发熔断 + 半开恢复 + 快速失败）
C. 降级链单测（无服务）：主源/备用/兜底/业务错误透传/熔断降级
D. 工具集成（真实 mock 服务，4 个实例）：
   D1 正常实例：读/写/幂等/业务错误透传
   D2 BIZ_FAIL_RATE=0.5：重试救回，成功率 ≥ 90%
   D3 BIZ_500_MODE=/api/v1/orders/PO-0001：快照降级 + 熔断触发 + 每工具独立熔断
   D4 BIZ_FAIL_RATE=1.0：熔断 OPEN + fallback，图不崩

运行：python scripts\\day3_tools_test.py
"""
import contextlib
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]                  # scm-copilot/
BACKEND = ROOT / "backend"
SERVER_MAIN = Path(__file__).resolve().parents[2] / "stage3-project-b" / "mock_biz_server" / "main.py"
sys.path.insert(0, str(BACKEND))

from app.domains.ops.agent.tools.order_tools import OrderTools
from app.domains.ops.agent.tools.registry import BizApiError, registry
from app.domains.ops.agent.tools.report_tools import ReportTools
from app.shared.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.shared.reliability.retry_policy import degrade_chain, is_retryable_http

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    _results.append((name, bool(cond), detail))
    print(f"  [{('PASS' if cond else 'FAIL')}] {name}" + (f"  ({detail})" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env() -> dict:
    env = dict(os.environ)
    for k in ("BIZ_FAIL_RATE", "BIZ_LATENCY_MS", "BIZ_500_MODE", "BIZ_429_MODE"):
        env.pop(k, None)
    return env


def start_server(env_extra: dict | None = None):
    port = free_port()
    env = clean_env()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_MAIN), "--port", str(port)],
        env=env, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
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


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


# ================= A. 注册表单测 =================

def test_registry():
    print("A. 注册表单测（工具集 + 危险等级 + 高危清单）")
    OrderTools(base_url="http://x", base_delay=0.01)
    ReportTools(base_url="http://x", base_delay=0.01)

    names = registry.names()
    check("4 工具注册", set(names) == {"query_order", "update_order", "cancel_order", "generate_report"},
          f"{sorted(names)}")
    check("query_order 低危无需审批",
          registry.get("query_order").risk_level == "low" and not registry.get("query_order").requires_approval)
    check("update_order 高危需审批",
          registry.get("update_order").risk_level == "high" and registry.get("update_order").requires_approval)
    check("cancel_order 高危需审批",
          registry.get("cancel_order").risk_level == "high" and registry.get("cancel_order").requires_approval)
    check("generate_report 中危无需审批",
          registry.get("generate_report").risk_level == "medium"
          and not registry.get("generate_report").requires_approval)
    high = {s.name for s in registry.high_risk()}
    check("高危清单 = {update_order, cancel_order}", high == {"update_order", "cancel_order"}, str(high))
    llm_schema = registry.describe_for_llm()
    check("LLM function schema 可导出", len(llm_schema) == 4 and llm_schema[0]["type"] == "function")


# ================= B. 熔断器单测 =================

def test_circuit_breaker():
    print("B. 熔断器单测（三态状态机）")
    cb = CircuitBreaker("test", failure_threshold=3, cooldown=0.2)

    def flaky():
        raise ConnectionError("down")

    for _ in range(3):
        with contextlib.suppress(ConnectionError):
            cb.call(flaky)
    check("连续失败3次 → OPEN", cb.state == "OPEN", cb.state)

    # OPEN 快速失败：不调 func
    calls = {"n": 0}

    def counting_flaky():
        calls["n"] += 1
        raise ConnectionError("down")

    try:
        cb.call(counting_flaky)
        check("OPEN 快速失败抛 CircuitOpenError", False)
    except CircuitOpenError:
        check("OPEN 快速失败抛 CircuitOpenError", True)
    check("OPEN 期间不调下游", calls["n"] == 0, f"n={calls['n']}")

    # 冷却结束 → HALF_OPEN 探测成功 → CLOSED
    time.sleep(0.25)
    cb.call(lambda: "ok")
    check("冷却后半开探测成功 → CLOSED", cb.state == "CLOSED", cb.state)
    check("复位失败计数", cb.consecutive_failures == 0)

    # 半开探测失败 → 回 OPEN
    cb2 = CircuitBreaker("test2", failure_threshold=2, cooldown=0.15)
    for _ in range(2):
        with contextlib.suppress(ConnectionError):
            cb2.call(flaky)
    check("连续失败2次 → OPEN", cb2.state == "OPEN")
    time.sleep(0.2)
    try:
        cb2.call(flaky)
        check("半开失败回 OPEN", False)
    except ConnectionError:
        pass
    check("半开探测失败 → 回 OPEN", cb2.state == "OPEN", cb2.state)


# ================= C. 降级链单测 =================

def test_degrade_chain():
    print("C. 降级链单测（主源/备用/兜底/业务错误/熔断降级）")
    ok_result = {"ok": True}

    # 主源成功
    result, meta = degrade_chain(lambda: ok_result, retries=1, base_delay=0.01)
    check("主源成功 level=0 不降级", meta["level"] == 0 and not meta["degraded"] and result == ok_result,
          f"level={meta['level']}")

    # 主源失败（可重试）→ 备用成功
    calls = {"p": 0, "b": 0}

    def bad_primary():
        calls["p"] += 1
        raise ConnectionError("down")

    def backup():
        calls["b"] += 1
        return {"source": "backup"}

    result, meta = degrade_chain(bad_primary, backups=(backup,), retries=1, base_delay=0.01)
    check("主源失败→备用成功 level=1 degraded", meta["level"] == 1 and meta["degraded"]
          and result["source"] == "backup", f"level={meta['level']}")

    # 主源+备用全失败 → fallback
    def bad_backup():
        raise ConnectionError("backup down")

    result, meta = degrade_chain(bad_primary, backups=(bad_backup,),
                                 fallback=lambda: {"error": "服务暂不可用"},
                                 retries=1, base_delay=0.01)
    check("全失败→fallback fallback_used", meta["fallback_used"] and meta["degraded"]
          and result == {"error": "服务暂不可用"}, f"level={meta['level']}")

    # 业务错误（409）→ 直接透传，不降级
    def biz_fail():
        raise BizApiError(409, "order PO-0001 is closed, cannot modify")

    try:
        degrade_chain(biz_fail, backups=(backup,), fallback=lambda: {"error": "兜底"},
                      retries=1, base_delay=0.01, retryable=is_retryable_http)
        check("业务错误透传不降级", False)
    except BizApiError as e:
        check("业务错误透传不降级", e.status_code == 409, str(e))

    # 熔断 OPEN → 不重试，直接降级备用
    cb = CircuitBreaker("cb_test", failure_threshold=1, cooldown=60)
    with contextlib.suppress(ConnectionError):
        cb.call(lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    result, meta = degrade_chain(lambda: cb.call(lambda: "never"),
                                 backups=(backup,), retries=3, base_delay=0.01,
                                 retryable=is_retryable_http)
    check("熔断 OPEN → 不重试直接降级备用", meta["level"] == 1 and result["source"] == "backup",
          f"level={meta['level']}")


# ================= D. 工具集成（真实服务） =================

def test_d1_normal(base: str):
    print("D1. 正常实例（读/写/幂等/业务错误透传）")
    tools = OrderTools(base, retries=2, base_delay=0.05)
    rep = ReportTools(base, retries=2, base_delay=0.05)

    # 读
    r = tools.query_order("PO-0001")
    assert r.data is not None
    check("query_order 成功", r.success and r.data["order_id"] == "PO-0001" and r.data["supplier_name"],
          f"attempts={r.attempts}")
    check("主源成功不降级", r.level == 0 and not r.degraded)
    check("成功后快照已写", "PO-0001" in tools.snapshot)

    # 写（幂等 key 防重）
    key = str(uuid.uuid4())
    r1 = tools.update_order("PO-0002", amount=9000.00, idempotency_key=key)
    r2 = tools.update_order("PO-0002", amount=9000.00, idempotency_key=key)
    assert r1.data is not None and r2.data is not None
    check("update_order 成功", r1.success and r1.data["amount"] == 9000.0)
    check("幂等：同 key 重复提交 body 一致（只执行一次）",
          r1.data == r2.data and r1.data["updated_at"] == r2.data["updated_at"],
          f"updated_at 一致={r1.data['updated_at'] == r2.data['updated_at']}")

    # 业务错误透传（不重试不降级）
    r = tools.query_order("PO-9999")
    check("不存在的订单 → 业务错误透传", not r.success and r.meta.get("status_code") == 404,
          f"status={r.meta.get('status_code')}")
    r = tools.update_order("PO-0019", amount=1)  # closed 订单
    check("closed 订单改金额 → 409 业务拒绝", not r.success and r.meta.get("status_code") == 409,
          f"msg={r.error}")
    r = tools.cancel_order("PO-0014", reason="test")  # shipped 订单
    check("shipped 订单取消 → 409 业务拒绝", not r.success and r.meta.get("status_code") == 409)

    # 写成功
    r = tools.cancel_order("PO-0005", reason="供应商停产", idempotency_key=str(uuid.uuid4()))
    assert r.data is not None
    check("cancel_order 成功", r.success and r.data["status"] == "closed")

    # 报表
    r = rep.generate_report("inventory")
    assert r.data is not None
    check("库存报表成功", r.success and r.data["summary"]["total_items"] == 15)
    r = rep.generate_report("reconciliation", from_date="2026-07-01", to_date="2026-07-31")
    assert r.data is not None
    check("对账报表成功", r.success and r.data["summary"]["order_count"] == 9)
    r = rep.generate_report("sales")
    check("非法报表类型 → 业务错误", not r.success and "sales" in (r.error or ""))


def test_d2_fail50(base: str):
    print("D2. BIZ_FAIL_RATE=0.5（重试 + 快照降级救回，成功率 ≥ 90%）")
    tools = OrderTools(base, retries=3, base_delay=0.03)
    total = 40
    succ = 0
    attempts_list = []
    for _ in range(total):
        r = tools.query_order("PO-0009")
        if r.success:
            succ += 1
        attempts_list.append(r.attempts)
    rate = succ / total
    # ① 成功率（含重试 + 快照降级）≥ 90%
    check("50% 失败后成功率 ≥ 90%（含重试）", rate >= 0.90, f"成功率={rate:.2%}")
    # ② 重试确实发生（至少一次调用经历了重试）
    check("重试确实发生（存在 attempts≥2）", max(attempts_list) >= 2,
          f"max attempts={max(attempts_list)}")
    # ③ 快照降级兜住：成功率 1.0 时说明备用源生效（快照"上次成功结果"）
    check("快照降级兜住持续失败（成功率接近 100%）", rate >= 0.95, f"成功率={rate:.2%}")
    # ④ 系统不崩：每次调用都返回 ToolResult
    check("所有调用返回 ToolResult（不抛异常）", len(attempts_list) == total)


def test_d3_500_mode(base: str, normal_base: str):
    print("D3. BIZ_500_MODE=/api/v1/orders/PO-0001（快照降级 + 独立熔断）")
    # 用正常实例查询 PO-0001 写入共享快照（模拟"上次成功结果"）
    snapshot: dict = {}
    ok_tools = OrderTools(normal_base, retries=2, base_delay=0.05)
    r = ok_tools.query_order("PO-0001")
    check("正常实例查询成功（建立快照）", r.success)
    snapshot.update(ok_tools.snapshot)

    tools = OrderTools(base, retries=2, base_delay=0.05, snapshot=snapshot)
    qb = tools._get_breaker("query_order")
    cb = tools._get_breaker("cancel_order")

    # ① 快照降级：PO-0001 主源恒 500 → 重试 → 备用快照命中
    r = tools.query_order("PO-0001")
    assert r.data is not None
    check("主源500→备用快照命中（degraded, level=1）", r.success and r.degraded
          and r.level == 1 and r.data["order_id"] == "PO-0001", f"level={r.level}")

    # ② 连续失败（主源持续 500）触发 query 熔断：PO-1111 无快照，主源+备用全败 → fallback
    for _ in range(5):
        tools.query_order("PO-1111")
    check("主源连续失败5次 → query 熔断 OPEN", qb.state == "OPEN", qb.state)

    # ③ 熔断 OPEN：不再打主源，直接快照/兜底（图不崩）
    r = tools.query_order("PO-0001")
    check("熔断中快照降级仍工作（不崩）", r.success and r.degraded, f"level={r.level}")
    r = tools.query_order("PO-1111")
    check("熔断中无快照 → fallback 兜底", not r.success and "服务暂不可用" in (r.error or ""))

    # ④ 每工具独立熔断：query OPEN 不影响 cancel
    r = tools.cancel_order("PO-0005", reason="独立熔断验证")
    check("cancel 熔断器独立（query OPEN 时 cancel 正常）", r.success and not r.degraded
          and cb.state == "CLOSED", f"cancel state={cb.state}")


def test_d4_fail100(base: str):
    print("D4. BIZ_FAIL_RATE=1.0（熔断 OPEN + fallback，图不崩）")
    tools = OrderTools(base, retries=2, base_delay=0.02)
    qb = tools._get_breaker("query_order")
    results = []
    for _ in range(8):
        r = tools.query_order("PO-0001")
        results.append((r.success, r.circuit_state))
    check("8 次调用后熔断 OPEN", qb.state == "OPEN", qb.state)
    check("全部返回不抛异常（图不崩）", len(results) == 8)
    last = results[-1]
    check("熔断后仍返回（fallback 兜底，success=False）", last[0] is False and last[1] == "OPEN", str(last))

    # update 独立熔断器：连续 5 次失败触发 OPEN，且每次返回不崩
    ru = None
    for _ in range(5):
        ru = tools.update_order("PO-0002", amount=1, idempotency_key=str(uuid.uuid4()))
    assert ru is not None
    check("update 连续失败5次熔断 OPEN",
          tools._get_breaker("update_order").state == "OPEN",
          tools._get_breaker("update_order").state)
    check("update 熔断降级不崩（fallback, success=False）",
          not ru.success and "服务暂不可用" in (ru.error or ""), str(ru.error))


def main():
    print(f"W19 Day3 工具层 + 可靠层测试 ｜ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    test_registry()
    test_circuit_breaker()
    test_degrade_chain()

    # 正常实例（D1 + D3 共享快照源）
    proc_n, base_n = start_server()
    try:
        test_d1_normal(base_n)
        proc_f, base_f = start_server({"BIZ_500_MODE": "/api/v1/orders/PO-0001"})
        try:
            test_d3_500_mode(base_f, base_n)
        finally:
            stop_server(proc_f)
    finally:
        stop_server(proc_n)

    # D2：50% 失败
    proc2, base2 = start_server({"BIZ_FAIL_RATE": "0.5"})
    try:
        test_d2_fail50(base2)
    finally:
        stop_server(proc2)

    # D4：100% 失败
    proc4, base4 = start_server({"BIZ_FAIL_RATE": "1.0"})
    try:
        test_d4_fail100(base4)
    finally:
        stop_server(proc4)

    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n===== 汇总：{passed}/{total} PASS =====")
    failed = [(n, d) for n, ok, d in _results if not ok]
    for name, detail in failed:
        print(f"  FAIL: {name} {detail}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
