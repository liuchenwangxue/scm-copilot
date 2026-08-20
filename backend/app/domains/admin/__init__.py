"""平台管理域（admin）——W25 Day2：调度面板 API 起步。

承载：`/api/v1/admin/scheduler/jobs`（任务状态 / 手动触发）、`/api/v1/admin/apikeys`
（机器身份管理）、`/api/v1/admin/brief/charts`（★ W28 Day3：BI 图表数据）。
import 纪律：域内模块只依赖本域（app.domains.admin.*）与平台基座/共享层，
不 import 其他业务域（ADR-01 模块化单体边界）。
"""
