"""★ A8 生产级持久化：LangGraph checkpointer（W23 Day5 切 MySQL）。

演进路线：
- W19 Day4：SqliteSaver（进程重启不丢）
- W21 Day5：AsyncSqliteSaver（async 图 + aiosqlite）
- ★ W23 Day5：AsyncMySaver（MySQL 权威库）——与审批/审计同源，双实例共享断点，
  历史 SQLite 断点已由 `scripts/migrate_sqlite_to_mysql.py` 无损迁移
  （358 断点 / 858 写入，含校验和比对）。

后端选择：`CHECKPOINTER_BACKEND`（ops/config.py）
- `mysql`（默认）：AsyncMySaver，连接平台库 scm_platform
- `sqlite`（回退）：AsyncSqliteSaver，本地 biz_agent.db（测试/无 MySQL 环境）

三个关键坑（Day5 实测，写入代码注释供面试讲）：
1. collation：langgraph-checkpoint-mysql 的 SELECT_SQL 用 json_table 生成临时列，
   硬编码 `CHARACTER SET utf8mb4`（MySQL 8 默认 utf8mb4_0900_ai_ci）；若表继承
   数据库默认 utf8mb4_unicode_ci，读回即报 1267 Illegal mix of collations。
   解决：setup() 建表后把 checkpointer 四表 CONVERT TO utf8mb4_0900_ai_ci。
2. 连接绑定事件循环：asyncmy 连接与创建它的 loop 绑定，必须在使用方 loop 内
   首次创建（graph.get_biz_graph 内懒编译，本模块只做单例缓存）。
3. setup() 在 lifespan/首次使用时调用（不在 import 时连库）。
"""
from pathlib import Path
from typing import Any

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

# MySQL 版单例缓存（AsyncMySaver + asyncmy 连接）
_mysql_saver: Any = None
_mysql_conn: Any = None

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

    默认 MySQL（AsyncMySaver）；`CHECKPOINTER_BACKEND=sqlite` 回退 AsyncSqliteSaver。
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


async def get_mysql_checkpointer() -> Any:
    """AsyncMySaver（MySQL 权威库）。

    首次调用：建立 asyncmy 连接 → setup() 建表 → 修正 collation。
    幂等：连接/表已就绪则直接返回单例。
    """
    global _mysql_saver, _mysql_conn
    if _mysql_saver is not None:
        return _mysql_saver

    from asyncmy import connect
    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from app.platform.settings import settings

    parsed = AsyncMySaver.parse_conn_string(settings.platform_dsn)
    _mysql_conn = await connect(**parsed, autocommit=True)
    _mysql_saver = AsyncMySaver(conn=_mysql_conn, serde=JsonPlusSerializer())

    # ① 建表（langgraph 自身迁移；幂等）
    await _mysql_saver.setup()
    # ② 修正 collation（utf8mb4_0900_ai_ci），否则读回报 1267
    await _fix_checkpointer_collation(_mysql_conn)
    return _mysql_saver


async def _fix_checkpointer_collation(conn: Any) -> None:
    """把 checkpointer 四表字符列统一为 utf8mb4_0900_ai_ci。

    只影响 langgraph 管理表；业务表（audit_logs 等）保持 utf8mb4_unicode_ci。
    幂等：重复执行 CONVERT 无副作用；表不存在时跳过（建表失败不应阻断启动）。
    """
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


async def close_mysql_checkpointer() -> None:
    """关闭 MySQL checkpointer 连接（测试/优雅停机用）。"""
    global _mysql_saver, _mysql_conn
    if _mysql_conn is not None:
        with __import__("contextlib").suppress(Exception):
            _mysql_conn.close()
    _mysql_saver = None
    _mysql_conn = None


def reset_checkpointer():
    """测试用：清空单例缓存（让下个 get_* 重建）。"""
    global _checkpointer, _ctx, _async_checkpointer, _async_ctx, _mysql_saver, _mysql_conn
    if _checkpointer is not None:
        from contextlib import suppress

        with suppress(Exception):
            _ctx.__exit__(None, None, None)
    if _async_checkpointer is not None:
        from contextlib import suppress

        with suppress(Exception):
            _async_ctx.__aexit__(None, None, None)
    if _mysql_conn is not None:
        from contextlib import suppress

        with suppress(Exception):
            _mysql_conn.close()
    _checkpointer = None
    _ctx = None
    _async_checkpointer = None
    _async_ctx = None
    _mysql_saver = None
    _mysql_conn = None
