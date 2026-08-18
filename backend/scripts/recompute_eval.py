"""★ 评测重算脚本（W24 Day6 调试工具）——复用评测报告中的 gen SQL，重算 execution accuracy。

用途：当评测比对逻辑修复后（如排序键改整行），无需重新跑 LLM 即可用已有报告里的
gen_sql 重新执行并比对——省 token、快速验证修复效果；并把修复后的分层指标 + P95
写回正式报告（Day6 全量指标的权威来源）。

用法：python -X utf8 backend/scripts/recompute_eval.py reports/nl2sql_eval_day6_real_v2.json
     --out reports/nl2sql_eval_day6_real_v2.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_nl2sql import _results_equal  # noqa: E402

from app.domains.data.executor import dispose_engine, execute_sql  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="评测重算（复用 gen SQL，不重跑 LLM）")
    parser.add_argument("report", nargs="?", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    report = Path(args.report) if args.report else Path(
        r"f:\code\agent\learning-outputs\scm-copilot\reports\nl2sql_eval_day6_real_v2.json"
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    records = data["records"]

    async def _run() -> None:
        by_layer: dict[str, list] = {}
        latencies: list[float] = []
        for r in records:
            try:
                gold_res = await execute_sql(r["gold_sql"])
            except Exception as exc:  # noqa: BLE001
                gold_res = {"columns": [], "rows": [], "error": str(exc)}
            try:
                gen_res = await execute_sql(r["gen_sql"])
            except Exception as exc:  # noqa: BLE001
                gen_res = {"columns": [], "rows": [], "error": str(exc)}
            ok, diffs = _results_equal(gold_res, gen_res)
            r["correct_recomputed"] = ok
            r["diffs_recomputed"] = diffs[:2]
            r["elapsed_recomputed_ms"] = round(gen_res.get("elapsed_ms", 0.0), 1)
            latencies.append(r["elapsed_recomputed_ms"])
            by_layer.setdefault(r["layer"], []).append(r)

        def _acc(rs: list) -> dict:
            n = len(rs)
            ok = sum(1 for r in rs if r["correct_recomputed"])
            return {"total": n, "correct": ok, "accuracy": round(ok / n, 3) if n else 0.0}

        summary = {
            "provider": data.get("provider", "real"),
            "prompt_version": data.get("prompt_version", "v2"),
            "base_date": data.get("base_date"),
            "recomputed": True,
            "overall": _acc(records),
            "single": _acc(by_layer.get("single", [])),
            "join": _acc(by_layer.get("join", [])),
            "aggregation": _acc(by_layer.get("aggregation", [])),
            "avg_prompt_tokens": round(
                sum(r.get("prompt_tokens", 0) for r in records) / max(len(records), 1), 1
            ),
            "p95_elapsed_ms": round(statistics.quantiles(latencies, n=20)[18], 1),
            "max_elapsed_ms": round(max(latencies), 1),
        }
        data["summary"] = summary
        data["errors"] = [r for r in records if not r["correct_recomputed"]]

        print("== 重算结果（排序键修复后）==")
        print(f"  provider={summary['provider']} v{summary['prompt_version']} "
              f"base_date={summary['base_date']}")
        for layer in ("single", "join", "aggregation"):
            s = summary[layer]
            print(f"  {layer:12s}: {s['accuracy']:.3f} ({s['correct']}/{s['total']})")
        s = summary["overall"]
        print(f"  整体: {s['accuracy']:.3f} ({s['correct']}/{s['total']})")
        print(f"  P95 elapsed: {summary['p95_elapsed_ms']}ms "
              f"(max {summary['max_elapsed_ms']}ms)")
        print(f"  avg prompt tokens: {summary['avg_prompt_tokens']}")
        print("\n== 仍错例 ==")
        for r in data["errors"]:
            print(f"  [{r['id']}] {r['layer']:11s} {r['question'][:36]}")
            print(f"      gold: {r['gold_sql'][:100]}")
            print(f"      gen : {r['gen_sql'][:100]}")

        out_path = Path(args.out) if args.out else report
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写回: {out_path}")
        await dispose_engine()

    import asyncio

    asyncio.run(_run())


if __name__ == "__main__":
    main()
