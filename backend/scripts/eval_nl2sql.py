"""NL2SQL execution accuracy 评测脚本（W24 Day3 + Day4 演进）——生成 SQL 与标准结果集比对。

对应《W24学习执行手册》Day3 下午 +《03》1.2 节：
- 评测集：backend/evals/nl2sql_eval_v1.jsonl（Day3 50 条 → Day4 扩到 90 条三层：
  单表 30 / join 40 / 聚合 20，固定 seed 数据保证 gold 结果稳定）
- 指标：execution accuracy（**结果集比对，而非 SQL 字符串比对**——同义 SQL 应判对）
- 规范化：
    1. 类型归一（Decimal→float、datetime→isoformat——executor 已做）；
    2. 排序键统一（按第一列排序，NULL 放最后）；
    3. 列对齐：gold 列 ⊆ gen 列 → 按列名子集对齐；列数一致 → 按位置（Day3 改进）。
- 输出：总分 + 分层（single/join/aggregation）+ 错例清单（问题/gold SQL/gen SQL/差异行）

★ Day4 新增：
- `--prompt-version v1|v2`：单版本跑（v1 全 schema / v2 Schema Linking 召回）；
- `--ab`：A/B 同数据集对比 v1 vs v2（准确率 + prompt token 估算，
  目标：v2 准确率 ≥ v1 - 2pp 且 token 降 ≥50%）；
- 每条记录 prompt_tokens（estimate_prompt_tokens 估算，A/B 同口径对比）。

用法：
  python -X utf8 backend/scripts/eval_nl2sql.py                     # mock 全量（测链路）
  LLM_PROVIDER=real python -X utf8 backend/scripts/eval_nl2sql.py   # real 全量
  python -X utf8 backend/scripts/eval_nl2sql.py --ab --limit 50     # mock A/B（token 对比）
  LLM_PROVIDER=real python -X utf8 backend/scripts/eval_nl2sql.py --ab  # real A/B

输出：控制台分层准确率 + reports/nl2sql_eval_day3.json（默认）或 reports/nl2sql_eval_ab_day4.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# scripts/ 运行：把 backend 加入 import path（与 seed_biz.py 同策略）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.data.executor import ExecutionError, dispose_engine, execute_sql
from app.domains.data.mock_sql import MockSQLGenerator
from app.domains.data.prompts import (
    DATA_BASE_DATE,
    build_nl2sql_messages_v1,
    build_nl2sql_messages_v2,
    estimate_prompt_tokens,
)
from app.domains.data.schema_linker import linker
from app.domains.data.sql_validator import SQLRejected, validate_sql
from app.shared.llm import get_provider

EVAL_FILE = Path(__file__).resolve().parents[1] / "evals" / "nl2sql_eval_v1.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"
OUT_JSON = OUT_DIR / "nl2sql_eval_day3.json"
OUT_AB_JSON = OUT_DIR / "nl2sql_eval_ab_day4.json"

_LAYERS = ("single", "join", "aggregation")


def _clean_sql(raw: str) -> str:
    """清洗 LLM 输出：去 ```sql 围栏/首尾空白/尾分号。"""
    import re

    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", raw or "", re.S)
    text = m.group(1).strip() if m else (raw or "").strip()
    return text.rstrip(";").strip()


def _sort_key(row: tuple[Any, ...]) -> tuple:
    """排序键：按**整行**排序（多列分组/复合排序结果集也能对齐），NULL 放最后。

    ★ W24 Day6 修复：旧实现只按第一列排序——"各区域各状态"这类多列分组
    （region,status）gold/gen 第一列相同但第二列顺序不同时被误判为错。
    """
    return tuple((1,) if v is None else (0, v) for v in row)


def _norm_rows(columns: list[str], rows: list[list[Any]]) -> list[tuple]:
    """规范化结果集：排序键统一（按整行，NULL 放最后），每行转 tuple 便于比对。"""
    return sorted([tuple(r) for r in rows], key=_sort_key)


# 列名归一（★ Day4 A/B 暴露：同义列别名误判——cnt vs order_count / total_sales vs total_amount）
# 归一规则保守：只折叠"计数/金额"两类高频聚合别名，其余列名原样保留。
_COUNT_COL_RE = re.compile(r"cnt|count|_num|order_count|delayed_count|delayed_cnt|数量|次数")
_AMOUNT_COL_RE = re.compile(r"amount|sales|sum|total|value|revenue|金额|总值|销售额")


def _norm_col(name: str) -> str:
    n = str(name).strip().lower()
    if _AMOUNT_COL_RE.search(n):
        return "amount"
    if _COUNT_COL_RE.search(n):
        return "count"
    return n


def _align_subset(
    g_cols: list[str], g_rows: list[list[Any]], n_cols: list[str], n_rows: list[list[Any]]
) -> tuple[list[tuple], list[tuple]] | None:
    """按列名子集对齐（支持同义列名归一）。成功返回 (gold_norm, gen_norm)。"""
    g_unique = len(set(g_cols)) == len(g_cols)
    if not g_unique:
        return None
    n_index = {col: i for i, col in enumerate(n_cols)}
    # 1) 精确列名子集
    if all(col in n_index for col in g_cols):
        g_norm = _norm_rows(g_cols, g_rows)
        n_sub = [[row[n_index[col]] for col in g_cols] for row in n_rows]
        return g_norm, _norm_rows(g_cols, n_sub)
    # 2) 归一列名后子集（cnt↔order_count / total_amount↔total_sales 同义判对）
    n_norm_index: dict[str, int] = {}
    for col, i in n_index.items():
        n_norm_index.setdefault(_norm_col(col), i)
    g_norm_cols = [_norm_col(c) for c in g_cols]
    if all(c in n_norm_index for c in g_norm_cols):
        g_norm = _norm_rows(g_cols, g_rows)
        n_sub = [[row[n_norm_index[c]] for c in g_norm_cols] for row in n_rows]
        return g_norm, _norm_rows(g_cols, n_sub)
    return None


def _results_equal(gold: dict, gen: dict) -> tuple[bool, list[Any]]:
    """execution accuracy 比对：结果集语义相等（非 SQL 字符串比对）。

    规范化规则（对应《W24学习执行手册》Day3 下午）：
    1. 类型归一：Decimal→float、datetime→isoformat（executor 已做）；
    2. 排序键统一：按第一列排序（NULL 放最后），行转 tuple；
    3. **列对齐**：
       - gold 列名 ⊆ gen 列名（含同义归一：cnt↔order_count、total_amount↔total_sales）
         → 按列名提取 gen 子集对齐比对（模型多加 id/sku 等从属列、别名不同不影响
         "答对数据"，应判对——面试 Q4"为什么不做字符串比对"的延伸）；
       - 列数一致（顺序可不同）→ 按位置比对；
       - 均不满足 → 判错（结构差异无法语义对齐）。

    返回 (是否相等, 差异行列表)。
    """
    g_cols, g_rows = gold.get("columns", []), gold.get("rows", [])
    n_cols, n_rows = gen.get("columns", []), gen.get("rows", [])

    # ---- 列对齐策略 ----
    aligned = _align_subset(g_cols, g_rows, n_cols, n_rows)
    if aligned is not None:
        g_norm, n_norm = aligned
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


def _build_messages(
    question: str, prompt_version: str, tables: list[str] | None
) -> list[dict[str, str]]:
    """按版本构建 prompt（v1 全 schema / v2 召回；tables 供 A/B 复用同一召回）。"""
    if prompt_version == "v2":
        return build_nl2sql_messages_v2(question, DATA_BASE_DATE, tables)
    return build_nl2sql_messages_v1(question, DATA_BASE_DATE)


async def eval_one(
    item: dict,
    provider: Any,
    mock_gen: MockSQLGenerator,
    prompt_version: str = "v1",
    tables: list[str] | None = None,
) -> dict:
    """单条评测：生成 SQL → 四道闸 → 执行 → 与 gold 结果集比对。

    prompt_version：v1|v2；tables：v2 召回表（A/B 复用同一召回结果）。
    prompt_tokens 一律记录（mock 也构建 messages 统计，供 A/B 对比）。
    """
    question = item["question"]
    record: dict[str, Any] = {
        "id": item["id"],
        "layer": item["layer"],
        "category": item["category"],
        "question": question,
        "gold_sql": item["gold_sql"],
    }

    # ---- 0. 构建 prompt + 统计 token（两版同口径，A/B 用）----
    messages = _build_messages(question, prompt_version, tables)
    record["prompt_tokens"] = estimate_prompt_tokens(messages)
    record["prompt_version"] = prompt_version
    record["recalled_tables"] = tables or []

    # ---- 1. 生成 SQL（mock 从评测集取 gold；real 调 LLM）----
    if provider.name == "mock":
        gen_sql = mock_gen.generate(question)
    else:
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


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """分层汇总（single/join/aggregation + 整体）。"""
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

    return {
        "total": len(records),
        "overall": _layer_acc(records),
        "single": _layer_acc(by_layer.get("single", [])),
        "join": _layer_acc(by_layer.get("join", [])),
        "aggregation": _layer_acc(by_layer.get("aggregation", [])),
    }


def _print_summary(tag: str, summary: dict[str, Any]) -> None:
    print(f"\n== {tag} ==")
    print(f"  整体 : {summary['overall']['accuracy']:.3f} ({summary['overall']['correct']}/{summary['overall']['total']})")
    print(f"  单表 : {summary['single']['accuracy']:.3f} ({summary['single']['correct']}/{summary['single']['total']})")
    print(f"  join : {summary['join']['accuracy']:.3f} ({summary['join']['correct']}/{summary['join']['total']})")
    print(f"  聚合 : {summary['aggregation']['accuracy']:.3f} ({summary['aggregation']['correct']}/{summary['aggregation']['total']})")


def _avg_tokens(records: list[dict[str, Any]]) -> float:
    return round(sum(r.get("prompt_tokens", 0) for r in records) / max(len(records), 1), 1)


async def run(
    cases: list[dict],
    provider: Any,
    mock_gen: MockSQLGenerator,
    prompt_version: str,
    reuse_tables: bool,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """跑一轮（单版本）。reuse_tables=True 时对 v2 先整体召回（A/B 复用，避免重复 embedding）。

    out_path：非 None 时写文件（单版本模式）；A/B 模式由调用方统一合并写。
    """
    # A/B 复用召回：先全量 link+裁剪一次（加载模型后 90 条召回很快）
    prelinked: dict[str, list[str]] = {}
    if prompt_version == "v2" and reuse_tables:
        for item in cases:
            prelinked[item["question"]] = linker.link_prompt_tables(item["question"])

    records: list[dict[str, Any]] = []
    total_t0 = time.perf_counter()
    for i, item in enumerate(cases, 1):
        tables = prelinked.get(item["question"]) if reuse_tables else None
        r = await eval_one(item, provider, mock_gen, prompt_version, tables)
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
        print(f"  [{i:>3}/{len(cases)}] {mark} {r['layer']:11s} v{r['prompt_version']} tok={r['prompt_tokens']:>5} {r['question'][:36]}")
    total_elapsed = time.perf_counter() - total_t0

    summary = _summarize(records)
    summary["elapsed_s"] = round(total_elapsed, 2)
    summary["avg_prompt_tokens"] = _avg_tokens(records)

    errors = [r for r in records if not (r.get("status") == "ok" and r.get("correct"))]
    print(f"\n错例 {len(errors)} 条：")
    for r in errors:
        why = r.get("rejected_reason") or r.get("error") or (
            json.dumps(r.get("diffs", []), ensure_ascii=False)[:200]
        )
        print(f"  [{r['id']}] {r['question'][:36]}  <- {why}")

    payload = {
        "provider": provider.name,
        "prompt_version": prompt_version,
        "base_date": DATA_BASE_DATE.isoformat(),
        "summary": summary,
        "records": records,
        "errors": errors,
    }
    if out_path is not None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出: {out_path}")
    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description="NL2SQL execution accuracy 评测")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全量）")
    parser.add_argument("--prompt-version", default="", help="v1|v2（默认读 PROMPT_VERSION，再默认 v1）")
    parser.add_argument("--ab", action="store_true", help="A/B 对比 v1 vs v2（准确率 + token 降幅）")
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
    version = args.prompt_version.strip().lower() or os.getenv("PROMPT_VERSION", "v1").strip().lower()
    print(f"评测集: {len(cases)} 条 ｜ provider={provider.name} ｜ 基准日={DATA_BASE_DATE}")

    if args.ab:
        out_ab = Path(args.out) if args.out else OUT_AB_JSON
        v1 = await run(cases, provider, mock_gen, "v1", reuse_tables=False)
        v2 = await run(cases, provider, mock_gen, "v2", reuse_tables=True)
        print("\n" + "=" * 60)
        _print_summary("A/B 对比：v1（全 schema）", v1["summary"])
        _print_summary("A/B 对比：v2（Schema Linking）", v2["summary"])
        s1, s2 = v1["summary"], v2["summary"]
        tok1, tok2 = s1["avg_prompt_tokens"], s2["avg_prompt_tokens"]
        drop = (tok1 - tok2) / tok1 * 100 if tok1 else 0.0
        acc1, acc2 = s1["overall"]["accuracy"], s2["overall"]["accuracy"]
        print("\n== token 对比 ==")
        print(f"  v1 avg={tok1}  v2 avg={tok2}  降幅 {drop:.1f}%  (目标 ≥50%)")
        print(f"  准确率: v1={acc1:.3f}  v2={acc2:.3f}  差值 {acc2 - acc1:+.3f} (目标 ≥ -0.02)")

        # 合并写一份 A/B 报告（v1/v2 各留一份完整记录，供错例与 token 分析）
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ab_payload = {
            "provider": provider.name,
            "base_date": DATA_BASE_DATE.isoformat(),
            "eval_count": len(cases),
            "v1": v1,
            "v2": v2,
            "ab": {
                "avg_prompt_tokens": {"v1": tok1, "v2": tok2},
                "token_drop_pct": round(drop, 1),
                "accuracy": {"v1": acc1, "v2": acc2},
                "accuracy_delta": round(acc2 - acc1, 3),
            },
        }
        out_ab.write_text(json.dumps(ab_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出: {out_ab}")
    else:
        await run(cases, provider, mock_gen, version, reuse_tables=False, out_path=Path(args.out or OUT_JSON))

    await dispose_engine()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
