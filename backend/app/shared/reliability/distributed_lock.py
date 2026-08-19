"""★ 分布式锁（W21 Day3）：Redis SETNX + TTL + owner 校验释放。
★ W27 Day3 A6：Redis 挂 fail-open 兜底升级——本地互斥（不再"无锁放行"）。

用途：防并发写（如重复改单、并发报表计算）——并发 2 请求抢同一把锁，只有 1 个成功。

设计（生产视角，手册坑提示）：
- acquire：`SET name owner NX EX ttl`（原子占位 + TTL 自动过期 → 防死锁）
- owner：进程内唯一 token（uuid），释放时必须校验 owner（防误删他人锁）
- release：Lua 脚本（GET 比较 owner 一致才 DEL）——比"GET→比较→DEL"两段式安全
  （避免 TOCTOU：比较与删除之间被他人改写）
- 超时自动释放：TTL 到即过期（业务超时锁自动让出，无需手动）
- ★ A6 fail-open 兜底：Redis 不可用 → 同 key 进程内 threading.Lock 互斥
  （防同进程并发）+ metrics 计数器 `scm_lock_local_fallback_total` + DEGRADED 日志。
  边界（写进注释防误用）：本地锁只防同进程并发，跨实例仍可能并发——
  高危写另见幂等写路径 fail-closed（idempotency.py risk="write"，A7）。

接口：
    DistributedLock(name, ttl=30, redis_client=None)
    .acquire() -> bool
    .release() -> None
    .acquired : bool
    支持 with 语句（自动释放）
"""
import threading
import time
import uuid

from app.shared.obs import logger as obs_logger
from app.shared.reliability.redis_client import get_redis_client

_log = obs_logger.get_logger("reliability.lock")

# ★ A6：本地互斥兜底注册表——{lock key: threading.Lock}（Redis 挂时防同进程并发）
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _get_local_lock(key: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        if key not in _LOCAL_LOCKS:
            _LOCAL_LOCKS[key] = threading.Lock()
        return _LOCAL_LOCKS[key]


def reset_local_locks() -> None:
    """测试用：清空本地兜底锁注册表（避免跨测试互相持锁）。"""
    global _LOCAL_LOCKS
    with _LOCAL_LOCKS_GUARD:
        _LOCAL_LOCKS = {}


class DistributedLock:
    """基于 Redis SETNX 的分布式锁（带 owner 校验释放 + TTL 防死锁 + 本地互斥兜底）。"""

    def __init__(self, name: str, ttl: int = 30, redis_client=None,
                 retry_times: int = 0, retry_delay: float = 0.1):
        from app.shared import config
        self.name = f"lock:{name}"
        self.ttl = ttl if ttl is not None else config.REDIS_LOCK_TTL
        self.rc = redis_client or get_redis_client()
        self.retry_times = retry_times          # 抢锁重试次数（0=只试一次）
        self.retry_delay = retry_delay
        self._owner = str(uuid.uuid4())         # 进程内唯一 owner token
        self._local_lock: threading.Lock | None = None  # A6 本地兜底锁（非 None=走了兜底）
        self.acquired = False

    def acquire(self) -> bool:
        """尝试抢锁。成功 True；Redis 不可用 → 本地互斥兜底（A6，非阻塞抢本地锁）。"""
        if not self.rc.available:
            # ★ A6：Redis 挂不再"无锁放行"——同 key 进程内互斥兜底（宁可互斥，不可双写）
            local = _get_local_lock(self.name)
            if local.acquire(blocking=False):
                self.acquired = True
                self._local_lock = local
                from app.shared.obs.metrics import inc_lock_fallback
                inc_lock_fallback("lock")
                obs_logger.log_event(
                    _log, "lock_local_fallback", level="warning",
                    lock=self.name, backend="local",
                    note="本地互斥仅防同进程并发，跨实例仍可能并发（A6 边界）")
            else:
                self.acquired = False  # 本地锁被同进程其他线程持有 → 未获取
            return self.acquired
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
        if self._local_lock is not None:
            # A6 本地兜底锁：直接释放本地锁（无 Redis 语义）
            self._local_lock.release()
            self._local_lock = None
            self.acquired = False
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
