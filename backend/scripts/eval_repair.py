"""错误自修复救回率评测脚本（W24 Day5）。

对应《W24学习执行手册》Day5 上午验收：**报错样本救回率 ≥50%**（30 条：错列名 10 / 错表名 10 / 语法错 10）。

构造方法（确定性坏 SQL，固定 seed 评测集）：
- 错列名：用 sqlglot 改 gold SQL 中第一个列名为错误拼写（如 amount→amountx）→ 执行报 1054；
- 错表名：改第一个表名为错误拼写（如 orders→ordersx）→ 被白名单闸拦（unknown-table，可修复类）；
- 语法错：gold SQL 末尾追加 `(` → sqlglot ParseError（parse-error，可修复类）。

评测口径：
- 把坏 SQL 经 `initial_sql` 注入 NL2SQL 图（跳过生成）→ 走 四道闸 → 执行 → 自修复循环；
- 救回 = 最终结果集与 gold 结果集比对一致（execution accuracy，复用 eval_nl2sql `_results_equal`）；
- 同时统计：修复次数分布 / 降级样本（未救回）及其修复轨迹——错例可解释。

mock / real 双路径（手册坑）：
- mock：MockRepairGenerator 按问题返回评测集 gold SQL（测链路救回必然高——不算效果）；
- real：模型池真实修复（救回率以 real 为准，gate ≥50%）。

用法：
  python -X utf8 backend/scripts/eval_repair.py            # mock（测链路）
  LLM_PROVIDER=real python -X utf8 backend/scripts/eval_repair.py  # real（测效果）

输出：控制台救回率（按坏 SQL 类型分层）+ reports/nl2sql_repair_day5.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

# scripts/ 运行：把 backend 与 scripts 加入 import path（与 eval_nl2sql.py 同策略）
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_nl2sql import _results_equal  # noqa: E402

from app.domains.data.executor import dispose_engine, execute_sql  # noqa: E402
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.sql_validator import SCM_BIZ_TABLES  # noqa: E402
from app.shared.llm import get_provider  # noqa: E402

EVAL_FILE = BACKEND / "evals" / "nl2sql_eval_v1.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"
OUT_JSON = OUT_DIR / "nl2sql_repair_day5.json"

# 每类坏 SQL 样本数（共 30）
PER_TYPE = 10


# ==================== 坏 SQL 构造（确定性） ====================


def _break_column(sql: str) -> str:
    """错列名：改第一个列名为错误拼写（amount→amountx）→ 执行报 Unknown column。

    注意 sqlglot 坑：列名 arg 键是 `this`（不是 `name`），`set("this", ...)` 才生效。
    """
    tree = sqlglot.parse_one(sql, read="mysql")
    for col in tree.find_all(exp.Column):
        col.set("this", exp.to_identifier(col.name + "x"))
        return tree.sql(dialect="mysql")
    return sql  # 无列（纯 COUNT(*)）→ 无法改列名，原样返回（此类不入选样本）


def _break_table(sql: str) -> str:
    """错表名：改第一个业务表名为错误拼写（orders→ordersx）→ 白名单闸拦 unknown-table。

    注意 sqlglot 坑：表名 arg 键是 `this`（不是 `name`）。
    """
    tree = sqlglot.parse_one(sql, read="mysql")
    for tab in tree.find_all(exp.Table):
        if tab.name in SCM_BIZ_TABLES:
            tab.set("this", exp.to_identifier(tab.name + "x"))
            return tree.sql(dialect="mysql")
    return sql


def _break_syntax(sql: str) -> str:
    """语法错：末尾追加未闭合括号 → sqlglot ParseError（parse-error，可修复类）。"""
    return sql + "("


BREAKERS = {"wrong-column": _break_column, "wrong-table": _break_table, "syntax": _break_syntax}


# ==================== 单样本跑修复链 ====================


async def run_repair_case(question: str, broken_sql: str, gold_sql: str) -> dict[str, Any]:
    """坏 SQL 注入图（initial_sql 跳过生成）→ 四道闸 → 执行 → 自修复循环 → 与 gold 比对。"""
    state = await data_graph.ainvoke(
        {"question": question, "today": DATA_BASE_DATE.isoformat(), "initial_sql": broken_sql}
    )
    res = state.get("result") or {}
    repaired = not state.get("error") and not state.get("rejected_reason") and bool(res.get("columns"))

    ok = False
    diffs: list[Any] = []
    if repaired:
        gold_res = await execute_sql(gold_sql)
        ok, diffs = _results_equal(gold_res, res)

    return {
        "question": question,
        "broken_sql": broken_sql,
        "final_sql": res.get("sql") or "",
        "rescued": ok,
        "repaired": repaired,
        "repair_attempts": state.get("repair_attempts", 0),
        "repair_exhausted": state.get("repair_exhausted", False),
        "rejected_reason": state.get("rejected_reason"),
        "error": state.get("error"),
        "diffs": diffs[:2],
        "repair_log": state.get("repair_log", []),
    }


def _pick_items(cases: list[dict], start: int, n: int, breaker) -> list[dict]:
    """从评测集顺序挑 n 条可成功构造坏 SQL 的样本。"""
    out: list[dict] = []
    for item in cases[start:]:
        broken = breaker(item["gold_sql"])
        if broken == item["gold_sql"]:
            continue  # 构造失败（如无可改列）跳过
        out.append({**item, "broken_sql": broken})
        if len(out) >= n:
            break
    return out


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r)

    def _acc(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(rs),
            "rescued": sum(1 for r in rs if r["rescued"]),
            "rescue_rate": round(sum(1 for r in rs if r["rescued"]) / max(len(rs), 1), 3),
            "degraded": sum(1 for r in rs if r["repair_exhausted"]),
            "avg_attempts": round(sum(r["repair_attempts"] for r in rs) / max(len(rs), 1), 2),
        }

    return {
        "total": len(records),
        "overall": _acc(records),
        "wrong-column": _acc(by_type.get("wrong-column", [])),
        "wrong-table": _acc(by_type.get("wrong-table", [])),
        "syntax": _acc(by_type.get("syntax", [])),
    }


def _print_summary(s: dict[str, Any]) -> None:
    print("\n== 修复救回率（gate: 整体 ≥0.50）==")
    print(f"  整体    : {s['overall']['rescue_rate']:.3f} ({s['overall']['rescued']}/{s['overall']['total']})")
    print(f"  错列名  : {s['wrong-column']['rescue_rate']:.3f} ({s['wrong-column']['rescued']}/{s['wrong-column']['total']})")
    print(f"  错表名  : {s['wrong-table']['rescue_rate']:.3f} ({s['wrong-table']['rescued']}/{s['wrong-table']['total']})")
    print(f"  语法错  : {s['syntax']['rescue_rate']:.3f} ({s['syntax']['rescued']}/{s['syntax']['total']})")
    print(f"  平均修复次数: {s['overall']['avg_attempts']} ｜ 降级样本: {s['overall']['degraded']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="错误自修复救回率评测（30 条坏 SQL）")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=PER_TYPE, help="每类样本数（默认 10）")
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.eval_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    provider = get_provider()
    print(f"修复评测: 评测集 {len(cases)} 条 ｜ provider={provider.name} ｜ 每类 {args.limit} 条坏 SQL")

    # 三类样本互不重叠地取（错列名从 #0 起、错表名错开、语法错再错开）
    col_items = _pick_items(cases, 0, args.limit, _break_column)
    tab_items = _pick_items(cases, len(cases) // 3, args.limit, _break_table)
    syn_items = _pick_items(cases, len(cases) // 3 * 2, args.limit, _break_syntax)

    records: list[dict[str, Any]] = []
    for i, item in enumerate(col_items, 1):
        r = await run_repair_case(item["question"], item["broken_sql"], item["gold_sql"])
        r["type"] = "wrong-column"
        records.append(r)
        print(f"  [列{i:>2}] {'✓' if r['rescued'] else '✗'} 修复{r['repair_attempts']}次  {item['question'][:28]}")
    for i, item in enumerate(tab_items, 1):
        r = await run_repair_case(item["question"], item["broken_sql"], item["gold_sql"])
        r["type"] = "wrong-table"
        records.append(r)
        print(f"  [表{i:>2}] {'✓' if r['rescued'] else '✗'} 修复{r['repair_attempts']}次  {item['question'][:28]}")
    for i, item in enumerate(syn_items, 1):
        r = await run_repair_case(item["question"], item["broken_sql"], item["gold_sql"])
        r["type"] = "syntax"
        records.append(r)
        print(f"  [语{i:>2}] {'✓' if r['rescued'] else '✗'} 修复{r['repair_attempts']}次  {item['question'][:28]}")

    summary = _summarize(records)
    _print_summary(summary)

    # 错例清单（未救回的可解释）
    failed = [r for r in records if not r["rescued"]]
    if failed:
        print(f"\n未救回 {len(failed)} 条（可解释，不是黑盒）：")
        for r in failed[:8]:
            why = r.get("rejected_reason") or r.get("error") or "结果不一致"
            print(f"  [{r['type']}] {r['question'][:24]} <- {str(why)[:80]}")

    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"nl2sql_repair_{'real_' if provider.name == 'real' else ''}day5.json"
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"provider": provider.name, "base_date": DATA_BASE_DATE.isoformat(),
             "summary": summary, "records": records},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写出: {out_path}")

    await dispose_engine()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
