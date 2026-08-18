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

from app.platform import rbac
from app.platform.audit import write_audit
from app.platform.models import SchedulerJobRun, User
from app.platform.scheduler import JOB_DEFS

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_scheduler(request: Request):
    """取当前 app 的调度器实例（未启动 → 503）。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None or not scheduler.running:
        raise HTTPException(status_code=503, detail="scheduler not running")
    return scheduler


@router.get("/scheduler/jobs")
async def list_scheduler_jobs(
    request: Request,
    _: Annotated[User, Depends(rbac.require_permission("admin:scheduler:manage"))],
) -> dict:
    """任务面板：六任务定义 + 调度状态 + 上次运行（job_runs 最近一条）。"""
    scheduler = _get_scheduler(request)
    aps = scheduler.scheduler
    running_jobs = {j.id: j for j in aps.get_jobs()} if aps is not None else {}

    # 每个任务最近一次运行（job_runs）
    last_runs: dict[str, dict] = {}
    factory = request.app.state.session_factory
    async with factory() as session:
        rows = list(
            (
                await session.scalars(select(SchedulerJobRun).order_by(SchedulerJobRun.id.desc()))
            ).all()
        )
    for row in rows:
        if row.job_id not in last_runs:
            last_runs[row.job_id] = {
                "status": row.status,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "duration_ms": row.duration_ms,
                "instance": row.instance,
                "trigger": row.trigger,
                "error": row.error,
            }

    jobs = []
    for spec in JOB_DEFS:
        aps_job = running_jobs.get(spec["name"])
        jobs.append(
            {
                "name": spec["name"],
                "cron": spec["cron"],
                "desc": spec["desc"],
                "enabled": aps_job is not None,
                "next_run_time": aps_job.next_run_time.isoformat()
                if aps_job is not None and aps_job.next_run_time
                else None,
                "last_run": last_runs.get(spec["name"]),
            }
        )
    return {
        "scheduler": {
            "running": scheduler.running,
            "instance": getattr(scheduler, "_instance_id", "local"),
            "timezone": getattr(scheduler, "_timezone", "Asia/Shanghai"),
        },
        "jobs": jobs,
    }


@router.post("/scheduler/jobs/{name}/trigger")
async def trigger_scheduler_job(
    request: Request,
    name: str,
    current: Annotated[User, Depends(rbac.require_permission("admin:scheduler:manage"))],
) -> dict:
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
            target=f"/api/admin/scheduler/jobs/{name}/trigger",
            detail={"job": name},
            trace_id=getattr(request.state, "request_id", None),
        )
        await session.commit()

    return {"ok": True, "job": name, "triggered": True, "audited": True}
