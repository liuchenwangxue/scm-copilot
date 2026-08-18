"""W25 调度域六任务包：每个模块一个 cron 任务，导出 `CRON` 与 `async def run()`。

Day1 注册表占位 → Day2/3 逐个填真实逻辑：
- ★ Day2 已实现：kb_increment_sync（KB 增量同步）/ vector_cleanup（向量卫生）/
  audit_archive（审计归档）
- Day3 待实现：daily_brief（日报）/ eval_nightly（夜间回归）/ cache_warmup（预热）

约定：
- 每个任务模块必须导出 `CRON`（crontab 表达式）与 `async def run() -> dict`
- run() 只做业务，不做锁/记录——互斥与 job_runs 由 scheduler 层统一包装
- run() 返回 dict 供 job_runs/面板展示；异常由 scheduler 层记 failed 下轮重试
"""
