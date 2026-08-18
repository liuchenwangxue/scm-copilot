"""NL2SQL mock SQL 生成器（W24 Day3）——mock 模式下的确定性 SQL 来源。

设计（对应《W24学习执行手册》坑："mock 模式测链路、real 测效果——两个数字分开记"）：
- mock 的职责是**验证链路**（generate → validate → execute → format 全通 + 评测脚本可跑），
  不是测 LLM 效果——准确率数字只来自 real 采样，两者分开记录；
- 因此 mock 生成 SQL 的确定性来源 = 评测集（jsonl）中**按问题文本精确查找 gold SQL**，
  命中返回该标准 SQL（走完整链路），未命中返回一个安全的默认查询；
- 这样 mock 评测跑出的数字接近 1.0 不代表效果，仅证明"评测脚本 + graph 链路正确"；
  真实基线以 real 结果为准（`eval_nl2sql.py --provider real`）。

实现：
    MockSQLGenerator(eval_file)  # eval_file: backend/evals/nl2sql_eval_v1.jsonl
    .generate(question) -> str
"""

from __future__ import annotations

import json
from pathlib import Path

# 默认安全 SQL（评测集里没有的问题，保证链路仍能跑通）
_FALLBACK_SQL = "SELECT COUNT(*) AS cnt FROM orders"


class MockSQLGenerator:
    """基于评测集 gold SQL 的确定性 mock 生成器。"""

    def __init__(self, eval_file: str | Path | None = None) -> None:
        # 默认指向 backend/evals/nl2sql_eval_v1.jsonl
        # __file__ = backend/app/domains/data/mock_sql.py → parents[3] = backend/
        if eval_file is None:
            eval_file = (
                Path(__file__).resolve().parents[3] / "evals" / "nl2sql_eval_v1.jsonl"
            )
        self._by_question: dict[str, str] = {}
        path = Path(eval_file)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                q = (item.get("question") or "").strip()
                sql = (item.get("gold_sql") or "").strip()
                if q and sql:
                    self._by_question[q] = sql
        self._path = path

    @property
    def loaded_count(self) -> int:
        return len(self._by_question)

    def generate(self, question: str) -> str:
        """按问题精确匹配返回评测集 gold SQL；未命中返回默认安全查询。"""
        return self._by_question.get(question.strip(), _FALLBACK_SQL)
