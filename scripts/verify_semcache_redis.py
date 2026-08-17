"""★ W23 Day5 验收脚本：语义缓存 Redis 化——双实例共享命中验证（mock embedder）。

场景（手册坑"语义缓存 keys 迁完务必双实例验证命中"）：
- 实例 A 写入缓存（`scm:semcache:{version}:{hash}`）
- 实例 B 全新进程，同一语义问题 lookup → 必须命中（Redis 共享，非本进程内存）

mock embedder：字符 bag-of-words 归一化向量（deterministic）——
相似问共享大多数字符 → 余弦接近 1 且字符重叠达标 → 命中；
无关问 → 相似度低 → 不命中。用于验证 Redis 共享机制，不依赖真实模型（mock-first）。

用法：
  python scripts/verify_semcache_redis.py put <query> <answer>   # 实例 A
  python scripts/verify_semcache_redis.py get <query>            # 实例 B
  python scripts/verify_semcache_redis.py two-phase              # 单进程 put+get 快速自检
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import numpy as np  # noqa: E402

from app.shared.rag.semantic_cache import SemanticCache  # noqa: E402

Q = "采购申请需要经过哪几级审批"
A = "三级审批：需求部门负责人 + 采购经理 + 分管副总。"
SIMILAR = "采购申请需要哪几级审批？"


class MockEmbedder:
    """字符 bag-of-words 归一化向量（deterministic；相似问→高余弦，无关问→低）。"""

    def __init__(self, dim: int = 512):
        self._dim = dim

    def _clean(self, text: str) -> str:
        return re.sub(r"[\s，。？！、；：,.!?;:（）()\"\"''——]", "", text)

    def embed_query(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        for ch in self._clean(text):
            vec[ord(ch) % self._dim] += 1.0
        n = float(np.linalg.norm(vec))
        return vec / n if n else vec


async def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "two-phase"
    cache = SemanticCache(embedder=MockEmbedder())

    if phase == "put":
        cache.clear()
        cache.put(Q, A, citations=[{"doc_id": "SCM-PUR-001"}])
        print(f"[A] 已写入: {Q!r}  redis_available={cache.hit_rate()['redis_available']}")
    elif phase == "get":
        hit = cache.lookup(SIMILAR)  # 全新实例（无本进程内存）→ 只能靠 Redis
        if hit:
            print(f"[B] Redis 跨实例命中! sim={hit['sim']} answer={hit['answer'][:30]}...")
            print("SEMCACHE_OK")
        else:
            print("[B] 未命中（Redis 共享失败）")
            sys.exit(1)
    else:  # two-phase：put → 新实例 → get
        c1 = SemanticCache(embedder=MockEmbedder())
        c1.clear()
        c1.put(Q, A)
        c2 = SemanticCache(embedder=MockEmbedder())  # 独立内存 = "新实例"
        hit = c2.lookup(SIMILAR)
        if hit:
            print(f"[two-phase] 新实例命中: sim={hit['sim']}")
            print("SEMCACHE_OK")
        else:
            print("[two-phase] 未命中")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
