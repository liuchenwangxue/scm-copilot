"""W25 Day1 调度基座测试：六任务注册表 + job_runs 落库 + MySQL job store 持久性。

覆盖手册 Day1 验收：
- 六任务注册表完整（name / cron 表达式与《03》2 节一致）——纯逻辑，无 DB
- scheduler_job_runs 写入（running → success；互斥时记录 skipped）——integration，需 MySQL
- 重启持久性：start → shutdown → start，job 定义与 next_run_time 完整（job store 在 MySQL）
"""

import pytest
import pytest_asyncio

from app.platform.scheduler import JOB_DEFS, PlatformScheduler, _run_job, _runtime

pytestmark = pytest.mark.integration


# ==================== 注册表完整性（纯逻辑，无 DB） ====================


def test_six_jobs_registry_complete():
    """六任务注册表：name 唯一 + cron 与《03》2 节一致 + func 可调用。"""
    names = [s["name"] for s in JOB_DEFS]
    assert len(JOB_DEFS) == 6
    assert len(set(names)) == 6  # 无重复任务名

    expect_cron = {
        "kb_increment_sync": "*/5 * * * *",
        "vector_cleanup": "0 3 * * *",
        "audit_archive": "0 4 1 * *",
        "daily_brief": "0 8 * * 1-5",
        "eval_nightly": "0 2 * * *",
        "cache_warmup": "0 7 * * *",
    }
    for spec in JOB_DEFS:
        assert spec["name"] in expect_cron, f"未预期的任务 {spec['name']}"
        assert spec["cron"] == expect_cron[spec["name"]], (
            f"{spec['name']} cron 应为 {expect_cron[spec['name']]}，实际 {spec['cron']}"
        )
        assert callable(spec["func"])


def test_cron_expressions_parse():
    """cron 表达式可被 APScheduler 解析（防止拼写错误到 Day6 才暴露）。"""
    from apscheduler.triggers.cron import CronTrigger

    for spec in JOB_DEFS:
        trigger = CronTrigger.from_crontab(spec["cron"])
        assert trigger is not None


def test_run_job_is_module_level_callable():
    """★ 序列化坑：_run_job 必须模块级可导入（job store pickle 依赖 module:func 引用）。"""
    import app.platform.scheduler as mod

    assert callable(mod._run_job)
    # qualname 无 '<locals>' = 非闭包，可被 pickle
    assert "<locals>" not in mod._run_job.__qualname__


# ==================== job_runs 落库（integration，需 MySQL） ====================


