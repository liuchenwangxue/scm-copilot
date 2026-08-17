"""★ 分布式锁（W21 Day3）：Redis SETNX + TTL + owner 校验释放。

用途：防并发写（如重复改单、并发报表计算）——并发 2 请求抢同一把锁，只有 1 个成功。

设计（生产视角，手册坑提示）：
- acquire：`SET name owner NX EX ttl`（原子占位 + TTL 自动过期 → 防死锁）
- owner：进程内唯一 token（uuid），释放时必须校验 owner（防误删他人锁）
- release：Lua 脚本（GET 比较 owner 一致才 DEL）——比"GET→比较→DEL"两段式安全
  （避免 TOCTOU：比较与删除之间被他人改写）
- 超时自动释放：TTL 到即过期（业务超时锁自动让出，无需手动）
- fail-open：Redis 不可用 → acquire 返回 True（不加锁放行）+ WARNING
  （降级不是拒绝——手册 Day3 故障降级原则）

接口：
    DistributedLock(name, ttl=30, redis_client=None)
    .acquire() -> bool
    .release() -> None
    .acquired : bool
    支持 with 语句（自动释放）
"""
import time
import uuid

from app.shared.reliability.redis_client import get_redis_client


class DistributedLock:
    """基于 Redis SETNX 的分布式锁（带 owner 校验释放 + TTL 防死锁）。"""

    def __init__(self, name: str, ttl: int = 30, redis_client=None,
                 retry_times: int = 0, retry_delay: float = 0.1):
        from app.shared import config
        self.name = f"lock:{name}"
        self.ttl = ttl if ttl is not None else config.REDIS_LOCK_TTL
        self.rc = redis_client or get_redis_client()
        self.retry_times = retry_times          # 抢锁重试次数（0=只试一次）
        self.retry_delay = retry_delay
        self._owner = str(uuid.uuid4())         # 进程内唯一 owner token
        self.acquired = False

    def acquire(self) -> bool:
        """尝试抢锁。成功 True；Redis 不可用 → fail-open 返回 True（打 WARNING）。"""
        if not self.rc.available:
            print(f"  [LOCK] Redis 不可用 → fail-open 放行（锁退化为无锁）: {self.name}")
            self.acquired = True   # 降级语义：不加锁直接放行（宁可多执行，不可卡死）
            return True
        for i in range(self.retry_times + 1):
            if self.rc.set_nx(self.name, self._owner, ex=self.ttl):
                self.acquired = True
                return True
            if i < self.retry_times:
                time.sleep(self.retry_delay)
        return False

    def release(self) -> None:
        """释放锁（owner 校验：只删自己的锁，防误删他人刚抢到的锁）。"""
        if not self.acquired:
            return
        if self.rc.available:
            # Lua 原子：GET 校验 owner == 自己才 DEL（防 TOCTOU 误删）
            self.rc.delete_if_equals(self.name, self._owner)
        self.acquired = False

    def __enter__(self) -> "DistributedLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
