"""Redis 客户端封装（★ W21 Day3）：统一连接 + 健康检查 + fail-open 语义。

设计（生产视角）：
- 懒连接单例：模块级 get_redis_client()，进程内共享一个连接池。
- 健康检查缓存：ping 失败后，`available` 在一段时间内保持 False
  （REDIS_SOCKET_TIMEOUT 内不再反复试连，避免每次操作都吃超时——fail-open 快速判定）。
- 所有操作 fail-open：返回 None/False 表示"Redis 不可用"，由调用方决定降级
  （幂等→sqlite、缓存→内存、锁→放行打 WARNING）。

接口：
    RedisClient(url, enabled, timeout)
    .available : bool            # 快速判定 Redis 是否可用（缓存状态）
    .ping()    : bool            # 实时探活
    .set(key, value, ex=None)    -> bool
    .set_nx(key, value, ex=None) -> bool   # SET key value NX EX ttl（SETNX 原子占位）
    .get(key)                    -> str|None
    .delete(key)                 -> bool
    .delete_if_equals(key, expected) -> bool  # 校验式删除（锁释放，防误删他人锁）
"""

from __future__ import annotations

import builtins
import threading
import time

from app.shared import config

# ping 失败后的"冷却"时间：冷却期内不再试连（fail-open 快速判定）
_AVAIL_COOLDOWN_S = 5.0


class RedisClient:
    def __init__(
        self, url: str | None = None, enabled: bool | None = None, timeout: float | None = None
    ):
        self.url = url or config.REDIS_URL
        self.enabled = config.REDIS_ENABLED if enabled is None else enabled
        self.timeout = config.REDIS_SOCKET_TIMEOUT if timeout is None else timeout
        self._client = None
        self._lock = threading.Lock()
        # 健康状态缓存：_last_fail_ts = 最近一次 ping 失败时间（0=从未失败）
        self._last_fail_ts = 0.0

    # ---------- 连接 ----------

    def _connect(self):
        """懒创建 redis 客户端（decode_responses=True：值即 str，省手动解码）。"""
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.url,
                socket_connect_timeout=self.timeout,
                socket_timeout=self.timeout,
                decode_responses=True,
            )
        return self._client

    def _recover(self):
        """退出冷却：连接建立 + ping 通过时调用。"""
        self._last_fail_ts = 0.0

    # ---------- 健康检查（fail-open 快速判定） ----------

    def ping(self) -> bool:
        """实时探活。失败记录时间戳（触发冷却）。"""
        if not self.enabled:
            return False
        try:
            ok = bool(self._connect().ping())
        except Exception:
            ok = False
        if ok:
            self._recover()
        else:
            self._last_fail_ts = time.time()
        return ok

    @property
    def available(self) -> bool:
        """快速判定：冷却期内直接 False，否则实时 ping 一次。"""
        if not self.enabled:
            return False
        if self._last_fail_ts and (time.time() - self._last_fail_ts) < _AVAIL_COOLDOWN_S:
            return False
        return self.ping()

    # ---------- 通用操作（fail-open：返回 None/False） ----------

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        try:
            return bool(self._connect().set(key, value, ex=ex))
        except Exception:
            self._last_fail_ts = time.time()
            return False

    def set_nx(self, key: str, value: str, ex: int | None = None) -> bool:
        """SET key value NX EX ttl：键不存在才设置（SETNX 语义，原子）。"""
        try:
            return bool(self._connect().set(key, value, nx=True, ex=ex))
        except Exception:
            self._last_fail_ts = time.time()
            return False

    def get(self, key: str) -> str | None:
        try:
            return self._connect().get(key)
        except Exception:
            self._last_fail_ts = time.time()
            return None

    def delete(self, key: str) -> bool:
        try:
            return bool(self._connect().delete(key))
        except Exception:
            self._last_fail_ts = time.time()
            return False

    def delete_if_equals(self, key: str, expected: str) -> bool:
        """校验式删除：仅当当前值 == expected 时才 DEL（分布式锁释放用）。

        用 Lua 脚本原子执行（GET 比较 + DEL），比"GET→比较→DEL"两段式安全
        （避免中间被其他进程改写的 TOCTOU——防误删他人刚抢到的锁）。"""
        lua = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            return bool(self._connect().eval(lua, 1, key, expected))
        except Exception:
            self._last_fail_ts = time.time()
            return False

    # ---------- 集合操作（W23 Day5 语义缓存索引用；fail-open） ----------

    def sadd(self, key: str, *members: str) -> int:
        """SADD：集合添加。返回新增成员数（0=已存在）。"""
        try:
            return int(self._connect().sadd(key, *members) or 0)
        except Exception:
            self._last_fail_ts = time.time()
            return 0

    def smembers(self, key: str) -> builtins.set[str]:
        """SMEMBERS：集合全部成员（语义缓存索引遍历）。"""
        try:
            return set(self._connect().smembers(key))
        except Exception:
            self._last_fail_ts = time.time()
            return set()

    def srem(self, key: str, *members: str) -> int:
        """SREM：集合移除。"""
        try:
            return int(self._connect().srem(key, *members) or 0)
        except Exception:
            self._last_fail_ts = time.time()
            return 0

    def delete_many(self, keys: list[str]) -> int:
        """批量 DEL（缓存失效用，语义缓存版本切换时清理旧前缀）。"""
        if not keys:
            return 0
        try:
            return int(self._connect().delete(*keys) or 0)
        except Exception:
            self._last_fail_ts = time.time()
            return 0

    def eval(self, lua: str, numkeys: int, *keys_and_args) -> object:
        """EVAL Lua 脚本（★ W25 Day5：API Key 令牌桶限速用）。

        fail-open：异常返回 None——调用方按"Redis 不可用 → 放行"降级
        （配额是软约束，Redis 抖动不拒绝请求；手册 fail-open 原则）。
        """
        try:
            return self._connect().eval(lua, numkeys, *keys_and_args)
        except Exception:
            self._last_fail_ts = time.time()
            return None

    def scan_keys(self, pattern: str, count: int = 500) -> list[str]:
        """SCAN 遍历匹配键（★ W25 Day2：vector_cleanup 语义缓存过期键扫描用）。

        fail-open：异常返回 []（清理是旁路，不因 Redis 抖动抛错）。
        `count` 每轮迭代游标提示数；跨批游标由 redis-py 内部处理，循环直到游标归零。
        """
        try:
            client = self._connect()
            keys: list[str] = []
            cursor = 0
            while True:
                cursor, batch = client.scan(cursor=cursor, match=pattern, count=count)
                keys.extend(batch)
                if cursor == 0:
                    break
            return keys
        except Exception:
            self._last_fail_ts = time.time()
            return []


# 模块级单例（进程内共享）
_client = None
_client_lock = threading.Lock()


def get_redis_client() -> RedisClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = RedisClient()
    return _client
