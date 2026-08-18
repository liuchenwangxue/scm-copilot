"""多轮指代消解 + NL2SQL 全链路评测脚本（W24 Day5）。

对应《W24学习执行手册》Day5 下午验收：**多轮 10 条过 8**（每条对话的所有追问轮执行准确率全对才算过）。

评测口径：
- 每轮：指代消解（有上下文时）→ 消解后问题进 NL2SQL 图 → 四道闸 → 只读沙箱执行
  → 与 gold 结果集比对（execution accuracy，复用 eval_nl2sql 的 `_results_equal`）；
- 指标：
    case_pass        每条对话（含首轮与全部追问轮）执行准确率全对 = 通过
    exec_accuracy    全部轮次的 execution accuracy（分层：首轮 / 追问轮）
    resolution_accuracy  消解文本与标注 resolved 一致的轮次占比（消解单独评测）
- mock / real 双路径（手册坑"mock 测链路、real 测效果"）：
    mock：`register_mock_sql` 预注册每轮问题 → gold SQL（确定性测链路）；消解走规则 _mock_resolve；
    real：消解走 LLM prompt，SQL 生成走模型池（真实效果，以 real 数字为准）。

用法：
  python -X utf8 backend/scripts/eval_multiturn.py            # mock（测链路）
  LLM_PROVIDER=real python -X utf8 backend/scripts/eval_multiturn.py  # real（测效果）

输出：控制台指标 + reports/nl2sql_multiturn_day5.json（默认 mock）/ ..._real_day5.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# scripts/ 运行：把 backend 与 scripts 加入 import path（与 eval_nl2sql.py 同策略）
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.domains.data.executor import dispose_engine, execute_sql  # noqa: E402
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.mock_sql import clear_mock_sql_registry, register_mock_sql  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.session_ctx import SessionContext  # noqa: E402
from app.shared.llm import get_provider  # noqa: E402

EVAL_FILE = BACKEND / "evals" / "multiturn_eval.jsonl"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"
OUT_JSON = OUT_DIR / "nl2sql_multiturn_day5.json"

# 复用 eval_nl2sql 的结果集比对（同口径，避免两份实现漂移）
from eval_nl2sql import _results_equal  # noqa: E402


async def run_case(
    turns: list[dict],
    provider: Any,
    today: str,
) -> dict[str, Any]:
    """跑一条多轮对话：逐轮 消解→入图→执行比对；首轮直接入图。"""
    ctx = SessionContext("eval-case")
    turn_records: list[dict[str, Any]] = []

    for idx, turn in enumerate(turns):
        q = turn["q"]
        expected_resolved = turn["resolved"]
        gold_sql = turn["gold_sql"]

        resolved = await ctx.resolve(q, today)  # 首轮无上下文 → 原样
        resolution_ok = resolved == expected_resolved

        state = await data_graph.ainvoke({"question": resolved, "today": today})
        res = state.get("result") or {}
        sql = res.get("sql") or ""
        columns = res.get("columns", [])

        ok = False
        diffs: list[Any] = []
        if columns and not state.get("error") and not state.get("rejected_reason"):
            gold_res = await execute_sql(gold_sql)
            ok, diffs = _results_equal(gold_res, res)
        turn_records.append(
            {
                "turn": idx,
                "q": q,
                "resolved": resolved,
                "expected_resolved": expected_resolved,
                "resolution_ok": resolution_ok,
                "ok": ok,
                "sql": sql,
                "diffs": diffs[:2],
                "rejected_reason": state.get("rejected_reason"),
                "error": state.get("error"),
                "repair_attempts": state.get("repair_attempts", 0),
            }
        )

        if columns:
            from app.domains.data.schema_linker import linker

            ctx.record(resolved, sql, linker.link_prompt_tables(resolved))

    return {
        "pass": all(t["ok"] for t in turn_records),
        "turns": turn_records,
    }


def _summarize(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    n_cases = len(case_results)
    n_pass = sum(1 for c in case_results if c["pass"])
    all_turns = [t for c in case_results for t in c["turns"]]
    firsts = [t for t in all_turns if t["turn"] == 0]
    follows = [t for t in all_turns if t["turn"] > 0]

    def _acc(ts: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(ts),
            "correct": sum(1 for t in ts if t["ok"]),
            "accuracy": round(sum(1 for t in ts if t["ok"]) / max(len(ts), 1), 3),
            "resolution_ok": round(
                sum(1 for t in ts if t["resolution_ok"]) / max(len(ts), 1), 3
            ),
        }

    return {
        "cases": n_cases,
        "case_pass": n_pass,
        "case_pass_rate": round(n_pass / max(n_cases, 1), 3),
        "overall": _acc(all_turns),
        "first_turn": _acc(firsts),
        "followup": _acc(follows),
    }


def _print_summary(s: dict[str, Any]) -> None:
    print("\n== 多轮评测（gate: 10 条过 8）==")
    print(f"  对话通过率: {s['case_pass']}/{s['cases']} = {s['case_pass_rate']:.3f}")
    print(f"  轮次整体  : {s['overall']['accuracy']:.3f} ({s['overall']['correct']}/{s['overall']['total']})")
    print(f"  首轮      : {s['first_turn']['accuracy']:.3f} ({s['first_turn']['correct']}/{s['first_turn']['total']})")
    print(f"  追问轮    : {s['followup']['accuracy']:.3f} ({s['followup']['correct']}/{s['followup']['total']})")
    print(f"  消解一致率: {s['overall']['resolution_ok']:.3f}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="多轮指代消解 + NL2SQL 评测")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.eval_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    provider = get_provider()
    today = DATA_BASE_DATE.isoformat()
    print(f"多轮评测集: {len(cases)} 条对话 ｜ provider={provider.name} ｜ 基准日={today}")

    # mock 链路：预注册每轮问题 → gold SQL（消解后问题文本 = 标注 resolved，确定性命中）
    if provider.name == "mock":
        clear_mock_sql_registry()
        for case in cases:
            for turn in case["turns"]:
                register_mock_sql(turn["resolved"], turn["gold_sql"])

    try:
        results = [await run_case(case["turns"], provider, today) for case in cases]
    finally:
        if provider.name == "mock":
            clear_mock_sql_registry()

    for i, r in enumerate(results, 1):
        mark = "✓" if r["pass"] else "✗"
        n_turns = len(r["turns"])
        ok_n = sum(1 for t in r["turns"] if t["ok"])
        res_n = sum(1 for t in r["turns"] if t["resolution_ok"])
        print(f"  [{i:>2}] {mark} 轮 {n_turns}（对 {ok_n}）消解一致 {res_n}  "
              f"{r['turns'][0]['q'][:24]}")

    summary = _summarize(results)
    _print_summary(summary)

    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"nl2sql_multiturn_{'real_' if provider.name == 'real' else ''}day5.json"
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"provider": provider.name, "base_date": today, "summary": summary, "cases": results},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写出: {out_path}")

    await dispose_engine()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
