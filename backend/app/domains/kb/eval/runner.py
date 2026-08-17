"""评测流水线：逐条 QA → 检索 → mock 生成（引用）→ 指标 → 分类下钻 → 失败样例。

供 scripts/day6_eval.py 调用，W18 优化后复用同一流水线跑 Δ。
"""
import asyncio
import time

from app.domains.kb.eval.metrics import (
    aggregate_metrics,
    citation_accuracy,
    evaluate_retrieval,
)
from app.shared.llm import get_provider
from app.shared.rag.retriever import Retriever


class EvalRunner:
    def __init__(self, top_k: int = 5, provider_name: str | None = None,
                 retriever=None, use_validator: bool = False):
        """retriever: 可注入任意实现同接口 (retrieve/retrieve_top_docs) 的检索器
        （纯向量 Retriever / HybridRetriever）——A/B 评测用同一流水线，只换注入点。
        use_validator: True 时走 generate_with_validation（规则+LLM 双校验 + 缺失回退，Day4）。"""
        self.top_k = top_k
        self.retriever = retriever or Retriever()
        self.provider = get_provider(provider_name)
        self.use_validator = use_validator

    async def run_qa(self, qa: dict) -> dict:
        """单条 QA 评测，返回指标 + 耗时。"""
        golden = set(qa["source_doc_ids"]) or {"<EMPTY_GOLDEN>"}
        t0 = time.time()
        hits = self.retriever.retrieve(qa["question"], top_k=self.top_k)
        retrieve_ms = (time.time() - t0) * 1000

        top_docs = []
        seen = set()
        top_text_parts = []
        for h in hits:
            if h["doc_id"] not in seen:
                seen.add(h["doc_id"])
                top_docs.append(h["doc_id"])
            top_text_parts.append(h["text"])

        top_text = " ".join(top_text_parts)
        retr = evaluate_retrieval(top_docs, golden, top_text, qa["answer"])

        ctx = [{"doc_id": h["doc_id"], "section_path": h.get("section_path", ""), "text": h["text"]}
               for h in hits]

        # ★ 引用准确率：真实 LLM 生成（generate_json），可选 validator 双校验
        t1 = time.time()
        if self.use_validator and self.provider.name != "mock":
            from app.domains.kb.agent.answer_validator import generate_with_validation
            result = await generate_with_validation(
                self.provider, qa["question"], ctx, golden_docs=sorted(golden),
                qa_type=qa.get("type"))
            citations = result.get("citations", [])
            validation = result.get("validation", {})
            degraded = result.get("degraded", False)
            retries = result.get("retries", 0)
        else:
            result = await self.provider.generate_json(
                [{"role": "user", "content": qa["question"]}],
                {"type": "object"},
                retrieval_context=ctx,
            )
            result = result if isinstance(result, dict) else {}
            citations = result.get("citations", [])
            if not isinstance(citations, list):
                citations = []
            # 短名 → 全名归一化（与 validator 路径一致，保证引用准确率口径统一）
            from app.domains.kb.agent.answer_validator import normalize_citations
            citations = normalize_citations(citations, {h["doc_id"] for h in ctx})
            validation, degraded, retries = {}, False, 0
        gen_ms = (time.time() - t1) * 1000

        citation_acc = citation_accuracy(citations, golden)

        return {
            **retr,
            "citation_acc": citation_acc,
            "retrieve_ms": round(retrieve_ms, 2),
            "gen_ms": round(gen_ms, 2),
            "top_docs": top_docs,
            "golden": sorted(golden),
            "citations": citations,
            "validation": validation,
            "degraded": int(degraded),
            "retries": retries,
        }

    async def run_all(self, qa_set: list[dict], concurrency: int = 4) -> dict:
        """全量评测：总体指标 + 分类下钻 + 失败样例 + 耗时统计。

        concurrency: 并发度（省时优化——推理模型每条耗时长，串行太慢；
        信号量限流避免并发过高触发限流/额度错误）。"""
        import asyncio as _asyncio

        per_item = []
        cat_stats: dict[str, list[dict]] = {}
        sem = _asyncio.Semaphore(concurrency)

        async def _run_one(qa: dict) -> dict:
            async with sem:
                r = await self.run_qa(qa)
                r["id"] = qa["id"]
                r["category"] = qa.get("category", "其他")
                r["type"] = qa.get("type", "single")
                return r

        results = await _asyncio.gather(*[_run_one(qa) for qa in qa_set])
        for r in results:
            per_item.append(r)
            cat_stats.setdefault(r["category"], []).append(r)

        # 总体
        metrics = aggregate_metrics(per_item, len(qa_set))

        # 分类下钻
        metrics_by_category = {
            cat: aggregate_metrics(items, len(items))
            for cat, items in sorted(cat_stats.items())
        }

        # 失败样例：Top-1 未命中 或 引用未全部命中
        misses = [
            {"id": r["id"], "category": r["category"], "type": r["type"],
             "question": next((q["question"] for q in qa_set if q["id"] == r["id"]), ""),
             "golden": r["golden"], "top1": r["top_docs"][0] if r["top_docs"] else None,
             "top_docs": r["top_docs"], "citations": r["citations"],
             "citation_acc": r["citation_acc"]}
            for r in per_item if not r["hit@1"] or r["citation_acc"] < 1.0
        ]

        # 校验统计（Day4 validator 工作量）
        validator_stats = {
            "degraded_count": sum(1 for r in per_item if r.get("degraded")),
            "retries_total": sum(r.get("retries", 0) for r in per_item),
            "retry_distribution": {
                str(k): sum(1 for r in per_item if r.get("retries", 0) == k)
                for k in sorted({r.get("retries", 0) for r in per_item})
            },
        }

        # 耗时统计
        r_ms = sorted(r["retrieve_ms"] for r in per_item)
        g_ms = sorted(r["gen_ms"] for r in per_item)
        n = len(per_item)

        def _pct(arr, p):
            return round(arr[min(int(n * p) - 1, n - 1)], 2) if arr else 0.0

        timing = {
            "retrieve_p50_ms": _pct(r_ms, 0.5),
            "retrieve_p95_ms": _pct(r_ms, 0.95),
            "gen_p50_ms": _pct(g_ms, 0.5),
            "gen_p95_ms": _pct(g_ms, 0.95),
            "total_p95_ms": round(_pct([a + b for a, b in zip(r_ms, g_ms, strict=False)], 0.95), 2),
        }

        return {
            "metrics": metrics,
            "metrics_by_category": metrics_by_category,
            "miss_samples": misses[:20],
            "timing": timing,
            "validator_stats": validator_stats,
            "per_item": per_item,  # 供报告深挖（degraded/retries/validation）
        }
