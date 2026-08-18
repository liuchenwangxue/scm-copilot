"""nl2sql：自然语言查数据（表格 + SQL + 洞察）。

对应后端 `POST /api/v1/data/query`（Nl2SqlOut 契约）。`as_dataframe=True`
时用 pandas 转 DataFrame（可选依赖：`pip install scm-copilot-client[dataframe]`，
缺 pandas 时抛出带提示的 `ScmError`，不让调用方在运行时困惑）。
"""

from __future__ import annotations

from typing import Any

from scm_client.errors import ScmError
from scm_client.models import Nl2SqlResult


def build_dataframe(payload: dict[str, Any]) -> Any:
    """把 {columns, rows} 结果转 pandas.DataFrame（可选依赖，缺则抛带安装提示的错误）。"""
    if not payload.get("table"):
        return None
    try:
        import pandas as pd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ScmError(
            0,
            "PANDAS_MISSING",
            "as_dataframe=True 需要 pandas：pip install scm-copilot-client[dataframe]",
            "",
        ) from exc
    return pd.DataFrame(list(payload.get("rows") or []), columns=list(payload.get("columns") or []))
