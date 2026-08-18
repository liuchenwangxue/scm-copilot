"""W25 调度域六任务包：每个模块一个 cron 任务，导出 `CRON` 与 `async def run()`。

Day1 注册表占位实现（保证调度基座闭环：注册→触发→job_runs 记录）；
真实业务逻辑在 W25 Day2/3 逐个填充（kb_sync→cleanup→archive→daily_brief→eval_nightly→warmup）。

约定：
- 每个任务模块必须导出 `CRON`（crontab 表达式）与 `async def run() -> dict`
- run() 只做业务，不做锁/记录——互斥与 job_runs 由 scheduler 层统一包装
"""
