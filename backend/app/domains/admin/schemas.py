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


class ApiKeyCreateIn(BaseModel):
    """API Key 创建请求体（★ W25 Day5：机器身份）。"""

    name: str = Field(..., min_length=1, max_length=64, description="Key 名称（如 data-pipeline / ci-bot）")
    owner_username: str | None = Field(None, description="属主用户名；缺省=当前用户（服务账号继承 owner 权限）")


class ApiKeyCreatedOut(BaseModel):
    """API Key 创建响应（明文 key 只在创建时返回一次，之后仅展示前缀）。"""

    key_id: int
    name: str
    key_prefix: str = Field(description="展示用前缀（sk- + 前 8 位）")
    api_key: str = Field(description="完整 Key（明文，仅此一次！请立即保存）")
    owner_username: str


class ApiKeyOut(BaseModel):
    """API Key 列表项（不返回哈希，不返回明文）。"""

    key_id: int
    name: str
    key_prefix: str
    owner_username: str | None
    enabled: bool
    created_at: str | None = None


class ApiKeyListOut(BaseModel):
    """API Key 列表响应。"""

    api_keys: list[ApiKeyOut]
    total: int


class ApiKeyRevokeOut(BaseModel):
    """API Key 吊销响应。"""

    ok: bool
    key_id: int
    revoked: bool
