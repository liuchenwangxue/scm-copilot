"""mock vs real 对比评测（★ W18 Day5，阶段三最硬核的一张报告）。

方法论红线（W3）：对比必须**同一评测集、同一指标**，否则不可比。
实现：同一批 QA，分别用 mock 与 real 跑"裸链路"（各 1 次生成、不加校验——
校验的影响 Day4 已单独记录），输出三维对比：

- 质量：引用准确率 / 含答案率（mock 虚高是预期的——它直接引检索 Top-N；real 才是真相）
- 延迟：P50 / P95（检索 / 生成 / 总），真实 LLM 首次请求含冷启动
- 成本：每请求 token 实测（usage）+ ¥ 估算（单价可注入，报告标注日期）
- 失败率：超时/限流/异常占比（real 侧关降级，失败如实暴露，不掩盖）

成本数据源：RealLLMProvider._log_cost 追加写 reports/cost_usage.jsonl；
day5_compare.py 跑 real 前截断该文件，跑完后解析（mock 不产生 usage）。

用法：
    CompareRunner(qa_set, retriever).run_side("mock", provider)   # 单侧
    sample_qa(qa_set, 30)                                          # 分层抽样
"""
import asyncio
import time

from app.domains.kb.eval.metrics import aggregate_metrics


def _pct(sorted_arr: list[float], p: float) -> float:
    """百分位（sorted_arr 升序）。空数组返回 0.0。"""
    n = len(sorted_arr)
    return round(sorted_arr[min(int(n * p) - 1, n - 1)], 2) if n else 0.0


def sample_qa(qa_set: list[dict], n: int = 30) -> list[dict]:
    """按 type（single/cross/conflict）分层抽样，保证每类代表性（确定性、可复现）。

    同类型内取前若干条（保持原顺序），四舍五入差量用剩余样本补齐。
    """
    n = max(1, n)
    type_order = ["single", "cross", "conflict"]
    groups: dict[str, list[dict]] = {}
    for t in type_order:
        groups[t] = [q for q in qa_set if q.get("type", "single") == t]
    total = len(qa_set)
    picked: list[dict] = []
    picked_ids: set[str] = set()

    def _take(qs: list[dict], k: int):
        for q in qs:
            if len(picked) >= n:
                break
            if q["id"] not in picked_ids:
                picked.append(q)
                picked_ids.add(q["id"])
            if len(picked) >= k:
                break

    for t in type_order:
        k = round(n * len(groups[t]) / total)
        _take(groups[t], k)

    # 四舍五入差量补齐（按原顺序扫一遍剩余的）
    if len(picked) < n:
        for q in qa_set:
            if len(picked) >= n:
                break
            if q["id"] not in picked_ids:
                picked.append(q)
                picked_ids.add(q["id"])
    return picked[:n]


class CompareRunner:
    """同一批 QA 分别跑 mock/real，产出可对比的指标 + 耗时 + 失败清单。

    与 EvalRunner 的差别：逐条 try/except（单条失败不中断整批，失败计入失败率），
    且固定 use_validator=False（裸链路对比，校验影响 Day4 已单独记录）。
    """

    def __init__(self, qa_set: list[dict], retriever=None, concurrency: int = 4):
        self.qa_set = qa_set
        self.retriever = retriever
        self.concurrency = max(1, concurrency)

    def _make_runner(self, provider):
        """复用 EvalRunner（同检索/同指标），只换 provider 注入点。"""
        from app.domains.kb.eval.runner import EvalRunner
        runner = EvalRunner(top_k=5, retriever=self.retriever)
        runner.provider = provider
        runner.use_validator = False
        return runner

    async def run_side(self, side_name: str, provider) -> dict:
        """跑一侧（mock 或 real），返回指标/耗时/失败清单/per_item。"""
        runner = self._make_runner(provider)
        per_item: list[dict] = []
        failures: list[dict] = []
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(qa: dict):
            async with sem:
                t0 = time.time()
                try:
                    r = await runner.run_qa(qa)
                    r["id"] = qa["id"]
                    r["category"] = qa.get("category", "其他")
                    r["type"] = qa.get("type", "single")
                    r["question"] = qa["question"]
                    r["wall_ms"] = round((time.time() - t0) * 1000, 2)
                    per_item.append(r)
                except Exception as e:  # noqa: BLE001 —— 单条失败记录为失败，不中断整批
                    golden = sorted(set(qa.get("source_doc_ids", [])) or {"<EMPTY_GOLDEN>"})
                    per_item.append({
                        "id": qa["id"], "category": qa.get("category", "其他"),
                        "type": qa.get("type", "single"), "question": qa["question"],
                        "hit@1": 0, "recall@5": 0, "answer_rate": 0, "citation_acc": 0.0,
                        "retrieve_ms": 0.0, "gen_ms": 0.0,
                        "wall_ms": round((time.time() - t0) * 1000, 2),
                        "top_docs": [], "golden": golden, "citations": [],
                        "error": f"{type(e).__name__}: {str(e)[:100]}",
                    })
                    failures.append({"id": qa["id"], "error": str(e)[:160]})

        await asyncio.gather(*[_one(q) for q in self.qa_set])

        metrics = aggregate_metrics(per_item, len(self.qa_set))

        # 耗时百分位（失败条不计入延迟统计——它没产出）
        ok = [r for r in per_item if not r.get("error")]
        g_ms = sorted(r["gen_ms"] for r in ok)
        r_ms = sorted(r["retrieve_ms"] for r in ok)
        t_ms = sorted(r["wall_ms"] for r in ok)
        timing = {
            "retrieve_p50_ms": _pct(r_ms, 0.5),
            "retrieve_p95_ms": _pct(r_ms, 0.95),
            "gen_p50_ms": _pct(g_ms, 0.5),
            "gen_p95_ms": _pct(g_ms, 0.95),
            "total_p95_ms": _pct(t_ms, 0.95),
        }

        return {
            "side": side_name,
            "n": len(self.qa_set),
            "n_ok": len(ok),
            "metrics": metrics,
            "timing": timing,
            "failures": failures,
            "fail_rate": round(len(failures) / max(len(self.qa_set), 1), 4),
            "per_item": per_item,
        }


