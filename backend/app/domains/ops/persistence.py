"""★ A8 生产级持久化：LangGraph checkpointer（W23 Day5 切 MySQL，W27 D1 连接池化）。

演进路线：
- W19 Day4：SqliteSaver（进程重启不丢）
- W21 Day5：AsyncSqliteSaver（async 图 + aiosqlite）
- ★ W23 Day5：AsyncMySaver（MySQL 权威库）——与审批/审计同源，双实例共享断点，
  历史 SQLite 断点已由 `scripts/migrate_sqlite_to_mysql.py` 无损迁移
  （358 断点 / 858 写入，含校验和比对）。
- ★ W27 D1：PooledAsyncMySaver（asyncmy 连接池）——单连接串行 → 池化并发。
  40 并发 P95=2087.1ms 未达 ≤1.5s 的根因（B1/A1/A2）修复：单连接 + 单锁把
  ops 全部请求串行排队（ops_query P95=3467.9ms），池化后并发写分散到池内连接。

后端选择：`CHECKPOINTER_BACKEND`（ops/config.py）
- `mysql`（默认）：PooledAsyncMySaver（连接池），连接平台库 scm_platform
- `sqlite`（回退）：AsyncSqliteSaver，本地 biz_agent.db（测试/无 MySQL 环境）

三个关键坑（Day5 实测，写入代码注释供面试讲）：
1. collation：langgraph-checkpoint-mysql 的 SELECT_SQL 用 json_table 生成临时列，
   硬编码 `CHARACTER SET utf8mb4`（MySQL 8 默认 utf8mb4_0900_ai_ci）；若表继承
   数据库默认 utf8mb4_unicode_ci，读回即报 1267 Illegal mix of collations。
   解决：setup() 建表后把 checkpointer 四表 CONVERT TO utf8mb4_0900_ai_ci。
2. 连接绑定事件循环：asyncmy 连接/连接池与创建它的 loop 绑定，必须在使用方 loop
   内首次创建（graph.get_biz_graph 内懒编译，本模块只做单例缓存；测试跨 loop
   复用须 reset_checkpointer() 重建池）。
3. setup() 在 lifespan/首次使用时调用（不在 import 时连库）。
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.domains.ops import config

# 共享数据库路径（SQLite 回退后端使用；Day5 迁移后主后端为 MySQL）
DATA_DIR = Path(__file__).resolve().parents[4] / "data"
DB_PATH = DATA_DIR / "biz_agent.db"

# SQLite 版单例缓存
_checkpointer = None
_ctx = None
_async_checkpointer = None
_async_ctx = None

# MySQL 版单例缓存（PooledAsyncMySaver + asyncmy 连接池）
_mysql_saver: Any = None
_mysql_pool: Any = None

# checkpointer 表 collation 修复目标（与 langgraph-checkpoint-mysql 包内
# json_table 临时列的 CHARACTER SET utf8mb4 默认 collation 保持一致）
_CP_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")
_CP_COLLATION = "utf8mb4_0900_ai_ci"


def get_checkpointer() -> SqliteSaver:
    """同步版 SqliteSaver（仅纯 sync 图/测试用，主路径已切 MySQL）。"""
    global _checkpointer, _ctx
    if _checkpointer is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _ctx = SqliteSaver.from_conn_string(str(DB_PATH))
        _checkpointer = _ctx.__enter__()
    return _checkpointer


async def get_async_checkpointer() -> Any:
    """异步 checkpointer（async 图用；进程内单例）。

    默认 MySQL（PooledAsyncMySaver）；`CHECKPOINTER_BACKEND=sqlite` 回退 AsyncSqliteSaver。
    注意：MySQL 连接绑定创建它的事件循环，须在调用方 loop 内首次调用
    （graph.get_biz_graph 懒编译，符合该约束）。
    """
    if config.CHECKPOINTER_BACKEND == "mysql":
        return await get_mysql_checkpointer()
    global _async_checkpointer, _async_ctx
    if _async_checkpointer is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _async_ctx = AsyncSqliteSaver.from_conn_string(str(DB_PATH))
        _async_checkpointer = await _async_ctx.__aenter__()
    return _async_checkpointer


class PooledAsyncMySaver(BaseCheckpointSaver):
    """连接池版 AsyncMySaver（★ W27 D1：40 并发 P95 根因修复）。

    单连接版 AsyncMySaver 把「单 conn + asyncio.Lock」当作全局限流器：40 并发下
    ops 请求全部在锁上排队（ops_query P95=3467.9ms，`loadtest_final.md` §四）。

    池化设计（面试可讲 Little's law）：
    - 每操作从 asyncmy.Pool 取一条连接，绑定临时 AsyncMySaver 执行
    - 临时 saver 每次新建 → 其内锁仅单操作独占、零争用，并发写分散到池内连接
    - 连接复用（pool_recycle），无每操作 TCP 握手开销
    - 池耗尽时 acquire() 自动排队（不报错）；默认 maxsize=10 是余量不是并发数
      （40 并发 × 单写 ~50ms / 1s ≈ 2 条忙连接，10 有余）
    - 继承 BaseCheckpointSaver 获得 with_allowlist（compile 的 msgpack 严格模式
      路径会调用，返回共享同一池的浅拷贝）
    """

    def __init__(self, pool: Any, serde: Any = None) -> None:
        super().__init__(serde=serde)
        self._pool = pool

    @asynccontextmanager
    async def _saver(self) -> AsyncIterator[Any]:
        async with self._pool.acquire() as conn:
            yield AsyncMySaver(conn=conn, serde=self.serde)

    async def setup(self) -> None:
        async with self._saver() as s:
            await s.setup()

    async def aget_tuple(self, config: Any) -> Any:
        async with self._saver() as s:
            return await s.aget_tuple(config)

    async def aput(
        self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any
    ) -> Any:
        async with self._saver() as s:
            return await s.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: Any, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        async with self._saver() as s:
            await s.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._saver() as s:
            await s.adelete_thread(thread_id)

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        async with self._saver() as s:
            async for tup in s.alist(config, filter=filter, before=before, limit=limit):
                yield tup


async def get_mysql_checkpointer() -> Any:
    """PooledAsyncMySaver（MySQL 权威库，连接池化）。

    首次调用：创建 asyncmy 连接池（minsize=SCM_CHECKPOINT_POOL_MIN、
    maxsize=SCM_CHECKPOINT_POOL_SIZE）→ setup() 建表 → 修正 collation。
    幂等：池已就绪则直接返回单例。
    """
    global _mysql_saver, _mysql_pool
    if _mysql_saver is not None:
        return _mysql_saver

    from asyncmy import create_pool
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from app.platform.settings import settings

    parsed = AsyncMySaver.parse_conn_string(settings.platform_dsn)
    _mysql_pool = await create_pool(
        minsize=config.SCM_CHECKPOINT_POOL_MIN,
        maxsize=config.SCM_CHECKPOINT_POOL_SIZE,
        pool_recycle=config.SCM_CHECKPOINT_POOL_RECYCLE,
        **parsed,
        autocommit=True,
    )
    _mysql_saver = PooledAsyncMySaver(pool=_mysql_pool, serde=JsonPlusSerializer())

    # ① 建表（langgraph 自身迁移；幂等）
    await _mysql_saver.setup()
    # ② 修正 collation（utf8mb4_0900_ai_ci），否则读回报 1267
    await _fix_checkpointer_collation(_mysql_pool)
    return _mysql_saver


async def _fix_checkpointer_collation(conn_or_pool: Any) -> None:
    """把 checkpointer 四表字符列统一为 utf8mb4_0900_ai_ci。

    只影响 langgraph 管理表；业务表（audit_logs 等）保持 utf8mb4_unicode_ci。
    幂等：重复执行 CONVERT 无副作用；表不存在时跳过（建表失败不应阻断启动）。
    ★ W27 D1：入参可为 asyncmy 连接或连接池（池则取一条连接执行）。
    """

    async def _fix(conn: Any) -> None:
        cur = conn.cursor()
        try:
            for tbl in _CP_TABLES:
                try:
                    await cur.execute(
                        f"ALTER TABLE {tbl} CONVERT TO CHARACTER SET utf8mb4 "
                        f"COLLATE {_CP_COLLATION}"
                    )
                except Exception:
                    continue  # 表可能尚未创建，静默跳过
        finally:
            await cur.close()

    if hasattr(conn_or_pool, "acquire"):
        async with conn_or_pool.acquire() as conn:
            await _fix(conn)
    else:
        await _fix(conn_or_pool)


async def close_mysql_checkpointer() -> None:
    """关闭 MySQL checkpointer 连接池（测试/优雅停机用）。"""
    global _mysql_saver, _mysql_pool
    if _mysql_pool is not None:
        with __import__("contextlib").suppress(Exception):
            _mysql_pool.close()
    _mysql_saver = None
    _mysql_pool = None


def reset_checkpointer():
    """测试用：清空单例缓存（让下个 get_* 重建）。"""
    global _checkpointer, _ctx, _async_checkpointer, _async_ctx, _mysql_saver, _mysql_pool
    if _checkpointer is not None:
        from contextlib import suppress

        with suppress(Exception):
            _ctx.__exit__(None, None, None)
    if _async_checkpointer is not None:
        from contextlib import suppress

        with suppress(Exception):
            _async_ctx.__aexit__(None, None, None)
    if _mysql_pool is not None:
        from contextlib import suppress

        with suppress(Exception):
            _mysql_pool.close()
    _checkpointer = None
    _ctx = None
    _async_checkpointer = None
    _async_ctx = None
    _mysql_saver = None
    _mysql_pool = None
