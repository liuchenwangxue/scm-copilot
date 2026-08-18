"""平台管理域 API 契约（★ W25 Day4 OpenAPI 规范化）——调度面板请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JobRunOut(BaseModel):
    """单次任务运行记录（job_runs 一行，零重复观测依据）。"""

    status: str = Field(description="success / skipped / failed")
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    instance: str | None = Field(None, description="执行实例（backend-a1/a2）")
    trigger: str | None = Field(None, description="cron / manual")
    error: str | None = None


class SchedulerJobOut(BaseModel):
    """调度任务定义 + 调度状态 + 最近运行。"""

    name: str
    cron: str = Field(description="cron 表达式")
    desc: str
    enabled: bool = Field(description="是否已注册到调度器")
    next_run_time: str | None = None
    last_run: JobRunOut | None = None
    recent_runs: list[JobRunOut] = Field(default_factory=list, description="最近 5 条运行")


class SchedulerJobsOut(BaseModel):
    """任务面板响应。"""

    scheduler: dict[str, Any] = Field(description="{running, instance, timezone}")
    jobs: list[SchedulerJobOut]


class SchedulerTriggerOut(BaseModel):
    """手动触发响应（触发的是独立一次性 job，不覆盖原 cron）。"""

    ok: bool
    job: str
    triggered: bool
    audited: bool
