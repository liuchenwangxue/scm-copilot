"""W25 Day3 daily_brief 测试：指标提取 + 模板渲染（纯逻辑）+ 全链路（integration）。"""

from datetime import date

import pytest
import pytest_asyncio

from app.platform.scheduler import _runtime
from app.platform.scheduler.jobs import daily_brief
from app.platform.scheduler.jobs.daily_brief import _extract_metric, _render_brief

# 纯逻辑用例：无需外部服务
pytestmark_pure = pytest.mark.filterwarnings("default")

# integration 用例（需 MySQL + Redis + scm_biz）
pytestmark = pytest.mark.integration


# ==================== 跨 loop 防护 ====================


@pytest_asyncio.fixture(autouse=True)
async def _dispose_executor_engine():
    """每个用例后释放只读沙箱 engine（防跨 loop 复用——CI pytest-asyncio 1.6.x 严格校验）。

    ★ 与 test_executor.py 的 `_dispose_engine` 同模式：本文件的 integration 用例
    （test_run_generates_brief_and_notifies）经 daily_brief → run_nl2sql_query → execute_sql
    创建 `_ExecutorEngine` 模块级单例，若不释放，后续 test_executor 的用例在新 loop
    复用旧 loop 的 engine → RuntimeError（CI 实测）。
    """
    yield
    from app.domains.data.executor import dispose_engine

    await dispose_engine()


# ==================== 纯逻辑：指标提取 ====================


def test_extract_metric_gmv_single_value():
    res = {"rows": [[12345.6]], "columns": ["gmv"], "rejected_reason": None}
    assert _extract_metric("gmv", res) == 12345.6


def test_extract_metric_delay_rate():
    res = {"rows": [[8.25]], "columns": ["delay_rate"], "rejected_reason": None}
    assert _extract_metric("delay_rate", res) == 8.25


def test_extract_metric_top_suppliers():
    res = {
        "rows": [["华东A", 100.0], ["华北B", 80.0]],
        "columns": ["supplier", "gmv"],
        "rejected_reason": None,
    }
    items = _extract_metric("top_suppliers", res)
    assert items == [{"supplier": "华东A", "gmv": 100.0}, {"supplier": "华北B", "gmv": 80.0}]


def test_extract_metric_rejected_returns_none():
    res = {"rows": [], "columns": [], "rejected_reason": "not-select"}
    assert _extract_metric("gmv", res) is None


def test_extract_metric_empty_rows_returns_none():
    res = {"rows": [], "columns": ["gmv"], "rejected_reason": None}
    assert _extract_metric("gmv", res) is None


# ==================== 纯逻辑：模板渲染（含 SQL 可回溯） ====================


def test_render_brief_contains_metrics_and_sql():
    metrics = {"gmv": 1000000.0, "delay_rate": 8.25, "top_suppliers": [{"supplier": "华东A", "gmv": 100.0}]}
    sqls = [
        {
            "key": "gmv",
            "question": "昨日订单总金额（GMV）是多少？",
            "sql": "SELECT SUM(amount) AS gmv FROM orders",
            "rejected_reason": None,
        }
    ]
    text = _render_brief("2026-09-02", metrics, sqls)
    assert "供应链经营日报 2026-09-02" in text
    assert "1,000,000.00" in text  # 金额千分位
    assert "8.25%" in text
    assert "华东A" in text
    assert "SELECT SUM(amount) AS gmv FROM orders" in text  # SQL 可回溯


def test_render_brief_no_data_placeholder():
    text = _render_brief("2026-09-02", {"gmv": None, "delay_rate": None, "top_suppliers": []}, [])
    assert "无数据" in text
    assert "TOP5 供应商" in text


# ==================== integration：全链路 ====================


@pytest_asyncio.fixture
async def runtime():
    """设置模块级运行时上下文（session_factory/instance_id），用完还原。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    old = dict(_runtime)
    _runtime["session_factory"] = session_factory
    _runtime["instance_id"] = "test-daily-brief"
    yield session_factory
    _runtime.clear()
    _runtime.update(old)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_generates_brief_and_notifies(runtime, monkeypatch):
    """全链路：三条 NL2SQL → brief 落库 → 订阅通知；第二次执行幂等跳过。"""
    from sqlalchemy import delete, select

    from app.platform.models import DailyBrief, Notification
    from app.shared.reliability.redis_client import get_redis_client

    rc = get_redis_client()
    if not rc.available:
        pytest.skip("Redis 不可用，跳过幂等链路验证")

    fixed = date(2026, 9, 2)

    class _FakeDate(date):
        """替换 daily_brief 模块的 date 绑定：today() 返回固定日期（C 扩展不可 setattr）。"""

        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr(daily_brief, "date", _FakeDate)
    brief_date = fixed.isoformat()
    title = f"供应链经营日报 {brief_date}"

    # 清理现场（幂等键 / 表记录）
    rc.delete(f"brief:{brief_date}")
    async with runtime() as s:
        await s.execute(delete(DailyBrief).where(DailyBrief.brief_date == brief_date))
        await s.execute(delete(Notification).where(Notification.title == title))
        await s.commit()

    try:
        result = await daily_brief.run()
        assert result["status"] == "success", result
        assert result["brief_date"] == brief_date
        assert len(result["sqls"]) == 3  # 三条模板问题
        assert all(s["sql"] for s in result["sqls"]), "每条 SQL 应可回溯（非空）"
        assert all(s["rejected_reason"] is None for s in result["sqls"]), "模板 SQL 不应被四道闸拒绝"

        async with runtime() as s:
            brief = await s.scalar(
                select(DailyBrief).where(DailyBrief.brief_date == brief_date)
            )
            assert brief is not None, "daily_briefs 应落库"
            assert brief.status == "pushed"
            assert brief.metrics is not None and "gmv" in brief.metrics
            assert brief.sqls is not None and len(brief.sqls) == 3

            notifs = list(
                (
                    await s.scalars(
                        select(Notification).where(Notification.title == title)
                    )
                ).all()
            )
            assert len(notifs) >= 1, "订阅用户应有站内通知"
            assert brief.notified_users is not None and len(brief.notified_users) >= 1

        # 幂等：第二次直接跳（Redis SETNX 已占用 + DB unique 双保险）
        result2 = await daily_brief.run()
        assert result2["status"] == "skipped"
    finally:
        rc.delete(f"brief:{brief_date}")
        async with runtime() as s:
            await s.execute(delete(DailyBrief).where(DailyBrief.brief_date == brief_date))
            await s.execute(delete(Notification).where(Notification.title == title))
            await s.commit()
