"""eval_nightly：夜间质量回归（每日 02:00，W25 Day3 实现）。

cron: 0 2 * * *
作用：RAG 156 条 + NL2SQL 100 条评测集全量回归（mock，断言结构）→ 报告落
eval_reports 表（各域分数 + 与 7 日均值偏离，劣化 >5pp 标红）。
幂等：结果快照（同日期同域只跑一次）。

Day1 为占位实现，真实逻辑见 W25 Day3。
"""

CRON = "0 2 * * *"


async def run() -> dict:
    # TODO(W25 Day3): 夜间回归——RAG/NL2SQL 评测集全量 mock 跑 → eval_reports 落库
    return {"job": "eval_nightly", "status": "stub", "note": "implemented in W25 Day3"}
