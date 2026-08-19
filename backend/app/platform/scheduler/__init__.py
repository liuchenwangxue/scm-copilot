"""★ 调度基座（W25 Day1）：APScheduler 封装 + MySQL job store + 六任务注册表。

目标：任务"定义不丢、互斥执行、重启自动恢复"。

设计（对照手册 Day1）：
- `AsyncIOScheduler` + `SQLAlchemyJobStore`（MySQL）：job 定义持久化在 `scm_platform.apscheduler_jobs`，
  **重启后任务定义与 next_run_time 完整恢复**（APScheduler 自带 store 锁，不加全局锁防死锁）。
- 六任务集中注册表（name → cron → 回调），`misfire_grace_time=300` + `coalesce=True`：
  错过触发点（如休眠/重启）在宽限期内合并补跑一次，不堆积。
- 每个任务回调统一包装：**leader 锁互斥**（双实例同一触发点只一个实例执行）→
  **job_runs 记录**（running → success/failed；未抢到锁记 skipped）——双实例零重复观测依据。
- FastAPI lifespan 集成：startup `start()`；shutdown `shutdown(wait=False)` 优雅停
  （不等运行中任务，立即退出——任务若被打断，下一轮 cron 补跑，幂等兜底）。

★ 序列化坑（手册未提，实测踩坑）：MySQL job store 用 pickle 持久化 job，回调必须是
**模块级可导入函数**（`module:func` 引用）。闭包/局部函数无法序列化（ValueError:
reference could not be determined）。因此任务入口是模块级 `_run_job(job_name)`，
运行时上下文（session_factory/instance_id）经 `_runtime` 模块级字典注入，
由 `PlatformScheduler.start()` 写入——job store 只存 `app.platform.scheduler:_run_job`。

其他坑（手册）：
- APScheduler 4.x 仍是 alpha，锁死 3.x 稳定线（pyproject 已 pin <4）
- job store 用**同步** SQLAlchemy engine（pymysql 驱动），与平台 asyncmy 连接池独立；
  DSN 由 settings 从 platform_dsn 派生（asyncmy → pymysql）
- 双实例同时 `scheduler.start()` 的 job store 初始化由 APScheduler 自带锁保护，不加全局锁
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.models import SchedulerJobRun
from app.platform.scheduler.jobs import (
    audit_archive,
    cache_warmup,
    daily_brief,
    eval_nightly,
    kb_increment_sync,
    vector_cleanup,
)
from app.platform.scheduler.leader import SkipResult, leader_lock

logger = logging.getLogger("scm.platform.scheduler")

# 六任务注册表（唯一权威定义；Day2/3 逐个替换 func 为真实实现）
JOB_DEFS: list[dict[str, Any]] = [
    {
        "name": "kb_increment_sync",
        "cron": "*/5 * * * *",
        "func": kb_increment_sync.run,
        "desc": "KB 增量同步（改文档 ≤5min 可检索）",
    },
    {
        "name": "vector_cleanup",
        "cron": "0 3 * * *",
        "func": vector_cleanup.run,
        "desc": "向量卫生清理（孤儿向量/缓存失效键）",
    },
    {
        "name": "audit_archive",
        "cron": "0 4 1 * *",
        "func": audit_archive.run,
        "desc": "审计日志按月归档（主表瘦身）",
    },
    {
        "name": "daily_brief",
        "cron": "0 8 * * 1-5",
        "func": daily_brief.run,
        "desc": "工作日经营日报（GMV/延迟率/TOP5）",
    },
    {
        "name": "eval_nightly",
        "cron": "0 2 * * *",
        "func": eval_nightly.run,
        "desc": "夜间质量回归（RAG 156 + NL2SQL 100）",
    },
    {
        "name": "cache_warmup",
        "cron": "0 7 * * *",
        "func": cache_warmup.run,
        "desc": "语义缓存预热（昨日热度 TOP100）",
    },
]

_JOB_BY_NAME: dict[str, dict[str, Any]] = {s["name"]: s for s in JOB_DEFS}

# 锁 TTL：任务超时后锁自动过期让出（防死锁），下轮其他实例接管
DEFAULT_LOCK_TTL = 300
# 错过宽限期 + 合并：休眠/重启/负载导致的错过触发点在 5min 内合并补跑一次
MISFIRE_GRACE_TIME = 300

@dataclass
class RuntimeContext:
    """调度运行时上下文（★ W27-D6 B10：显式传参替代模块级 dict 紧耦合）。

    APScheduler 序列化坑要求回调是模块级函数（job store 只持久化
    `app.platform.scheduler:_run_job` 引用）；运行时依赖（session_factory /
    instance_id）经本上下文注入一次，任务函数显式读字段而非隐式读 dict——
    类型可查、职责清晰，测试也可直接替换整个上下文对象。
    """

    session_factory: async_sessionmaker[AsyncSession] | None = None
    instance_id: str = "local"


# 模块级运行时上下文（★ 序列化坑的解法）：job store 只持久化 `_run_job` 的函数引用，
# 它执行时从这里读 session_factory / instance_id——由 PlatformScheduler.start() 注入。
_runtime = RuntimeContext()


# ==================== 模块级任务入口（APScheduler 可序列化引用） ====================


async def _run_job(job_name: str) -> Any:
    """模块级统一任务入口：按 job_name 查注册表 → 抢 leader 锁 → 执行业务 → 落 job_runs。

    这是 APScheduler 持久化的唯一回调引用（`app.platform.scheduler:_run_job`），
    重启后从 MySQL job store 恢复时按模块导入即可重新调用。
    """
    spec = _JOB_BY_NAME.get(job_name)
    if spec is None:
        logger.error("scheduler: unknown job %s", job_name)
        return {"job": job_name, "status": "failed", "error": "unknown job"}

    @leader_lock(job_name, ttl=DEFAULT_LOCK_TTL)
    async def _guarded() -> Any:
        run_id = _make_run_id(job_name)
        started = datetime.now()
        await _record(
            job_id=job_name, run_id=run_id, trigger="cron", status="running", started_at=started
        )
        try:
            result = await spec["func"]()
        except Exception as e:  # noqa: BLE001  # 任务失败不中断调度器，记 failed 下轮重试
            await _record(
                job_id=job_name,
                run_id=run_id,
                trigger="cron",
                status="failed",
                started_at=started,
                finished_at=datetime.now(),
                error=str(e),
            )
            return {"job": job_name, "status": "failed", "error": str(e)}
        await _record(
            job_id=job_name,
            run_id=run_id,
            trigger="cron",
            status="success",
            started_at=started,
            finished_at=datetime.now(),
        )
        return result

    result = await _guarded()
    if isinstance(result, SkipResult):
        # 未抢到锁：job_runs 记 skipped（双实例零重复的可观测证据）
        await _record(
            job_id=job_name,
            run_id=_make_run_id(job_name),
            trigger="cron",
            status="skipped",
            started_at=datetime.now(),
            error=result.reason,
        )
    return result


def _make_run_id(job_id: str) -> str:
    """run_id 幂等键：job + 秒级时间 + 实例后缀。

    ★ 双实例并发时若 run_id 相同，success 与 skipped 会互相覆盖同一行（状态混杂）——
    秒级时间戳在同一触发点两个实例上一致，故追加 instance 后缀，每实例各留一行：
    - 执行实例：running → success（1 行）
    - 跳过实例：skipped（1 行）
    零重复观测 = 按 (job, 秒窗口) 聚合，status != skipped 恰 1 行。
    """
    instance = _runtime.instance_id
    return f"{job_id}:{datetime.now().strftime('%Y%m%d%H%M%S')}:{instance}"


async def _record(
    *,
    job_id: str,
    run_id: str,
    trigger: str,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """写 scheduler_job_runs：同 (job_id, run_id) 首插后 update（running → 终态）。

    ★ W26 Day1：终态 success/failed 同步记录 Prometheus Counter
    （scm_job_success_total / scm_job_failed_total，label=job）——Grafana
    "队列与调度" 面板 24h 成功率/失败曲线数据源。
    """
    # 业务指标埋点（观测旁路，fail-open）：终态才计数，running/skipped 不计
    if status in ("success", "failed"):
        try:
            from app.shared.obs.metrics import inc_job_failed, inc_job_success
            if status == "success":
                inc_job_success(job_id)
            else:
                inc_job_failed(job_id)
        except Exception:  # noqa: BLE001  # 指标旁路失败不影响任务记录
            pass
    session_factory = _runtime.session_factory
    if session_factory is None:
        logger.warning("scheduler runtime not initialized, skip job_runs record for %s", job_id)
        return
    try:
        async with session_factory() as session:
            existing = await session.scalar(
                select(SchedulerJobRun).where(
                    SchedulerJobRun.job_id == job_id,
                    SchedulerJobRun.run_id == run_id,
                )
            )
            if existing is None:
                session.add(
                    SchedulerJobRun(
                        job_id=job_id,
                        run_id=run_id,
                        trigger=trigger,
                        status=status,
                        started_at=started_at,
                        finished_at=finished_at,
                        instance=_runtime.instance_id,
                        error=error,
                    )
                )
            else:
                existing.status = status
                existing.finished_at = finished_at
                existing.duration_ms = (
                    int((finished_at - existing.started_at).total_seconds() * 1000)
                    if finished_at and existing.started_at
                    else None
                )
                existing.error = error
            await session.commit()
    except Exception:  # noqa: BLE001  # job_runs 是观测旁路，写失败不拖垮任务
        logger.exception("job_runs record failed job=%s run=%s status=%s", job_id, run_id, status)


# ==================== PlatformScheduler 封装 ====================


class PlatformScheduler:
    """APScheduler 封装：job store + 六任务注册 + lifespan 启停。"""

    def __init__(
        self,
        jobstore_dsn: str,
        session_factory: async_sessionmaker[AsyncSession],
        instance_id: str,
        timezone: str = "Asia/Shanghai",
    ):
        self._jobstore_dsn = jobstore_dsn
        self._session_factory = session_factory
        self._instance_id = instance_id or "local"
        self._timezone = timezone
        self._scheduler: AsyncIOScheduler | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """创建调度器并启动：job store 建在 MySQL（重启不丢任务定义）。"""
        global _runtime
        # 注入模块级运行时上下文（序列化回调的依赖从这里读；★ W27-D6 B10：显式字段赋值）
        _runtime.session_factory = self._session_factory
        _runtime.instance_id = self._instance_id

        store = SQLAlchemyJobStore(url=self._jobstore_dsn)
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": store},
            timezone=self._timezone,
            job_defaults={
                "coalesce": True,
                "misfire_grace_time": MISFIRE_GRACE_TIME,
                "max_instances": 1,
            },
        )
        for spec in JOB_DEFS:
            self._scheduler.add_job(
                _run_job,  # ★ 模块级函数引用（可被 pickle），job_name 作参数
                CronTrigger.from_crontab(spec["cron"], timezone=self._timezone),
                args=[spec["name"]],
                id=spec["name"],
                replace_existing=True,
                name=spec["desc"],
            )
        self._scheduler.start()
        logger.info(
            "scheduler started: instance=%s jobs=%s store=%s",
            self._instance_id,
            [s["name"] for s in JOB_DEFS],
            self._jobstore_dsn.split("@")[-1],
        )

    def shutdown(self, wait: bool = False) -> None:
        """优雅停止。wait=False：不等运行中任务，立即退出（任务幂等兜底下轮补跑）。"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    @property
    def scheduler(self) -> AsyncIOScheduler | None:
        return self._scheduler

    def trigger(self, job_name: str) -> Any:
        """手动触发任务（admin 调度面板用，Day2 接 API；写审计在 API 层）。

        实现：注册一个**独立的**一次性 DateTrigger job（不覆盖原 cron job——
        ★ 坑：reschedule_job 会把原 cron job 换成一次性触发器，触发后即被移除，
        后续 cron 调度丢失）。独立 job 触发一次后自删，原 cron job 不受影响。
        """
        if self._scheduler is None:
            raise RuntimeError("scheduler not started")
        if job_name not in _JOB_BY_NAME:
            raise KeyError(f"unknown job {job_name}")
        from apscheduler.triggers.date import DateTrigger

        trigger_id = f"{job_name}:manual:{datetime.now().strftime('%H%M%S%f')}"
        self._scheduler.add_job(
            _run_job,
            DateTrigger(run_date=datetime.now(self._scheduler.timezone)),
            args=[job_name],
            id=trigger_id,
            replace_existing=False,
            name=f"{job_name} manual",
        )
