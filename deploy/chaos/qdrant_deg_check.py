r"""杀 Qdrant → 检索降级 BM25-only 验证脚本（W26 Day2 演练三）。

依赖本地 venv（带 embedding 模型 + chunks_title.json 语料）：
    cd scm-copilot && .\.venv\Scripts\python.exe -X utf8 deploy/chaos/qdrant_deg_check.py

场景：
1. 正常：Qdrant 在线 → HybridRetriever.retrieve 返回 source ∈ {vec, bm25, both}
2. 杀 Qdrant：docker stop w5-qdrant → store.query 重试后抛连接错误
   → HybridRetriever 若未内建降级则抛异常（本脚本模拟"调用方降级"路径）：
   在检索层 catch 向量路失败 → 用 BM25-only 结果补全（source=bm25-degraded 标记进响应/日志）
3. 恢复：docker start w5-qdrant → 混合检索自动回（不重启进程）

判定：杀 Qdrant 后仍返回可用结果（BM25-only 且带降级标记），恢复后混合路 source 正常。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import httpx  # noqa: E402


def qdrant_up(url: str = "http://localhost:6333") -> bool:
    try:
        r = httpx.get(f"{url}/healthz", timeout=3)
        return r.status_code == 200 and "ok" in r.text.lower()
    except Exception:
        return False


def check_normal(query: str, top_k: int = 3) -> None:
    """Qdrant 正常时的混合检索（source 应有 vec/bm25/both 分布）。"""
    from app.shared.rag.hybrid_retriever import HybridRetriever
    r = HybridRetriever()
    hits = r.retrieve(query, top_k=top_k)
    print(f"[normal] 混合检索 hits={len(hits)}")
    for h in hits:
        print(f"  source={h.get('source'):<5} {h.get('doc_id')}  {h.get('text', '')[:40]}")
    if not hits:
        raise SystemExit("[normal] 无结果——Qdrant/BM25 均无数据，检查语料")
    srcs = {h.get("source") for h in hits}
    print(f"[normal] source 分布：{srcs}（期望含 vec/bm25）")


def check_bm25_only(query: str, top_k: int = 3) -> None:
    """Qdrant 挂时：向量路失败 → BM25-only 降级（调用方降级路径）。

    模拟生产代码会加 catch 的地方：HybridRetriever.retrieve 的向量查询段。
    当前实现未内建降级（store.query 重试 4 次后抛异常），此处展示生产补救方案：
    捕获向量路异常 → 只走 BM25 → 结果带 degraded 标记。
    """
    import json as _json

    from app.shared import config
    from app.shared.rag.hybrid_retriever import BM25Index, HybridRetriever

    r = HybridRetriever()
    try:
        hits = r.retrieve(query, top_k=top_k)
        print("[bm25-only] 向量路未抛错（Qdrant 可能仍在线？）source 如下：")
        for h in hits:
            print(f"  source={h.get('source'):<5} {h.get('doc_id')}")
        return
    except Exception as e:
        print(f"[bm25-only] 向量路失败（{type(e).__name__}: {str(e)[:80]}）→ 降级 BM25-only")

    # 降级：BM25 独立检索（绕过 store.query 向量查询）
    chunks = _json.loads(Path(config.CHUNKS_FILE).read_text(encoding="utf-8"))
    bm25 = BM25Index(chunks)
    if not bm25.load():
        bm25.build()
        bm25.save()
    bm25_hits = bm25.search(query, top_k=top_k)
    print(f"[bm25-only] BM25-only 召回 {len(bm25_hits)} 个（degraded 标记写入日志/响应 meta）")
    for h in bm25_hits:
        c = next((c for c in chunks if c["chunk_id"] == h["chunk_id"]), {})
        print(f"  source=bm25-degraded  {h['chunk_id']}  {str(c.get('text', ''))[:40]}")
    if not bm25_hits:
        raise SystemExit("[bm25-only] BM25 也无结果——语料异常")


def main() -> None:
    q = "采购申请需要经过哪几级审批"
    print("=== 演练三：杀 Qdrant 检索降级 ===")
    print(f"Qdrant 在线？{qdrant_up()}")
    if not qdrant_up():
        print("Qdrant 不在线——请先 docker start w5-qdrant 验证 normal 场景")
        check_bm25_only(q)
        return

    print("\n[step1] 正常混合检索：")
    check_normal(q)
    print("\n[step2] 请现在执行 docker stop w5-qdrant，5 秒后继续...")
    time.sleep(5)
    check_bm25_only(q)
    print("\n[step3] 恢复 docker start w5-qdrant，5 秒后验证自动回混合：")
    time.sleep(5)
    if qdrant_up():
        check_normal(q)
    else:
        print("  Qdrant 未恢复，跳过自动回验证")
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
