"""audit_archive：审计日志按月归档（每月 1 日 04:00，W25 Day2）。

cron: 0 4 1 * *
作用（对照手册 Day2 下午）：
- 上月 `audit_logs` → 归档表 `audit_logs_YYYYmm`：
  `CREATE TABLE ... AS SELECT` + 校验行数 + 删主表（主表瘦身，归档表只读保留可回溯）
- 幂等：批次名 = YYYYmm，记 Redis `archive:batch:{batch}`（SETNX）——已存在 → 跳过
  （幂等键只有任务成功才设置，失败不推进；下月重跑可重试）
- 先校验后删主表：行数对不上直接失败（不删主表，保留现场）；两段间由批次锁防并发
  （月跑一次，但"先校验后删 + 锁批次号"的习惯要养——手册坑）

边界口径：`created_at >= 上月1号 AND < 本月1号`（跨月/跨年由 Python date 处理，
归档表名固定 YYYYmm，无歧义）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.shared.reliability.redis_client import RedisClient, get_redis_client

CRON = "0 4 1 * *"

# 批次锁前缀（Redis；SETNX 语义，成功才设置 → 幂等）
_BATCH_PREFIX = "archive:batch"


def batch_name_of(dt: datetime) -> str:
    """归档批次名：YYYYmm（如 202608）。跨年时 month-1 自动退位（date 处理）。"""
    if dt.month == 1:
        return f"{dt.year - 1}12"
    return f"{dt.year}{dt.month - 1:02d}"


def month_range(dt: datetime) -> tuple[str, str]:
    """上月边界 (start, end) ISO 字符串：上月 1 号 00:00 → 本月 1 号 00:00。"""
    if dt.month == 1:
        start = date(dt.year - 1, 12, 1)
        end = date(dt.year, 1, 1)
    else:
        start = date(dt.year, dt.month - 1, 1)
        end = date(dt.year, dt.month, 1)
    return start.isoformat(), end.isoformat()


def _archive_table(batch: str) -> str:
    return f"audit_logs_{batch}"


async def _archive_batch(
    session_factory: async_sessionmaker[AsyncSession],
    rc: RedisClient,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    batch = batch_name_of(now)
    start_iso, end_iso = month_range(now)
    table = _archive_table(batch)
    lock_key = f"{_BATCH_PREFIX}:{batch}"

    async with session_factory() as session:
        # ★ 幂等判定（数据库真相）：归档表已存在 → 跳过（成功过一次即不再跑）
        exists = (
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = :t"
                ),
                {"t": table},
            )
        ) or 0
        if exists:
            return {
                "job": "audit_archive",
                "status": "skipped",
                "batch": batch,
                "reason": f"archive table {table} already exists",
            }

    # ★ 批次锁（两段间防并发，手册坑）：先校验后删主表的两步不能被打断；
    #   任务完成/finally 都释放（失败不残留锁，下轮可重试——不是幂等标记）
    owner = _make_owner()
    if rc.available and not rc.set_nx(lock_key, owner, ex=600):
        return {
            "job": "audit_archive",
            "status": "skipped",
            "batch": batch,
            "reason": "another instance holds archive lock",
        }
    try:
        async with session_factory() as session:
            # 1) 迁移数据（CTAS 原子建表 + 复制）
            await session.execute(
                text(
                    f"CREATE TABLE {table} AS SELECT * FROM audit_logs "
                    f"WHERE created_at >= '{start_iso}' AND created_at < '{end_iso}'"
                )
            )
            await session.commit()

            # 2) 校验行数（先校验后删主表——手册坑）
            archived = (await session.scalar(text(f"SELECT COUNT(*) FROM {table}"))) or 0
            src = (
                await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM audit_logs WHERE created_at >= :s AND created_at < :e"
                    ),
                    {"s": start_iso, "e": end_iso},
                )
            ) or 0
            if archived != src:
                # 校验失败：保留主表现场（不删）；归档表已建会在下轮判定 skipped——补删保持可重试
                await session.execute(text(f"DROP TABLE {table}"))
                await session.commit()
                raise RuntimeError(
                    f"audit archive row count mismatch: {table}={archived} vs audit_logs={src}"
                )

            # 3) 删主表（瘦身；归档表已含全部数据）
            await session.execute(
                text("DELETE FROM audit_logs WHERE created_at >= :s AND created_at < :e"),
                {"s": start_iso, "e": end_iso},
            )
            await session.commit()
    finally:
        if rc.available:
            rc.delete_if_equals(lock_key, owner)

    return {
        "job": "audit_archive",
        "status": "success",
        "batch": batch,
        "table": table,
        "archived_rows": archived,
        "range": [start_iso, end_iso],
    }


def _make_owner() -> str:
    """批次锁 owner（uuid4 hex；释放时 owner 校验防误删他人锁）。"""
    import uuid

    return uuid.uuid4().hex


async def run(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    rc: RedisClient | None = None,
    now: datetime | None = None,
) -> dict:
    """调度器入口（无参契约）。单测可注入依赖/固定时间。"""
    from app.platform.scheduler import _runtime

    sf = session_factory or _runtime.session_factory  # ★ W27-D6 B10：RuntimeContext 字段
    if sf is None:
        return {
            "job": "audit_archive",
            "status": "degraded",
            "error": "scheduler runtime not initialized",
        }
    rc = rc or get_redis_client()
    try:
        return await _archive_batch(sf, rc, now)
    except Exception as e:  # noqa: BLE001
        return {
            "job": "audit_archive",
            "status": "failed",
            "batch": batch_name_of(now or datetime.now()),
            "error": str(e),
        }


# 类型引用别名
AsyncSessionFactory = async_sessionmaker[AsyncSession]
