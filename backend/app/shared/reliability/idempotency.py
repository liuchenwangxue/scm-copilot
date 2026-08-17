"""★ B6 分布式幂等（W19 Day4 欠账 + W21 Day3 升级真 Redis）：SETNX 语义

W21 Day3 升级（手册要求"sqlite → Redis SETNX"）：
- 生产默认 backend="auto"：Redis 可用 → 真 SETNX（`SET key value NX EX ttl`）；Redis 不可用 → fail-open 回 sqlite（W19 代码保留为兜底）
- TTL（REDIS_IDEM_TTL=300s）：幂等 key 超时自动过期 → 允许安全重试（Stripe 语义：别永久缓存业务结果）
- 只缓存成功结果（status=SUCCESS + result_json）；RUNNING/FAILED 不缓存 → 同 key 可安全重试

设计（对照 Stripe 幂等语义，W19 原逻辑保留）：
- 幂等键 = sha256(会话 + 操作 + 目标) —— 同一会话对同一目标做同一操作，只会执行一次
- claim()：SETNX 原子占位 → True 表示首次占用
- 生产环境 claim = Redis SETNX（跨进程原子，真分布式）；sqlite 用 INSERT OR IGNORE（本地兜底）

★ 幂等键生成时机（手册坑）：在**审批发起时**生成幂等键（防重复审批），批准后执行带同一个 key。

接口（与 W19 完全一致，调用方零改动）：
    IdempotencyStore(db_path, backend="auto", namespace="scm", redis_client=None)
    .build_key / .claim / .complete / .mark_failed / .get_result / .status
    execute_idempotent(store, ...)   # 不变

面试话术："幂等键的目的是允许客户端重试而不重复副作用（Stripe 语义）——
成功才缓存，失败不缓存；重复提交返回首次成功结果，绝不二次执行。
W21 升级为 Redis SETNX（跨实例原子），Redis 挂了自动降级 sqlite（fail-open），
TTL 300s 保证超时后可重试。"
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

_STATUS_RUNNING = "RUNNING"
_STATUS_SUCCESS = "SUCCESS"
_STATUS_FAILED = "FAILED"

DEFAULT_TTL = 300  # 幂等 key TTL（config.REDIS_IDEM_TTL，超时后允许重试）


# ==================== 后端实现 ====================

class _SqliteBackend:
    """W19 原实现（本地兜底）：sqlite PRIMARY KEY 唯一约束模拟 SETNX。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_ops (
                    idem_key      TEXT PRIMARY KEY,
                    session_id    TEXT NOT NULL,
                    operation     TEXT NOT NULL,
                    target        TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    result_json   TEXT,
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL
                )
            """)

    def claim(self, idem_key: str, session_id: str, operation: str, target: str) -> bool:
        """SETNX 语义（Stripe）：首次占用 True；已存在时——
        - RUNNING/SUCCESS：False（并发占用 / 幂等命中）
        - FAILED：重置回 RUNNING 返回 True（失败不缓存 → 同 key 可安全重试）"""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO idempotency_ops "
                "(idem_key, session_id, operation, target, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (idem_key, session_id, operation, target, _STATUS_RUNNING, now, now))
            if cur.rowcount == 1:
                return True
            # key 已存在：仅 FAILED 允许重试（重置 RUNNING 并更新元数据）
            row = conn.execute(
                "SELECT status FROM idempotency_ops WHERE idem_key=?", (idem_key,)).fetchone()
            if row is not None and row["status"] == _STATUS_FAILED:
                conn.execute(
                    "UPDATE idempotency_ops SET status=?, session_id=?, operation=?, target=?, "
                    "created_at=?, updated_at=? WHERE idem_key=?",
                    (_STATUS_RUNNING, session_id, operation, target, now, now, idem_key))
                return True
            return False

    def complete(self, idem_key: str, result: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE idempotency_ops SET status=?, result_json=?, updated_at=? WHERE idem_key=?",
                (_STATUS_SUCCESS, json.dumps(result, ensure_ascii=False), time.time(), idem_key))

    def mark_failed(self, idem_key: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE idempotency_ops SET status=?, result_json=?, updated_at=? WHERE idem_key=?",
                (_STATUS_FAILED, json.dumps({"error": str(error)}, ensure_ascii=False),
                 time.time(), idem_key))

    def get_result(self, idem_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, result_json FROM idempotency_ops WHERE idem_key=?",
                (idem_key,)).fetchone()
        if row is None or row["status"] != _STATUS_SUCCESS or not row["result_json"]:
            return None
        return json.loads(row["result_json"])

    def status(self, idem_key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM idempotency_ops WHERE idem_key=?",
                (idem_key,)).fetchone()
        return row["status"] if row else None


class _RedisBackend:
    """★ W21 Day3 真 Redis 幂等：SET key value NX EX ttl（SETNX + TTL）。

    value = JSON 载荷 {session_id, operation, target, status, result/error, updated_at}。
    - claim：set_nx（原子占位 + TTL）
    - complete/mark_failed：set（覆盖 payload，重新记 TTL）
    - get_result：get → status==SUCCESS 才返回 result（RUNNING/FAILED 不返回 → 可重试）
    - TTL 过期 → key 消失 → 再次 claim 成功 → 允许重试（不永久缓存业务结果）
    """

    def __init__(self, redis_client, namespace: str = "scm", ttl: int = DEFAULT_TTL):
        self.rc = redis_client
        self.prefix = f"{namespace}:idem:"
        self.ttl = ttl

    def _k(self, idem_key: str) -> str:
        return f"{self.prefix}{idem_key}"

    def claim(self, idem_key: str, session_id: str, operation: str, target: str) -> bool:
        """SETNX 语义：首次 True；RUNNING/SUCCESS → False；FAILED → 重置 RUNNING True（可重试）。"""
        payload = json.dumps({
            "session_id": session_id, "operation": operation, "target": target,
            "status": _STATUS_RUNNING, "updated_at": time.time()},
            ensure_ascii=False)
        if self.rc.set_nx(self._k(idem_key), payload, ex=self.ttl):
            return True
        # key 已存在：FAILED 允许重试（覆盖回 RUNNING，重置 TTL）
        if self.status(idem_key) == _STATUS_FAILED:
            return self.rc.set(self._k(idem_key), payload, ex=self.ttl)
        return False

    def complete(self, idem_key: str, result: dict) -> None:
        payload = json.dumps({
            "status": _STATUS_SUCCESS, "result": result,
            "updated_at": time.time()}, ensure_ascii=False)
        self.rc.set(self._k(idem_key), payload, ex=self.ttl)

    def mark_failed(self, idem_key: str, error: str) -> None:
        payload = json.dumps({
            "status": _STATUS_FAILED, "error": str(error),
            "updated_at": time.time()}, ensure_ascii=False)
        self.rc.set(self._k(idem_key), payload, ex=self.ttl)

    def get_result(self, idem_key: str) -> dict | None:
        raw = self.rc.get(self._k(idem_key))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if data.get("status") != _STATUS_SUCCESS or "result" not in data:
            return None
        return data["result"]

    def status(self, idem_key: str) -> str | None:
        raw = self.rc.get(self._k(idem_key))
        if not raw:
            return None
        try:
            return json.loads(raw).get("status")
        except (ValueError, TypeError):
            return None


# ==================== 门面（W19 接口不变） ====================

class IdempotencyStore:
    """幂等存储门面：backend 决定实现（auto=redis 优先，sqlite 兜底）。

    backend 可选值：
    - "auto"  （默认）：Redis 可用 → Redis；否则 fail-open → sqlite
    - "redis"：强制 Redis；Redis 不可用 → fail-open → sqlite（并打日志）
    - "sqlite"：强制 sqlite（本地/无 Redis 环境）
    """

    def __init__(self, db_path: str | Path, backend: str = "auto",
                 namespace: str = "scm", redis_client=None, ttl: int | None = None):
        self._sqlite = _SqliteBackend(db_path)
        from app.shared.reliability.redis_client import get_redis_client
        self._redis = _RedisBackend(
            redis_client or get_redis_client(), namespace=namespace,
            ttl=ttl or DEFAULT_TTL)
        self.backend = backend

    def resolve_backend(self):
        """解析当前生效的后端（一次操作内固定——★ W21 Day6 修复）。

        原实现 `_active` 是属性（每次访问重新判定），Redis 状态抖动时同一操作的
        claim/complete/get_result 可能走不同后端（数据分裂）。改为显式解析一次，
        由 execute_idempotent 在一次调用内复用。"""
        if self.backend == "sqlite":
            return self._sqlite
        rc = self._redis.rc
        if rc.available:
            return self._redis
        # fail-open：Redis 不可用 → sqlite（W19 代码保留为兜底）
        if self.backend == "redis":
            print("  [IDEM] Redis 不可用 → fail-open 降级 sqlite（幂等语义仍成立）")
        return self._sqlite

    @property
    def _active(self):
        """当前生效的后端（保持原接口；execute_idempotent 内请用 resolve_backend 固定）。"""
        return self.resolve_backend()

    # ---- 幂等键生成（审批发起时调用） ----

    @staticmethod
    def build_key(session_id: str, operation: str, target: str) -> str:
        raw = f"{session_id}::{operation}::{target}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---- SETNX 语义 ----

    def claim(self, idem_key: str, session_id: str, operation: str, target: str,
              backend=None) -> bool:
        b = backend or self.resolve_backend()
        return b.claim(idem_key, session_id, operation, target)

    def complete(self, idem_key: str, result: dict, backend=None) -> None:
        b = backend or self.resolve_backend()
        b.complete(idem_key, result)

    def mark_failed(self, idem_key: str, error: str, backend=None) -> None:
        b = backend or self.resolve_backend()
        b.mark_failed(idem_key, error)

    def get_result(self, idem_key: str, backend=None) -> dict | None:
        b = backend or self.resolve_backend()
        return b.get_result(idem_key)

    def status(self, idem_key: str, backend=None) -> str | None:
        b = backend or self.resolve_backend()
        return b.status(idem_key)


def execute_idempotent(store: IdempotencyStore, session_id: str, operation: str,
                       target: str, func, audit=None, *args, **kwargs):
    """幂等执行包装：同 key 重复 → 返回首次结果；首次 → 执行并缓存。

    Returns:
        (result, hit)：hit=True 表示幂等命中（未重复执行）
    """
    key = store.build_key(session_id, operation, target)
    # ★ 一次操作内固定后端（W21 Day6：resolve_backend 只调一次，
    #    claim/complete/get_result 用同一后端，避免 Redis 抖动导致数据分裂）
    backend = store.resolve_backend()
    cached = store.get_result(key, backend=backend)
    if cached is not None:
        if audit:
            audit.log("idempotency_hit", session_id=session_id, operation=operation,
                      target=target, idem_key=key[:12], result="cached")
        return cached, True
    if not store.claim(key, session_id, operation, target, backend=backend):
        # 并发/重复占用：等一小段再取缓存结果（生产用 Redis SETNX + 自旋）
        for _ in range(20):
            time.sleep(0.01)
            cached = store.get_result(key, backend=backend)
            if cached is not None:
                if audit:
                    audit.log("idempotency_hit", session_id=session_id, operation=operation,
                              target=target, idem_key=key[:12], result="cached-after-spin")
                return cached, True
        raise RuntimeError(f"idempotency claim failed for key {key[:12]}")
    try:
        result = func(*args, **kwargs)
    except Exception as e:
        store.mark_failed(key, str(e), backend=backend)
        raise
    store.complete(key, result, backend=backend)
    return result, False
