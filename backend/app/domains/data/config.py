"""NL2SQL 数据域单一配置源（★ W27 Day6 B6/B7：常量收敛）。

把散落在 validator / executor / session_ctx / prompts 的"限制常量 / 魔法数"
收拢到一处，消除双定义漂移（B6）与魔法数硬编码（B7）：

- `MAX_ROWS` / `EXEC_TIMEOUT_SECONDS` / `MAX_RESULT_BYTES`：SQL 沙箱三重资源约束
  （sql_validator 强制 LIMIT、executor 超时与结果截断共用同一份定义）；
- `DEFAULT_TTL_SECONDS` / `DEFAULT_MAX_TURNS`：多轮会话 Redis TTL 与轮数上限
  （session_ctx 使用）；
- `DATA_BASE_DATE`：评测数据基准日，可由 `SCM_DATA_BASE_DATE` 环境变量覆盖
  （评测可复现性：评测脚本显式传固定值，运行日漂移不污染结果）。
"""

from __future__ import annotations

import logging
import os
from datetime import date

logger = logging.getLogger("scm.data.config")

# ---- NL2SQL 沙箱三重资源约束（validator / executor 共用） ----
MAX_ROWS = 200
EXEC_TIMEOUT_SECONDS = 3.0
MAX_RESULT_BYTES = 1_048_576  # 1MB

# ---- 多轮会话（session_ctx 使用） ----
DEFAULT_TTL_SECONDS = 30 * 60  # 1800s：会话 Redis TTL（读/写路径都刷新，活跃会话不过期）
DEFAULT_MAX_TURNS = 4


def _parse_base_date() -> date:
    """从 `SCM_DATA_BASE_DATE` 解析数据基准日；未设/非法 → 默认 2026-08-18。

    ★ W27-D6 (B7)：评测可复现性——评测脚本显式传固定基准日，运行日漂移
    不会让"近 N 天"类 few-shot 产生空结果集被误判为 SQL 错。
    """
    raw = os.getenv("SCM_DATA_BASE_DATE", "2026-08-18")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        logger.warning("SCM_DATA_BASE_DATE=%r 非法（应为 YYYY-MM-DD），回退默认 2026-08-18", raw)
        return date(2026, 8, 18)


# ---- 评测数据基准日（与 scripts/seed_biz.py BASE_DATE 对齐） ----
DATA_BASE_DATE = _parse_base_date()
