"""ops 域轻量单测（W23 Day4 由 stage3-b `tests/test_b_core.py` 迁移）。

平台化后调整：
- import 从 `reliability.xxx` 改为 `app.shared.reliability.xxx`
- JWT/RBAC 测试移除——认证/权限已由平台 `test_auth.py` / `test_rbac.py` 覆盖（W23 Day3）

覆盖（CI 可跑——纯逻辑 + Redis 可用时才测真 Redis）：
- idempotency：build_key 确定性 + sqlite 兜底（backend="sqlite" 强制）+ 失败不缓存可重试
- redis_client：坏地址 fail-open（available=False、操作返回 None/False）
- retry_policy：重试次数/恢复标记 + 业务错误不重试
"""
import contextlib

# ==================== idempotency（sqlite 兜底 + 键确定性） ====================

def test_idem_key_deterministic():
    from app.shared.reliability.idempotency import IdempotencyStore
    k1 = IdempotencyStore.build_key("s1", "update_order", "PO-0001")
    k2 = IdempotencyStore.build_key("s1", "update_order", "PO-0001")
    k3 = IdempotencyStore.build_key("s1", "update_order", "PO-0002")
    assert k1 == k2 and k1 != k3 and len(k1) == 64


def test_idem_sqlite_backend(tmp_path):
    """backend=sqlite：同 key 只执行 1 次 + 成功缓存 + 失败不缓存可重试。"""
    from app.shared.reliability.idempotency import IdempotencyStore, execute_idempotent
    store = IdempotencyStore(tmp_path / "idem.db", backend="sqlite")
    count = {"n": 0}

    def exec_once():
        count["n"] += 1
        return {"ok": True, "n": count["n"]}

    r1, h1 = execute_idempotent(store, "s1", "update_order", "PO-1", exec_once)
    r2, h2 = execute_idempotent(store, "s1", "update_order", "PO-1", exec_once)
    assert count["n"] == 1 and h1 is False and h2 is True and r1 == r2

    # 失败不缓存 → 可重试
    def exec_fail():
        count["n"] += 1
        raise ConnectionError("down")

    with contextlib.suppress(ConnectionError):
        execute_idempotent(store, "s2", "update_order", "PO-2", exec_fail)
    before = count["n"]
    key = IdempotencyStore.build_key("s2", "update_order", "PO-2")
    assert store.status(key) == "FAILED"
    with contextlib.suppress(ConnectionError):
        execute_idempotent(store, "s2", "update_order", "PO-2", exec_fail)
    assert count["n"] == before + 1


# ==================== redis_client（fail-open） ====================

def test_redis_fail_open():
    """坏地址：available=False，操作返回 None/False（不抛异常）。"""
    from app.shared.reliability.redis_client import RedisClient
    bad = RedisClient(url="redis://localhost:19999/0", enabled=True, timeout=0.3)
    assert not bad.available
    assert bad.set("k", "v") is False
    assert bad.set_nx("k", "v") is False
    assert bad.get("k") is None
    assert bad.delete("k") is False


# ==================== retry_policy（重试 + 业务错误） ====================

def test_retry_recovers():
    from app.shared.reliability.retry_policy import RetryPolicy
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("timeout")
        return "ok"

    rp = RetryPolicy(max_retries=3, base_delay=0.01)
    result, attempts, recovered = rp.run(flaky)
    assert result == "ok" and attempts == 2 and recovered is True


def test_retry_biz_error_not_retried():
    from app.shared.reliability.retry_policy import RetryPolicy

    class BizError(Exception):
        pass

    calls = {"n": 0}

    def biz_fail():
        calls["n"] += 1
        raise BizError("biz 400")

    rp = RetryPolicy(max_retries=3, base_delay=0.01,
                     retryable=lambda e: False)  # 业务错误不可重试
    with contextlib.suppress(BizError):
        rp.run(biz_fail)
    assert calls["n"] == 1  # 不可重试 → 只调 1 次
