"""daily_brief：经营日报（工作日 08:00，W25 Day3 实现）。

cron: 0 8 * * 1-5
作用：三条 NL2SQL（昨日 GMV / 延迟发货率 / TOP5 供应商）→ 模板渲染 brief
（含 SQL 链接可回溯）→ 写 daily_briefs 表 + 推送订阅用户。
幂等键：brief:{date}（Redis SETNX，重复执行直接跳）。

Day1 为占位实现，真实逻辑见 W25 Day3。
"""

CRON = "0 8 * * 1-5"


async def run() -> dict:
    # TODO(W25 Day3): 日报生成——NL2SQL 三问 → 模板渲染 → daily_briefs 表 + 订阅推送
    return {"job": "daily_brief", "status": "stub", "note": "implemented in W25 Day3"}
