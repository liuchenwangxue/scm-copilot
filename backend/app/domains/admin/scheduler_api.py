"""调度面板 API（W25 Day2）：任务状态查询 + 手动触发。

权限（手册六问 #3）：调度面板 `admin:scheduler:manage`；手动触发写审计。

设计要点：
- `GET /api/admin/scheduler/jobs`：六任务 + 上次运行状态（SchedulerJobRun 最近一条）+
  next_run_time（APScheduler）；调度器未启用 → 503（部署环境默认开启，CI 关闭属预期）
- `POST /api/admin/scheduler/jobs/{name}/trigger`：调用 `PlatformScheduler.trigger(name)`
  （独立一次性 job，不覆盖原 cron，Day1 已实现），写审计
- 审计：`write_audit(session, event="admin.scheduler.trigger", ...)`，actor 来自 JWT
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.domains.admin.schemas import (
    JobRunOut,
    SchedulerJobOut,
    SchedulerJobsOut,
    SchedulerTriggerOut,
)
from app.platform import rbac
from app.platform.audit import write_audit
from app.platform.models import SchedulerJobRun, User
from app.platform.scheduler import JOB_DEFS

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _get_scheduler(request: Request):
    """取当前 app 的调度器实例（未启动 → 503）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None or not scheduler.running:
        raise HTTPException(status_code=503, detail="scheduler not running")
    return scheduler


@router.get(
    "/scheduler/jobs",
    response_model=SchedulerJobsOut,
    summary="调度任务面板",
    description="六任务定义 + 调度状态 + 最近运行历史（job_runs；零重复观测依据）。需要 admin:scheduler:manage。",
)
async def list_scheduler_jobs(
    request: Request,
    _: Annotated[User, Depends(rbac.require_permission("admin:scheduler:manage"))],
) -> SchedulerJobsOut:
    """任务面板：六任务定义 + 调度状态 + 上次运行（job_runs 最近一条）。"""
    scheduler = _get_scheduler(request)
    aps = scheduler.scheduler
    running_jobs = {j.id: j for j in aps.get_jobs()} if aps is not None else {}

    # 每个任务最近运行历史（★ W25 Day3：面板展示最近 N 条，零重复观测依据）
    # rows 按 id 降序 → 先到的即最近；每个 job_id 收集最近 RECENT_RUNS_LIMIT 条
    RECENT_RUNS_LIMIT = 5
    recent_runs: dict[str, list[JobRunOut]] = {}
    factory = request.app.state.session_factory
    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(SchedulerJobRun)
                    .order_by(SchedulerJobRun.id.desc())
                    # ★ 修复：job_runs 表随调度持续追加，全表加载内存/查询成本无界
                    #   增长——SQL 层加全局上限（6 任务 × 每 job 取 5 条，500 条绰绰有余）
                    .limit(500)
                )
            ).all()
        )
    for row in rows:
        bucket = recent_runs.setdefault(row.job_id, [])
        if len(bucket) >= RECENT_RUNS_LIMIT:
            continue
        bucket.append(
            JobRunOut(
                status=row.status,
                started_at=row.started_at.isoformat() if row.started_at else None,
                finished_at=row.finished_at.isoformat() if row.finished_at else None,
                duration_ms=row.duration_ms,
                instance=row.instance,
                trigger=row.trigger,
                error=row.error,
            )
        )

    jobs = []
    for spec in JOB_DEFS:
        aps_job = running_jobs.get(spec["name"])
        runs = recent_runs.get(spec["name"], [])
        jobs.append(
            SchedulerJobOut(
                name=spec["name"],
                cron=spec["cron"],
                desc=spec["desc"],
                enabled=aps_job is not None,
                next_run_time=aps_job.next_run_time.isoformat()
                if aps_job is not None and aps_job.next_run_time
                else None,
                last_run=runs[0] if runs else None,
                recent_runs=runs,
            )
        )
    return SchedulerJobsOut(
        scheduler={
            "running": scheduler.running,
            "instance": getattr(scheduler, "_instance_id", "local"),
            "timezone": getattr(scheduler, "_timezone", "Asia/Shanghai"),
        },
        jobs=jobs,
    )


@router.post(
    "/scheduler/jobs/{name}/trigger",
    response_model=SchedulerTriggerOut,
    summary="手动触发调度任务",
    description="手动触发任务（写审计；触发的是独立一次性 job，原 cron 不受影响）。需要 admin:scheduler:manage。",
)
async def trigger_scheduler_job(
    request: Request,
    name: str,
    current: Annotated[User, Depends(rbac.require_permission("admin:scheduler:manage"))],
) -> SchedulerTriggerOut:
    """手动触发任务（写审计；触发的是独立一次性 job，原 cron 不受影响）。"""
    scheduler = _get_scheduler(request)
    try:
        scheduler.trigger(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # 写审计（触发动作留痕）
    factory = request.app.state.session_factory
    async with factory() as session:
        await write_audit(
            session,
            event="admin.scheduler.trigger",
            actor=current.username,
            target=f"/api/v1/admin/scheduler/jobs/{name}/trigger",
            detail={"job": name},
            # ★ 修复：request_id 在 scope（RequestIdMiddleware 写 scope["request_id"]），
            #   request.state 上从未写入——原实现 trace_id 恒为 None，排障链路断裂
            trace_id=request.scope.get("request_id"),
        )
        await session.commit()

    return SchedulerTriggerOut(ok=True, job=name, triggered=True, audited=True)
