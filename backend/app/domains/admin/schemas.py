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


# ==================== W28 Day3：BI 图表数据 ====================


class BriefChartPoint(BaseModel):
    """单日快照点（daily_briefs.metrics 固化口径，非现算）。"""

    date: str = Field(description="日报归属日 YYYY-MM-DD")
    gmv: float | None = Field(None, description="当日 GMV（元）；早期 brief 缺字段为 None")
    delay_rate: float | None = Field(None, description="当日延迟发货率（%）")


class TopSupplierItem(BaseModel):
    """最近一日 TOP5 供应商（按订单金额降序）。"""

    rank: int
    supplier: str
    gmv: float | None = Field(None, description="该供应商昨日订单金额（元）")


class BriefSqlOut(BaseModel):
    """SQL 回溯项（数字可验证：图表 = 快照的可视化，SQL 来自已落库原文）。"""

    key: str = Field(description="gmv / delay_rate / top_suppliers")
    question: str
    sql: str = Field(description="执行过的 SQL 原文（可回溯）；被安全闸拒绝时为空串")


class BriefChartsOut(BaseModel):
    """BI 图表数据（近 7 日）：GMV 折线 / 延迟率趋势 / TOP5 柱状一次取齐。"""

    latest_date: str | None = Field(None, description="最近一日（无记录为 None，前端显示空态）")
    points: list[BriefChartPoint] = Field(default_factory=list, description="近 7 日，按日期升序")
    top_suppliers: list[TopSupplierItem] = Field(default_factory=list, description="最近一日 TOP5")
    sqls: list[BriefSqlOut] = Field(default_factory=list, description="三条模板 SQL 原文（可回溯）")
    baseline_delay_rate: float = Field(default=9.91, description="延迟率基准虚线（W25 首份实测 9.91%）")
