"""★ 调度任务级互斥（W25 Day1）：Redis leader 锁装饰器。

设计（复用 W19/W21 `reliability/distributed_lock.py` 的 SETNX + owner 校验，改造成 async 装饰器）：
- 语义：**调度器全实例运行、任务级互斥**——每个触发点所有实例都跑回调，
  但只有抢到 `lock:job:{name}` 的实例真正执行，其余返回 `SkipResult`。
  实例挂了锁自动过期（TTL），另一实例下轮接管——比"单实例跑调度"更高可用。
- 抢锁：`SET lock:job:{name} {owner} NX EX ttl`（复用 RedisClient.set_nx，原子）
- 释放：`delete_if_equals` Lua 原子（GET 校验 owner 一致才 DEL，防误删他人刚抢到的锁）
- fail-open：Redis 不可用 → 不加锁放行（宁可全实例跑，不可卡死）；
  "全实例跑"的副作用由任务幂等键兜底（日报用日期键、KB 用 uuid5 内容寻址，
  W25 Day2/3 实现）——手册六问#2：Redis 挂 → fail-open 全实例跑但任务幂等兜底。
- 返回 `SkipResult(reason)`：未抢到锁的实例返回该值，由调用方记录 job_runs（status=skipped），
  成为"双实例零重复"观测的依据。

面试话术（Q5）：三层防重复 = 调度器全实例高可用 + 任务级互斥（本装饰器）+ 任务幂等键双保险。
"""

from dataclasses import dataclass
from functools import wraps
from uuid import uuid4

from app.shared.reliability.redis_client import RedisClient, get_redis_client


@dataclass(frozen=True)
class SkipResult:
    """未抢到 leader 锁的任务跳过结果（调用方据此写 job_runs status=skipped）。"""

    job: str
    reason: str


def leader_lock(name: str, ttl: int = 300, redis_client: RedisClient | None = None):
    """任务级互斥装饰器：包一层 async 函数，抢锁失败返回 SkipResult。

    Args:
        name: 锁名（建议与 job 名一致，落在 Redis key `lock:job:{name}`）。
        ttl: 锁 TTL 秒。业务超时后锁自动过期让出（防死锁），下轮其他实例可接管。
        redis_client: 测试注入用；默认全局单例。

    Example:
        @leader_lock("daily_brief", ttl=300)
        async def run():
            ...
    """

    def deco(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            rc = redis_client or get_redis_client()
            owner = uuid4().hex
            key = f"lock:job:{name}"
            # fail-open：Redis 不可用 → 放行（锁退化为无锁，任务幂等兜底）
            if not rc.available:
                print(f"  [SCHED] leader 锁 Redis 不可用 → fail-open 放行（锁退化为无锁）: {key}")
                return await fn(*args, **kwargs)
            if not rc.set_nx(key, owner, ex=ttl):
                return SkipResult(job=name, reason=f"{name}: another instance holds lock")
            try:
                return await fn(*args, **kwargs)
            finally:
                # owner 校验后释放（Lua 原子，防误删他人刚抢到的锁）
                rc.delete_if_equals(key, owner)

        return wrapper

    return deco
