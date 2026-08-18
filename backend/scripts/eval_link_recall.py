"""★ Schema Linking 召回准确率评测（W24 Day4）——该在的表在 Top-3 里的比例。

对应《W24学习执行手册》Day4：
- 召回评测集：从 nl2sql_eval_v1.jsonl（90 条）取全部问题；
  "应包含的表"标注 = gold SQL（人工编写）经 sqlglot AST 提取的表名（金标准，权威）；
- 指标：召回准确率 = "gold 涉及表 ⊆ 召回 Top-3 表" 的条数 / 总条数（手册验收 ≥90%）；
- 分层汇总：single / join / aggregation 分开记；
- 错例清单：问题 / 标注表 / 召回表 / 得分——定位"表召回错了还是 SQL 生成错了"（Day4 错例分类）。

用法：
  python -X utf8 backend/scripts/eval_link_recall.py            # 全量 90 条
  python -X utf8 backend/scripts/eval_link_recall.py --limit 50 # 前 50 条（手册口径）

输出：控制台分层召回率 + reports/link_recall_day4.json（机器可读）
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# scripts/ 运行：把 backend 加入 import path（与 eval_nl2sql.py 同策略）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlglot
from sqlglot import exp

from app.domains.data.schema_linker import linker

EVAL_FILE = Path(__file__).resolve().parents[1] / "evals" / "nl2sql_eval_v1.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"
OUT_JSON = OUT_DIR / "link_recall_day4.json"

# 业务库六表白名单（gold SQL 只应涉及这些表）
_BIZ_TABLES = {"orders", "order_items", "products", "suppliers", "inventory", "shipments"}


def tables_from_sql(sql: str) -> list[str]:
    """从 gold SQL 提取涉及表（sqlglot AST，确定性；别名表去重后按原顺序）。"""
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return []
    seen: list[str] = []
    for t in tree.find_all(exp.Table):
        name = (t.name or "").strip()
        if name in _BIZ_TABLES and name not in seen:
            seen.append(name)
    return seen


def recall_one(question: str, gold_tables: list[str], top_k: int = 3) -> dict[str, Any]:
    """单条召回：gold 表 ⊆ 召回表 即召回成功。返回明细（供错例分类）。"""
    recalled = linker.link_tables(question, top_k=top_k)
    hit = set(gold_tables).issubset(set(recalled))
    missing = [t for t in gold_tables if t not in recalled]
    return {
        "hit": hit,
        "gold_tables": gold_tables,
        "recalled_tables": recalled,
        "missing": missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Schema Linking 召回准确率评测")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--out", default=str(OUT_JSON))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.eval_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        cases = cases[: args.limit]

    records: list[dict[str, Any]] = []
    for item in cases:
        gold = tables_from_sql(item["gold_sql"])
        r = recall_one(item["question"], gold, top_k=args.top_k)
        records.append(
            {
                "id": item["id"],
                "layer": item["layer"],
                "category": item["category"],
                "question": item["question"],
                "gold_sql": item["gold_sql"],
                **r,
            }
        )

    # ---- 汇总 ----
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_layer.setdefault(r["layer"], []).append(r)

    def _layer_recall(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        hit = sum(1 for r in rs if r["hit"])
        return {"total": n, "hit": hit, "recall": round(hit / n, 3) if n else 0.0}

    summary: dict[str, Any] = {
        "top_k": args.top_k,
        "total": len(records),
        "overall": _layer_recall(records),
        "single": _layer_recall(by_layer.get("single", [])),
        "join": _layer_recall(by_layer.get("join", [])),
        "aggregation": _layer_recall(by_layer.get("aggregation", [])),
    }

    # ---- 输出 ----
    print(f"== Schema Linking 召回准确率（gold 表 ⊆ Top-{args.top_k}）==")
    print(f"  整体 : {summary['overall']['recall']:.3f} ({summary['overall']['hit']}/{summary['overall']['total']})")
    print(f"  单表 : {summary['single']['recall']:.3f} ({summary['single']['hit']}/{summary['single']['total']})")
    print(f"  join : {summary['join']['recall']:.3f} ({summary['join']['hit']}/{summary['join']['total']})")
    print(f"  聚合 : {summary['aggregation']['recall']:.3f} ({summary['aggregation']['hit']}/{summary['aggregation']['total']})")

    errors = [r for r in records if not r["hit"]]
    print(f"\n漏召回 {len(errors)} 条：")
    for r in errors:
        print(f"  [{r['id']}] {r['layer']:6s} gold={r['gold_tables']} "
              f"recall={r['recalled_tables']} missing={r['missing']}")
        print(f"        {r['question'][:44]}")

    # 错因分类：全漏 vs 部分漏
    if errors:
        full_miss = sum(1 for r in errors if not r["gold_tables"] or not any(t in r["recalled_tables"] for t in r["gold_tables"]))
        part_miss = len(errors) - full_miss
        print(f"\n错因分类：完全漏召回 {full_miss} / 部分漏 {part_miss}")

    # 各表被问及频次 vs 漏召回频次（排查语料薄弱点）
    asked: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    for r in records:
        for t in r["gold_tables"]:
            asked[t] += 1
            if t in r["missing"]:
                missed[t] += 1
    print("\n各表被问及/漏召回：")
    for t in sorted(asked):
        print(f"  {t:12s} asked={asked[t]:>3}  miss={missed.get(t, 0)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "records": records, "errors": errors}
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写出: {args.out}")


if __name__ == "__main__":
    main()
