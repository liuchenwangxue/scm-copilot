"""W25 Day2 audit_archive 测试：批次名/边界（纯逻辑）+ 归档全流程（integration）。

覆盖手册 Day2 下午：
- 上月 audit_logs → 归档表（CTAS）+ 校验行数 + 删主表
- 幂等：归档表已存在 → 跳过；批次锁（Redis）只防并发、完成释放
- 跨年边界：1 月归档上月 → 前一年 12 月
"""

from datetime import datetime

import pytest

from app.platform.scheduler.jobs.audit_archive import (
    _make_owner,
    batch_name_of,
    month_range,
)

pytestmark = pytest.mark.integration


# ==================== 批次名 / 边界（纯逻辑） ====================


def test_batch_name_normal():
    assert batch_name_of(datetime(2026, 9, 1, 4, 0)) == "202608"


def test_batch_name_cross_year():
    assert batch_name_of(datetime(2026, 1, 1, 4, 0)) == "202512"


def test_month_range_normal():
    assert month_range(datetime(2026, 9, 15)) == ("2026-08-01", "2026-09-01")


def test_month_range_cross_year():
    assert month_range(datetime(2026, 1, 15)) == ("2025-12-01", "2026-01-01")


def test_make_owner_unique():
    assert _make_owner() != _make_owner()


# ==================== 归档全流程（integration：MySQL + FakeRedis） ====================


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.available = True

    def set_nx(self, key: str, value: str, ex=None) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    def delete_if_equals(self, key: str, expected: str) -> bool:
        if self.store.get(key) == expected:
            del self.store[key]
            return True
        return False


@pytest.fixture
async def factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_flow_idempotent(factory):
    """归档：插入 8 月审计 → 归档 202608 → 行数一致 + 主表删空 → 重跑幂等跳过。"""
    from sqlalchemy import text

    from app.platform.scheduler.jobs.audit_archive import _archive_batch

    now = datetime(2026, 9, 15, 4, 0)
    table = "audit_logs_202608"
    rc = FakeRedis()

    async def _cleanup():
        async with factory() as s:
            await s.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await s.execute(text("DELETE FROM audit_logs WHERE created_at >= '2026-08-01'"))
            await s.commit()

    await _cleanup()
    # 预置 8 月测试审计 3 条 + 9 月 1 条（9 月不应被归档）
    async with factory() as s:
        for i in range(3):
            await s.execute(
                text(
                    "INSERT INTO audit_logs (event, actor, created_at) "
                    f"VALUES ('test.evt', 'tester', '2026-08-{i + 1:02d} 10:00:00.000')"
                )
            )
        await s.execute(
            text(
                "INSERT INTO audit_logs (event, actor, created_at) "
                "VALUES ('test.sep', 'tester', '2026-09-01 00:00:01.000')"
            )
        )
        await s.commit()

    try:
        # ---- 执行归档 ----
        r1 = await _archive_batch(factory, rc, now)
        assert r1["status"] == "success"
        assert r1["batch"] == "202608" and r1["archived_rows"] == 3
        # 归档表有 3 行、主表 8 月数据已删（9 月 1 条保留）
        async with factory() as s:
            archived = (await s.scalar(text(f"SELECT COUNT(*) FROM {table}"))) or 0
            remain = (
                await s.scalar(
                    text("SELECT COUNT(*) FROM audit_logs WHERE created_at < '2026-09-01'")
                )
            ) or 0
            sep = (
                await s.scalar(text("SELECT COUNT(*) FROM audit_logs WHERE event = 'test.sep'"))
            ) or 0
        assert archived == 3
        assert remain == 0
        assert sep == 1

        # ---- 幂等：归档表已存在 → 跳过（即使批次锁被清） ----
        r2 = await _archive_batch(factory, FakeRedis(), now)
        assert r2["status"] == "skipped"
        assert "already exists" in r2["reason"]

        # ---- 批次锁：完成后已释放 ----
        assert rc.store == {}
    finally:
        await _cleanup()
