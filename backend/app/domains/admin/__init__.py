"""平台管理域（admin）——W25 Day2：调度面板 API 起步。

承载：`/api/admin/scheduler/jobs`（任务状态 / 手动触发）。
import 纪律：域内模块只依赖本域（app.domains.admin.*）与平台基座/共享层，
不 import 其他业务域（ADR-01 模块化单体边界）。

后续（W26 Day1）：业务监控面板五区同样挂本域（job_runs 失败率/评测分数趋势）。
"""
