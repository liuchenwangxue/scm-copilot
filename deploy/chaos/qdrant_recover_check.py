"""演练三辅助：Qdrant 恢复后混合检索自动回（无需重启进程）。

预期：恢复后 retrieve 的 source 包含 vec / bm25 / both（混合路回来了），
且 degraded 标记消失（不降级了）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.shared.rag.hybrid_retriever import HybridRetriever  # noqa: E402


def main() -> None:
    r = HybridRetriever()
    hits = r.retrieve("采购申请需要经过哪几级审批", top_k=5)
    print(f"[recover] hits={len(hits)}")
    for h in hits:
        print(f"[recover] source={h.get('source'):<5} degraded={h.get('degraded')} {h.get('doc_id')}")
    sources = {h.get("source") for h in hits}
    degraded = any(h.get("degraded") for h in hits)
    has_mixed = bool(sources & {"vec", "both"})
    print(f"[recover] sources={sources} degraded={degraded} has_mixed={has_mixed}")
    assert has_mixed, "混合检索未自动回（应出现 vec/both source）"
    assert not degraded, "恢复后仍带 degraded 标记"
    print("[recover] PASS: Qdrant 恢复 -> 混合检索自动回（无需重启）")


if __name__ == "__main__":
    main()
