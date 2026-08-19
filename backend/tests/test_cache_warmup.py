"""W25 Day3 cache_warmup 测试：无数据降级（纯逻辑）+ 昨日高频问题查询（integration）。"""

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio

from app.platform.scheduler.jobs import cache_warmup

pytestmark = pytest.mark.integration


# ==================== 纯逻辑：无数据降级 ====================


@pytest.mark.asyncio
async def test_run_no_questions_returns_empty_stats():
    """无昨日会话（数据源不可用/无记录）→ 返回空统计，不抛错。

    ★ W27-D6 (B10)：_runtime 已是 RuntimeContext 对象，直接改字段（同一对象引用）。
    """
    import app.platform.scheduler as mod

    old_sf, old_iid = mod._runtime.session_factory, mod._runtime.instance_id
    mod._runtime.session_factory = None
    mod._runtime.instance_id = "test-warmup-none"
    try:
        result = await cache_warmup.run()
        assert result["candidates"] == 0
        assert result["warmed"] == 0
        assert result["failed"] == 0
        assert result["status"] == "success"
    finally:
        mod._runtime.session_factory, mod._runtime.instance_id = old_sf, old_iid


# ==================== integration：昨日高频问题 ====================


@pytest_asyncio.fixture
async def runtime():
    """设置模块级运行时上下文（session_factory），用完还原。

    ★ W27-D6 (B10)：_runtime 已是 RuntimeContext 对象——直接改字段（同一对象引用）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.platform.scheduler as mod
    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    old_sf, old_iid = mod._runtime.session_factory, mod._runtime.instance_id
    mod._runtime.session_factory = session_factory
    mod._runtime.instance_id = "test-warmup"
    yield session_factory
    mod._runtime.session_factory, mod._runtime.instance_id = old_sf, old_iid
    await engine.dispose()


@pytest.mark.asyncio
async def test_yesterday_hot_questions_finds_yesterday(runtime):
    """conversations 昨日标题按频次聚合可查到（预热数据源）。"""
    from sqlalchemy import delete, insert

    from app.platform.models import Conversation
    from app.platform.scheduler.jobs.cache_warmup import _yesterday_hot_questions

    yesterday = date.today() - timedelta(days=1)
    thread_ids = ["warmup-test-1", "warmup-test-2"]
    async with runtime() as s:
        for tid in thread_ids:
            await s.execute(
                insert(Conversation).values(
                    thread_id=tid,
                    title="缓存预热测试问题",
                    user_id=None,
                    tenant_id="t_test",
                    created_at=datetime.combine(yesterday, datetime.min.time()),
                )
            )
        await s.commit()
    try:
        qs = await _yesterday_hot_questions(yesterday, limit=10)
        assert "缓存预热测试问题" in qs, f"昨日标题应被聚合查询命中，实际 {qs}"
    finally:
        async with runtime() as s:
            await s.execute(
                delete(Conversation).where(Conversation.thread_id.in_(thread_ids))
            )
            await s.commit()