def aggregate_cost(cost_file, n_qa: int) -> dict:
    """解析 cost_usage.jsonl（跑 real 前已截断，文件里全是本次 real 调用）。

    返回：总量 + 按模型分解 + 每请求均值。mock 无 usage（成本=0）。
    """
    import json
    from pathlib import Path

    path = Path(cost_file)
    total = {"count": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "reasoning_tokens": 0, "total_tokens": 0}
    by_model: dict[str, dict] = {}
    if not path.exists():
        return {"total": total, "by_model": by_model, "per_request": {}}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        total["count"] += 1
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            total[k] += int(e.get(k, 0) or 0)
        m = e.get("model", "?")
        b = by_model.setdefault(m, {"count": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                    "reasoning_tokens": 0, "total_tokens": 0})
        b["count"] += 1
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            b[k] += int(e.get(k, 0) or 0)

    if total["count"]:
        per_request = {
            "prompt_tokens": round(total["prompt_tokens"] / total["count"], 1),
            "completion_tokens": round(total["completion_tokens"] / total["count"], 1),
            "total_tokens": round(total["total_tokens"] / total["count"], 1),
        }
    else:
        per_request = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {"total": total, "by_model": by_model, "per_request": per_request}


def estimate_cost(cost: dict, input_price: float, output_price: float) -> dict:
    """按单价（¥/百万 token）估算总成本。reasoning_tokens 计入 completion 计费。"""
    t = cost.get("total", {})
    prompt = t.get("prompt_tokens", 0)
    completion = t.get("completion_tokens", 0)
    total_yuan = prompt / 1e6 * input_price + completion / 1e6 * output_price
    n = max(t.get("count", 0), 1)
    return {
        "input_price_per_m": input_price,
        "output_price_per_m": output_price,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost_total_yuan": round(total_yuan, 4),
        "cost_per_request_yuan": round(total_yuan / n, 6),
    }


def build_compare(mock_result: dict, real_result: dict, cost: dict,
                  cost_estimate: dict, qa_type_dist: dict) -> dict:
    """汇总三维对比 dict（供 day5_compare.py 写报告）。"""
    rows = {}
    for side, r in [("mock", mock_result), ("real", real_result)]:
        m = r["metrics"]
        rows[side] = {
            "citation_accuracy": m["citation_accuracy"],
            "answer_rate": m["answer_rate"],
            "hit@1": m["hit@1"],
            "gen_p50_ms": r["timing"]["gen_p50_ms"],
            "gen_p95_ms": r["timing"]["gen_p95_ms"],
            "retrieve_p50_ms": r["timing"]["retrieve_p50_ms"],
            "total_p95_ms": r["timing"]["total_p95_ms"],
            "n_ok": r["n_ok"],
            "fail_count": len(r["failures"]),
            "fail_rate": r["fail_rate"],
        }
    return {
        "mock": rows["mock"],
        "real": rows["real"],
        "cost": cost,
        "cost_estimate": cost_estimate,
        "qa_type_dist": qa_type_dist,
        "mock_per_item": mock_result.get("per_item", []),
        "real_per_item": real_result.get("per_item", []),
        "mock_failures": mock_result.get("failures", []),
        "real_failures": real_result.get("failures", []),
    }
