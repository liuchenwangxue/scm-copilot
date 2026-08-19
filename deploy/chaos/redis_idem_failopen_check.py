r"""演练二辅助：Redis 挂时幂等 fail-open 降 SQLite 验证（纯逻辑，本地可跑）。

验证（W26 Day2 手册预期）：
- Redis 不可用 → IdempotencyStore fail-open 降 sqlite
- 同 key 重复执行 → 只执行一次（幂等语义仍成立，Stripe 语义不破）

用法：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/redis_idem_failopen_check.py
"""
import pathlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


def main() -> None:
    from app.shared.reliability.idempotency import IdempotencyStore, execute_idempotent
    from app.shared.reliability.redis_client import RedisClient

    rc = RedisClient()
    print(f"redis available: {rc.available}")

    store = IdempotencyStore(pathlib.Path(tempfile.mkdtemp()) / "idem.db", backend="auto")
    count = {"n": 0}

    def once():
        count["n"] += 1
        return {"ok": True, "n": count["n"]}

    r1, h1 = execute_idempotent(store, "s1", "update_order", "PO-1", once)
    r2, h2 = execute_idempotent(store, "s1", "update_order", "PO-1", once)
    print(f"exec_count={count['n']} hit1={h1} hit2={h2} r1={r1}")
    assert count["n"] == 1 and h1 is False and h2 is True, "幂等 fail-open 语义失败"
    print("PASS: Redis 挂 -> 幂等降 SQLite，同 key 只执行一次（幂等语义成立）")

    # 查询缓存 fail-open：Redis 挂 → 内存兜底
    from app.shared.reliability.cache import QueryCache

    qc = QueryCache(ttl=60, redis_client=rc)
    qc.set({"v": 1}, "biz", "demo")
    val, hit = qc.get("biz", "demo")
    print(f"query_cache hit={hit} val={val}")
    assert hit and val == {"v": 1}, "查询缓存内存兜底失败"
    print("PASS: 查询缓存 Redis 挂 -> 内存兜底仍命中")

    # 分布式锁 fail-open：Redis 挂 → 放行（锁退化为无锁）
    from app.shared.reliability.distributed_lock import DistributedLock

    lock = DistributedLock("chaos-test", ttl=30, redis_client=rc)
    got = lock.acquire()
    print(f"distributed_lock acquired={got}")
    assert got is True, "锁 fail-open 放行失败"
    lock.release()
    print("PASS: 分布式锁 Redis 挂 -> fail-open 放行（不卡死）")

    print("\n=== Redis fail-open 降级链验证完成 ===")


if __name__ == "__main__":
    main()
