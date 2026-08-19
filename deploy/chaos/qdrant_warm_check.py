"""演练三辅助：预热后 BM25-only 降级真实耗时（进程常驻场景）。

说明：每次新进程首次调用 HybridRetriever 会加载 embedding 模型（~15-20s），
生产进程常驻模型只加载一次——本脚本验证"模型已加载"前提下降级耗时。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.shared.rag.hybrid_retriever import HybridRetriever  # noqa: E402


def main() -> None:
    r = HybridRetriever()  # 进程启动加载模型（一次性）
    # 预热：先跑一次（含模型加载）
    _ = r.retrieve("采购申请需要经过哪几级审批", top_k=1)
    print("[warm] 预热完成（模型已加载）")
    # 正式测降级耗时
    t0 = time.time()
    hits = r.retrieve("供应商准入需要提交哪些资质材料", top_k=3)
    elapsed = time.time() - t0
    print(f"[warm] BM25-only 降级查询耗时 {elapsed:.1f}s")
    print(f"[warm] sources={[h.get('source') for h in hits]} degraded={[h.get('degraded') for h in hits]}")
    assert all(h.get("degraded") for h in hits), "降级标记缺失"
    print(f"[warm] PASS: 预热后 BM25-only 降级 {elapsed:.1f}s 返回")


if __name__ == "__main__":
    main()