@pytest_asyncio.fixture
async def runtime():
    """设置模块级运行时上下文（session_factory/instance_id），用完还原。

    ★ W27-D6 (B10)：_runtime 已是 RuntimeContext 对象——测试文件顶部的 `_runtime`
    与模块属性是同一对象引用，直接改字段（而非替换对象），两边都可见。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.platform.scheduler as mod
    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    old_sf, old_iid = mod._runtime.session_factory, mod._runtime.instance_id
    mod._runtime.session_factory = session_factory
    mod._runtime.instance_id = "test-instance"
    yield session_factory
    mod._runtime.session_factory, mod._runtime.instance_id = old_sf, old_iid
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_runs_written_on_execute(runtime, monkeypatch):
    """任务执行写 job_runs：running → success，字段完整（instance/trigger/时间）。

    ★ W25 Day2 修正：Day1 时 kb_increment_sync 是 stub，直接 `_run_job` 无副作用；
      现在它已是真实实现（会全量同步 57 篇文档 + 重建 Qdrant）。测试 job_runs 机制
      用轻量 stub 替换业务回调——机制验证与业务实现解耦。
    """
    from app.platform.scheduler import _JOB_BY_NAME

    async def _stub() -> dict:
        return {"job": "kb_increment_sync", "status": "stub"}

    monkeypatch.setitem(
        _JOB_BY_NAME,
        "kb_increment_sync",
        {**_JOB_BY_NAME["kb_increment_sync"], "func": _stub},
    )

    result = await _run_job("kb_increment_sync")
    assert result["job"] == "kb_increment_sync"
    # 业务回调返回自身结果（stub）；job_runs 表里的状态才是 success（下方断言）

    from sqlalchemy import select

    from app.platform.models import SchedulerJobRun

    async with _runtime.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(SchedulerJobRun)
                    .where(
                        SchedulerJobRun.job_id == "kb_increment_sync",
                        SchedulerJobRun.status == "success",
                    )
                    .order_by(SchedulerJobRun.id.desc())
                    .limit(1)
                )
            ).all()
        )
    assert rows, "job_runs 应有 success 记录"
    row = rows[0]
    assert row.trigger == "cron"
    assert row.instance == "test-instance"
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.duration_ms is not None and row.duration_ms >= 0


@pytest.mark.asyncio
async def test_job_runs_skipped_when_lock_held(runtime, monkeypatch):
    """互斥场景：任务未抢到锁 → job_runs 记 skipped（零重复观测依据）。

    依赖真实 Redis（预置锁需要 SETNX；leader 锁 Redis 不可用会 fail-open 放行）。
    CI 无 Redis service → skip（互斥语义已由 test_scheduler_leader.py FakeRedis 覆盖）。
    ★ W25 Day2：kb_increment_sync 已是真实实现，用 stub 替换避免触发全量同步副作用。
    """
    from sqlalchemy import select

    from app.platform.models import SchedulerJobRun
    from app.platform.scheduler import _JOB_BY_NAME
    from app.platform.scheduler.leader import SkipResult
    from app.shared.reliability.redis_client import get_redis_client

    spec = JOB_DEFS[0]

    async def _stub() -> dict:
        return {"job": spec["name"], "status": "stub"}

    monkeypatch.setitem(_JOB_BY_NAME, spec["name"], {**_JOB_BY_NAME[spec["name"]], "func": _stub})

    rc = get_redis_client()
    if not rc.available:
        pytest.skip("Redis 不可用，跳过真实互斥落库验证（FakeRedis 单测已覆盖）")
    key = f"lock:job:{spec['name']}"
    rc.set_nx(key, "other-instance-owner", ex=30)

    try:
        result = await _run_job(spec["name"])
        assert isinstance(result, SkipResult)

        async with _runtime.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SchedulerJobRun)
                        .where(
                            SchedulerJobRun.job_id == spec["name"],
                            SchedulerJobRun.status == "skipped",
                        )
                        .order_by(SchedulerJobRun.id.desc())
                        .limit(1)
                    )
                ).all()
            )
        assert rows, "互斥时应有 skipped 记录"
        assert "another instance" in (rows[0].error or "")
    finally:
        rc.delete(key)


# ==================== 重启持久性（integration，需 MySQL） ====================


@pytest.mark.asyncio
async def test_job_store_persistence_across_restart():
    """MySQL job store：start → shutdown → start，六任务定义完整且 next_run_time 存在。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    svc = PlatformScheduler(
        jobstore_dsn=settings.jobstore_dsn,
        session_factory=session_factory,
        instance_id="restart-test",
    )
    svc2 = None
    try:
        svc.start()
        assert svc.scheduler is not None
        jobs_first = {j.id: j.next_run_time for j in svc.scheduler.get_jobs()}
        assert len(jobs_first) == 6, f"首启应注册六任务，实际 {list(jobs_first)}"
        assert all(nt is not None for nt in jobs_first.values())

        svc.shutdown(wait=False)

        # 模拟重启：新建实例（同 MySQL job store）
        svc2 = PlatformScheduler(
            jobstore_dsn=settings.jobstore_dsn,
            session_factory=session_factory,
            instance_id="restart-test-2",
        )
        svc2.start()
        assert svc2.scheduler is not None
        jobs_second = {j.id: j.next_run_time for j in svc2.scheduler.get_jobs()}
        svc2.shutdown(wait=False)

        # 六任务定义完整恢复
        assert set(jobs_second) == set(jobs_first), "重启后任务定义应完整"
        assert all(nt is not None for nt in jobs_second.values()), "重启后 next_run_time 应存在"
    finally:
        if svc2 is not None and svc2.scheduler is not None and svc2.scheduler.running:
            svc2.shutdown(wait=False)
        if svc.scheduler is not None and svc.scheduler.running:
            svc.shutdown(wait=False)
        await engine.dispose()
