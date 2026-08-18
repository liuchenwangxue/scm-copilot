"""audit_archive：审计日志按月归档（每月 1 日 04:00，W25 Day2 实现）。

cron: 0 4 1 * *
作用：上月 audit_logs → audit_logs_2026m08 归档表（CREATE TABLE ... AS SELECT +
校验行数 + 删主表）；归档批次号记 job_runs（幂等：批次已存在则跳过）。

Day1 为占位实现，真实逻辑见 W25 Day2。
"""

CRON = "0 4 1 * *"


async def run() -> dict:
    # TODO(W25 Day2): 审计归档——按月建表迁移 + 行数校验 + 删主表 + 批次号幂等
    return {"job": "audit_archive", "status": "stub", "note": "implemented in W25 Day2"}
