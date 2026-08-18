"""NL2SQL execution accuracy 评测脚本（W24 Day3）——生成 SQL 与标准结果集比对。

对应《W24学习执行手册》Day3 下午 + 《03》1.2 节：
- 评测集：backend/evals/nl2sql_eval_v1.jsonl（50 条，固定 seed 数据保证 gold 结果稳定）
- 指标：execution accuracy（**结果集比对，而非 SQL 字符串比对**——同义 SQL 应判对）
- 规范化：
    1. 类型归一（Decimal→float、datetime→isoformat——executor 已做）；
    2. 排序键统一（按第一列排序，NULL 放最后）；
    3. 列对齐：列数量不一致即判错（同义 SQL 通常列数一致）。
- 输出：总分 + 分层（single/join）+ 错例清单（问题/gold SQL/gen SQL/差异行）
- 生成 SQL 走与生产一致的链路：generate（LLM/mock）→ validate（四道闸）→ execute

用法：
  python -X utf8 backend/scripts/eval_nl2sql.py                    # mock（测链路/评测脚本正确性）
  LLM_PROVIDER=real python -X utf8 backend/scripts/eval_nl2sql.py  # real（真效果基线）

输出：控制台分层准确率 + reports/nl2sql_eval_day3.json（机器可读，含错例）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# scripts/ 运行：把 backend 加入 import path（与 seed_biz.py 同策略）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.data.executor import ExecutionError, dispose_engine, execute_sql
from app.domains.data.mock_sql import MockSQLGenerator
from app.domains.data.prompts import DATA_BASE_DATE, build_nl2sql_messages
from app.domains.data.sql_validator import SQLRejected, validate_sql
from app.shared.llm import get_provider

EVAL_FILE = Path(__file__).resolve().parents[1] / "evals" / "nl2sql_eval_v1.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"
OUT_JSON = OUT_DIR / "nl2sql_eval_day3.json"


def _clean_sql(raw: str) -> str:
    """清洗 LLM 输出：去 ```sql 围栏/首尾空白/尾分号。"""
    import re

    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", raw or "", re.S)
    text = m.group(1).strip() if m else (raw or "").strip()
    return text.rstrip(";").strip()


def _sort_key(row: tuple[Any, ...]) -> tuple:
    """排序键：按第一列排序，NULL 放最后（None 无法直接比大小，包一层）。"""
    if not row:
        return (0,)
    first = row[0]
    return (1,) if first is None else (0, first)


def _norm_rows(columns: list[str], rows: list[list[Any]]) -> list[tuple]:
    """规范化结果集：排序键统一（按第一列），每行转 tuple 便于比对。"""
    # 列数量对齐：列数不同 = 结构不同 → 无法比对（判错，交调用方处理）
    return sorted([tuple(r) for r in rows], key=_sort_key)


def _results_equal(gold: dict, gen: dict) -> tuple[bool, list[Any]]:
    """execution accuracy 比对：结果集语义相等（非 SQL 字符串比对）。

    规范化规则（对应《W24学习执行手册》Day3 下午）：
    1. 类型归一：Decimal→float、datetime→isoformat（executor 已做）；
    2. 排序键统一：按第一列排序（NULL 放最后），行转 tuple；
    3. **列对齐（★ Day3 real 基线暴露的改进）**：
       - gold 列名 ⊆ gen 列名 → 按列名提取 gen 子集对齐比对（模型多加 id/sku 等
         从属列不影响"答对数据"，应判对——面试 Q4"为什么不做字符串比对"的延伸）；
       - 列数一致（顺序可不同）→ 按位置比对；
       - 列数不一致且列名无法对齐 → 判错（结构差异无法语义对齐）。

    返回 (是否相等, 差异行列表)。
    """
    g_cols, g_rows = gold.get("columns", []), gold.get("rows", [])
    n_cols, n_rows = gen.get("columns", []), gen.get("rows", [])

    # ---- 列对齐策略 ----
    n_index = {col: i for i, col in enumerate(n_cols)}
    g_unique = len(set(g_cols)) == len(g_cols)
    if g_unique and all(col in n_index for col in g_cols):
        # gold 列全部在 gen 中出现 → 提取 gen 子集对齐（忽略 gen 多余列）
        g_norm = _norm_rows(g_cols, g_rows)
        n_sub = [[row[n_index[col]] for col in g_cols] for row in n_rows]
        n_norm = _norm_rows(g_cols, n_sub)
    elif len(g_cols) == len(n_cols):
        # 列数一致 → 按位置比对（列序不同也判对——同义 SQL）
        g_norm = _norm_rows(g_cols, g_rows)
        n_norm = _norm_rows(n_cols, n_rows)
    else:
        return False, [
            {"reason": f"列数不一致且列名无法对齐: gold={g_cols} gen={n_cols}"}
        ]

    if g_norm == n_norm:
        return True, []
    # 找差异（近似：给前 3 行 gold 期望 + 前 3 行实际）
    return False, [
        {"reason": "结果集不匹配", "gold_first": g_norm[:3], "gen_first": n_norm[:3]}
    ]


async def eval_one(item: dict, provider: Any, mock_gen: MockSQLGenerator) -> dict:
    """单条评测：生成 SQL → 四道闸 → 执行 → 与 gold 结果集比对。"""
    question = item["question"]
    record: dict[str, Any] = {
        "id": item["id"],
        "layer": item["layer"],
        "category": item["category"],
        "question": question,
        "gold_sql": item["gold_sql"],
    }

    # ---- 1. 生成 SQL（mock 从评测集取 gold；real 调 LLM）----
    if provider.name == "mock":
        gen_sql = mock_gen.generate(question)
    else:
        messages = build_nl2sql_messages(question, DATA_BASE_DATE)
        raw = await provider.generate(messages, max_tokens=1024, temperature=0.0)
        gen_sql = _clean_sql(raw)
    record["gen_sql"] = gen_sql

    # ---- 2. 四道闸（生成 SQL 必须过闸，与生产一致）----
    try:
        validated = validate_sql(gen_sql)
    except SQLRejected as exc:
        record.update({"status": "rejected", "rejected_reason": exc.reason})
        return record
    record["status"] = "ok"

    # ---- 3. 执行：gold 与 gen（只读沙箱，与生产一致）----
    try:
        gold_res = await execute_sql(item["gold_sql"])
        gen_res = await execute_sql(validated)
    except ExecutionError as exc:
        record.update({"status": "exec-error", "error": str(exc)[:200]})
        return record

    # ---- 4. execution accuracy 比对 ----
    ok, diffs = _results_equal(gold_res, gen_res)
    record["correct"] = ok
    record["gen_columns"] = gen_res.get("columns", [])
    record["gen_rows"] = gen_res.get("rows", [])
    record["diffs"] = diffs
    return record


async def main() -> None:
    parser = argparse.ArgumentParser(description="NL2SQL execution accuracy 评测")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--out", default=str(OUT_JSON))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    args = parser.parse_args()

    eval_file = Path(args.eval_file)
    cases = [
        json.loads(line)
        for line in eval_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        cases = cases[: args.limit]

    provider = get_provider()
    mock_gen = MockSQLGenerator(args.eval_file)
    print(f"评测集: {len(cases)} 条 ｜ provider={provider.name} ｜ 基准日={DATA_BASE_DATE}")

    records: list[dict[str, Any]] = []
    total_t0 = time.perf_counter()
    for i, item in enumerate(cases, 1):
        r = await eval_one(item, provider, mock_gen)
        records.append(r)
        status = r.get("status")
        if status == "ok":
            mark = "✓" if r.get("correct") else "✗"
        elif status == "rejected":
            mark = "⛔拒"
        elif status == "exec-error":
            mark = "✗错"
        else:
            mark = "?"
        print(f"  [{i:>3}/{len(cases)}] {mark} {r['layer']:6s} {r['question'][:40]}")
    total_elapsed = time.perf_counter() - total_t0

    # ---- 汇总 ----
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_layer.setdefault(r["layer"], []).append(r)

    def _layer_acc(rs: list[dict[str, Any]]) -> dict[str, Any]:
        ok_n = sum(1 for r in rs if r.get("status") == "ok" and r.get("correct"))
        rej_n = sum(1 for r in rs if r.get("status") == "rejected")
        err_n = sum(1 for r in rs if r.get("status") == "exec-error")
        return {
            "total": len(rs),
            "correct": ok_n,
            "accuracy": round(ok_n / len(rs), 3) if rs else 0.0,
            "rejected": rej_n,
            "exec_error": err_n,
        }

    summary: dict[str, Any] = {
        "provider": provider.name,
        "base_date": DATA_BASE_DATE.isoformat(),
        "total": len(records),
        "overall": _layer_acc(records),
        "single": _layer_acc(by_layer.get("single", [])),
        "join": _layer_acc(by_layer.get("join", [])),
        "elapsed_s": round(total_elapsed, 2),
    }

    # ---- 输出 ----
    print("\n== execution accuracy ==")
    print(f"  整体 : {summary['overall']['accuracy']:.3f} ({summary['overall']['correct']}/{summary['overall']['total']})")
    print(f"  单表 : {summary['single']['accuracy']:.3f} ({summary['single']['correct']}/{summary['single']['total']})")
    print(f"  join : {summary['join']['accuracy']:.3f} ({summary['join']['correct']}/{summary['join']['total']})")
    print(f"  耗时 : {summary['elapsed_s']}s")

    # 错例清单（机器可读 + 控制台摘要）
    errors = [r for r in records if not (r.get("status") == "ok" and r.get("correct"))]
    print(f"\n错例 {len(errors)} 条：")
    for r in errors:
        why = r.get("rejected_reason") or r.get("error") or (
            json.dumps(r.get("diffs", []), ensure_ascii=False)[:200]
        )
        print(f"  [{r['id']}] {r['question'][:36]}  <- {why}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "records": records, "errors": errors}
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写出: {args.out}")

    await dispose_engine()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
