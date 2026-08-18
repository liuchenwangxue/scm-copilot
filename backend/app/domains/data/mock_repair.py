"""NL2SQL mock 修复生成器（W24 Day5）——mock 模式下的确定性修复来源。

延续 W24 Day3 原则："mock 模式测链路、real 测效果——两个数字分开记"：
- mock 修复的职责是**验证修复链路**（execute 报错 → repair → 修复后 SQL 仍过四道闸
  → 再执行 → 救回 / 降级），不是测 LLM 的修复能力——救回率数字只来自 real 采样；
- 因此 mock 修复的确定性来源 = 评测集（jsonl）中按问题文本精确查找 gold SQL：
  命中 → 返回该标准 SQL（走完整修复链路，必然救回）；未命中 → 原样返回（继续失败 → 测降级路径）；
- `MOCK_REPAIR_MODE=fail` 强制原样返回（测试"两次失败 → 降级话术"路径专用）。

修复入口（repair.py `repair_sql`）：
    provider.name == "mock" 时实例化本生成器（mode 取环境变量 MOCK_REPAIR_MODE，默认 gold）。

实现：
    MockRepairGenerator(eval_file=None, mode="gold" | "fail")
    .generate(question, failed_sql) -> str
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class MockRepairGenerator:
    """基于评测集 gold SQL 的确定性 mock 修复器。"""

    def __init__(self, eval_file: str | Path | None = None, mode: str | None = None) -> None:
        # 默认指向 backend/evals/nl2sql_eval_v1.jsonl（与 MockSQLGenerator 同路径推导）
        if eval_file is None:
            eval_file = (
                Path(__file__).resolve().parents[3] / "evals" / "nl2sql_eval_v1.jsonl"
            )
        self.mode = (mode if mode is not None else os.getenv("MOCK_REPAIR_MODE", "gold")).strip().lower()
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

    def generate(self, question: str, failed_sql: str) -> str:
        """返回修复后的 SQL。

        - mode=fail：强制原样返回（降级路径测试——修复循环耗尽 → 降级话术）；
        - mode=gold：评测集问题命中 → gold SQL（必然救回）；未命中 → 原样返回（继续失败）。
        """
        if self.mode == "fail":
            return failed_sql
        return self._by_question.get(question.strip(), failed_sql)
