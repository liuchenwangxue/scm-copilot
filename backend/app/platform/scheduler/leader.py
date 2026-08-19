"""★ 调度任务级互斥（W25 Day1）：Redis leader 锁装饰器。

设计（复用 W19/W21 `reliability/distributed_lock.py` 的 SETNX + owner 校验，改造成 async 装饰器）：
- 语义：**调度器全实例运行、任务级互斥**——每个触发点所有实例都跑回调，
  但只有抢到 `lock:job:{name}` 的实例真正执行，其余返回 `SkipResult`。
  实例挂了锁自动过期（TTL），另一实例下轮接管——比"单实例跑调度"更高可用。
- 抢锁：`SET lock:job:{name} {owner} NX EX ttl`（复用 RedisClient.set_nx，原子）
- 释放：`delete_if_equals` Lua 原子（GET 校验 owner 一致才 DEL，防误删他人刚抢到的锁）
- ★ W27 D3 A6 fail-open 兜底：Redis 不可用 → 同 key 进程内 `asyncio.Lock()` 互斥
  （原"无锁放行"升级——同进程并发串行化；本地锁不跨实例，跨实例重复仍由任务幂等键兜底：
  日报用日期键、KB 用 uuid5 内容寻址，W25 Day2/3 实现）+ metrics `scm_lock_local_fallback_total`。
- 返回 `SkipResult(reason)`：未抢到锁的实例返回该值，由调用方记录 job_runs（status=skipped），
  成为"双实例零重复"观测的依据。

面试话术（Q5）：三层防重复 = 调度器全实例高可用 + 任务级互斥（本装饰器）+ 任务幂等键双保险。
"""

import asyncio
from dataclasses import dataclass
from functools import wraps
from uuid import uuid4

from app.shared.obs import logger as obs_logger
from app.shared.reliability.redis_client import RedisClient, get_redis_client

_log = obs_logger.get_logger("scheduler.leader")

# ★ A6：本地互斥兜底注册表——{锁 key: asyncio.Lock}（Redis 挂时同进程串行化）
_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}


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
            # ★ A6 fail-open 兜底：Redis 不可用 → 同 key 进程内 asyncio.Lock 互斥
            #   （同进程并发串行化，不再"无锁放行"；跨实例重复由任务幂等键兜底）
            if not rc.available:
                local = _LOCAL_LOCKS.setdefault(key, asyncio.Lock())
                async with local:
                    from app.shared.obs.metrics import inc_lock_fallback
                    inc_lock_fallback("leader")
                    obs_logger.log_event(
                        _log, "lock_local_fallback", level="warning",
                        lock=key, backend="local",
                        note="本地互斥仅防同进程并发，跨实例重复由任务幂等键兜底（A6 边界）")
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
