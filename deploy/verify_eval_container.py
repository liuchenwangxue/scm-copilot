"""★ W28-D1 容器口径统一（C1）：容器内 RAG 156 条评测（真实 bge embedding + bge reranker）。

在 backend 容器内运行：python /app/verify_eval_container.py
与 eval_nightly 同链路（HybridRetriever(reranker=get_reranker()) + mock provider），
输出 hit@1/recall@5/citation_accuracy + 检索耗时——用于"容器内外评测对照表"。
"""
import asyncio
import json
import time
from pathlib import Path

from app.domains.kb.eval.metrics import aggregate_metrics
from app.domains.kb.eval.runner import EvalRunner
from app.shared.rag.hybrid_retriever import HybridRetriever
from app.shared.rag.reranker import get_reranker


async def main() -> None:
    eval_file = Path("/app/backend/evals/rag_eval_v2.json")
    cases = json.loads(eval_file.read_text(encoding="utf-8"))
    print("cases:", len(cases))
    retriever = HybridRetriever(reranker=get_reranker())
    runner = EvalRunner(top_k=5, retriever=retriever, provider_name="mock")
    t0 = time.time()
    results = []
    for qa in cases:
        r = await runner.run_qa(qa)
        results.append(r)
    dt = time.time() - t0
    m = aggregate_metrics(results, len(cases))
    print("elapsed_s:", round(dt, 1))
    for k in ("n", "hit@1", "recall@5", "citation_accuracy"):
        print(k, m.get(k))
    rms = sorted(r["retrieve_ms"] for r in results)
    n = len(rms)
    print("retrieve_p50_ms:", round(rms[n // 2], 2))
    print("retrieve_p95_ms:", round(rms[min(int(n * 0.95), n - 1)], 2))


if __name__ == "__main__":
    asyncio.run(main())
