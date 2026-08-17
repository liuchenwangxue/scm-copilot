"""语义缓存（W22 Day1）：语义相似的问题"不再重算"。

为什么（面试 44 题/高并发 素材）：
- 真实业务里用户反复问高度相似的问题（"这个申请还有效吗" / "这个申请单有效期是多久"）。
  每次都走完整 RAG 检索 + LLM 生成 = 重复烧 token、重复等待。
- 语义缓存：key = query embedding；与已缓存历史 query 的相似度 ≥ 阈值（默认 0.92）→
  直接返回缓存答案（省掉检索 + LLM），并打 `source=cache` 标记，命中与未命中可区分。

设计要点（对应手册坑）：
- 高阈值（宁不命中不漏命中）：错误命中返回错答案，比不命中更糟（手册原话）。
- 缓存内容带版本（SEMANTIC_CACHE_VERSION）：知识库更新后 bump 版本号 → 全部失效（W21 定时任务衔接）。
- 缓存 = 最终回答 + citations（项目 A）；命中返回完整可溯源结果。
- 降级（fail-open）：缓存仅是最速通道，挂掉/无命中都不阻塞主链路（try/except 包裹）。
- 内存实现（进程级 LRU 上限）为默认；生产可换 Redis（W21 已有真实 Redis，此处保留接口注释说明）。

接口：
    SemanticCache(threshold=None, max_size=None, version=None, embedder=None)
    .lookup(query) -> dict | None          # 命中返回 {"source":"cache", "answer", "citations", "query_hash", "sim"}
    .put(query, answer, citations=None)    # 写入缓存（带 query embedding + 版本）
    .hit_rate() -> dict                     # 统计 {hits, misses, rate}
    .clear() / .invalidate()               # 清空 / 版本失效
"""
import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from app.shared import config
from app.shared.rag.embedder import Embedder


def _query_hash(query: str) -> str:
    """文本哈希（用于日志/去重 key 的可读标识，不用于相似度判定）。"""
    return hashlib.md5(query.encode("utf-8")).hexdigest()[:12]


# 字符重叠下限（防跨主题误命中的第二道闸门）：表述需足够接近才命中
_OVERLAP_MIN = 0.40


def _char_overlap(a: str, b: str) -> float:
    """字符级 Jaccard 重叠（去空白/标点）。

    为什么（★ 生产级防误命中闸门）：bge 类 embedding 对"疑问句式"普遍高相似——
    不同主题的"…如何…？"问题也可能相似度 >0.92。若只凭 embedding 阈值命中，
    会把"主题不同但句式相同"的问题错配到缓存答案（比不命中更糟，手册原话）。
    故在 embedding 相似度之上叠加"字符重叠"第二道闸门：只有语义相似 **且** 表述
    足够接近才命中，彻底落实"宁不命中不漏命中"。
    """
    import re
    ka = set(re.sub(r"[\s，。？！、；：,.!?;:（）()\"\"''——]", "", a))
    kb = set(re.sub(r"[\s，。？！、；：,.!?;:（）()\"\"''——]", "", b))
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


class SemanticCache:
    """语义缓存：embedding 相似度命中 + 版本失效 + 内存 LRU。"""

    def __init__(self, threshold: float | None = None, max_size: int | None = None,
                 version: str | None = None, embedder: Embedder | None = None):
        self.threshold = threshold if threshold is not None else config.SEMANTIC_CACHE_THRESHOLD
        self.max_size = max_size or config.SEMANTIC_CACHE_MAX_SIZE
        self.version = version or config.SEMANTIC_CACHE_VERSION
        self.embedder = embedder or Embedder()
        # 缓存条目：query -> {"vec", "answer", "citations", "version"}
        self._store: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []          # 简单 LRU 顺序（命中/写入移动到末尾）
        self._stats = {"hits": 0, "misses": 0, "error": 0}

    def _touch(self, key: str) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        if len(self._order) > self.max_size:
            old = self._order.pop(0)
            self._store.pop(old, None)

    def lookup(self, query: str) -> dict | None:
        """语义查询：与缓存库中所有 query 的 embedding 算相似度，≥ 阈值且字符重叠达标才命中。
        返回 None = 未命中（走主链路）；返回 dict = 命中（source=cache）。

        命中双闸门（★ 生产级防误命中）：
          1. embedding 相似度 ≥ threshold（语义相近）
          2. 字符 Jaccard 重叠 ≥ _OVERLAP_MIN（表述足够接近——bge 对疑问句高相似，需防跨主题错配）
        """
        if not self._store or not query.strip():
            self._stats["misses"] += 1
            return None
        try:
            qv = self.embedder.embed_query(query)
            best_key, best_sim, best_overlap = None, -1.0, 0.0
            for key, entry in self._store.items():
                if entry.get("version") != self.version:
                    continue  # 版本不符的缓存不参与匹配
                sim = float(np.dot(qv, entry["vec"]))
                if sim > best_sim:
                    best_sim, best_key = sim, key
            # 双闸门：相似度达标 + 与匹配项字符重叠达标
            if best_key is not None and best_sim >= self.threshold:
                matched_query = self._store[best_key].get("query", "")
                best_overlap = _char_overlap(query, matched_query)
                if best_overlap >= _OVERLAP_MIN:
                    self._stats["hits"] += 1
                    self._touch(best_key)
                    return {
                        "source": "cache",
                        "query_hash": _query_hash(query),
                        "matched_query": matched_query[:60],
                        "sim": round(best_sim, 4),
                        "char_overlap": round(best_overlap, 4),
                        "answer": self._store[best_key]["answer"],
                        "citations": self._store[best_key].get("citations") or [],
                        "version": self.version,
                    }
            self._stats["misses"] += 1
            return None
        except Exception as e:  # 缓存挂掉/embedding 失败 → fail-open，不阻塞主链路
            self._stats["error"] += 1
            print(f"[semantic_cache] lookup 异常降级: {type(e).__name__}: {str(e)[:80]}")
            return None

    def put(self, query: str, answer: str, citations: list | None = None) -> None:
        """写入缓存（带 query embedding + 版本）。异常静默降级（缓存失败不影响主链路）。"""
        if not query.strip() or not answer:
            return
        try:
            vec = self.embedder.embed_query(query)
            key = f"{self.version}:{_query_hash(query)}"
            self._store[key] = {"vec": vec, "answer": answer,
                                "citations": citations or [],
                                "version": self.version, "query": query}
            self._touch(key)
        except Exception as e:
            print(f"[semantic_cache] put 异常降级: {type(e).__name__}: {str(e)[:80]}")

    def hit_rate(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "errors": self._stats["error"],
            "rate": round(self._stats["hits"] / total, 4) if total else 0.0,
            "size": len(self._store),
            "threshold": self.threshold,
            "version": self.version,
        }

    def clear(self) -> None:
        self._store.clear()
        self._order.clear()

    def invalidate(self, new_version: str | None = None) -> int:
        """版本失效：bump 版本号并清空旧条目（知识库更新后调用）。返回清空条数。"""
        n = len(self._store)
        self._store.clear()
        self._order.clear()
        if new_version:
            self.version = new_version
        return n


if __name__ == "__main__":
    # 自检：写入 2 条 → 相似问命中 / 无关问未命中 / hit_rate
    c = SemanticCache()
    c.put("采购申请需要经过哪几级审批", "三级审批：需求部门负责人+采购经理+分管副总。",
          citations=[{"doc_id": "SCM-PUR-001"}])
    c.put("供应商准入要提交哪些材料", "营业执照、质量体系认证、财务报表等。",
          citations=[{"doc_id": "SCM-SUP-001"}])
    for q in ["采购申请要哪几级审批？", "今天天气怎么样？", "供应商准入需要什么资质"]:
        hit = c.lookup(q)
        print(f"  {q!r:24} -> {hit if hit else '(miss)'}")
    print(f"  hit_rate: {c.hit_rate()}")
