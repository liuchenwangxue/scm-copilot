"""语义缓存（W22 Day1 + ★ W23 Day5 Redis 化）：语义相似的问题"不再重算"。

为什么（面试 44 题/高并发 素材）：
- 真实业务里用户反复问高度相似的问题（"这个申请还有效吗" / "这个申请单有效期是多久"）。
  每次都走完整 RAG 检索 + LLM 生成 = 重复烧 token、重复等待。
- 语义缓存：key = query embedding；与已缓存历史 query 的相似度 ≥ 阈值（默认 0.92）→
  直接返回缓存答案（省掉检索 + LLM），并打 `source=cache` 标记，命中与未命中可区分。

★ W23 Day5 Redis 化（无状态化核销清单"幂等/缓存/锁 → Redis 共享"落项）：
- 存储分层：**Redis 权威共享 + 进程内存兜底**——双实例下"实例 A 写入、实例 B 命中"
  （Day4 欠账"kb 语义缓存仍内存实现"清零；W24 双实例压测防"缓存视图不一致"误判）
- key 前缀 `scm:semcache:{version}:{query_hash}`（条目）+ `scm:semcache:{version}:keys`（索引 set）
- 命中双闸门（★ 生产级防误命中）：embedding 相似度 ≥ 阈值 **且** 字符 Jaccard 重叠 ≥ 0.40
  （bge 对疑问句式普遍高相似，需防跨主题错配——宁不命中不漏命中）
- fail-open：Redis 不可用 → 内存兜底；都不行 → miss（缓存透明，不影响主链路）

接口（不变）：
    SemanticCache(threshold=None, max_size=None, version=None, embedder=None, redis_client=None)
    .lookup(query) -> dict | None
    .put(query, answer, citations=None) -> None
    .hit_rate() -> dict
    .clear() / .invalidate(new_version=None) -> int
"""
import hashlib
import json
import time
from typing import Any

import numpy as np

from app.shared import config
from app.shared.rag.embedder import Embedder
from app.shared.reliability.redis_client import get_redis_client

# Redis 前缀与条目 TTL（知识库更新 bump version 全量失效；TTL 兜底防无限增长）
_SEMCACHE_PREFIX = "scm:semcache"
_REDIS_TTL = 7 * 24 * 3600  # 7 天
# ★ W27-D6 (B12)：内存条目 TTL 与清扫周期——内存兜底不再无限驻留
#   （原实现内存条目无 TTL，只在 LRU 超 max_size 时淘汰；Redis 侧 7 天淘汰，
#   内存侧与 Redis 的淘汰口径对齐，60s 周期清扫 + 访问时惰性触发）。
_MEM_TTL_SECONDS = 60.0
_SWEEP_INTERVAL_SECONDS = 60.0


def _query_hash(query: str) -> str:
    """文本哈希（Redis key / 去重标识，不用于相似度判定）。"""
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


def _vec_to_list(vec: np.ndarray) -> list[float]:
    return [float(x) for x in vec]


class SemanticCache:
    """语义缓存：Redis 权威共享 + 内存兜底 + 版本失效。"""

    def __init__(self, threshold: float | None = None, max_size: int | None = None,
                 version: str | None = None, embedder: Embedder | None = None,
                 redis_client: Any | None = None):
        self.threshold = threshold if threshold is not None else config.SEMANTIC_CACHE_THRESHOLD
        self.max_size = max_size or config.SEMANTIC_CACHE_MAX_SIZE
        self.version = version or config.SEMANTIC_CACHE_VERSION
        self.embedder = embedder or Embedder()
        self.rc = redis_client or get_redis_client()
        # 内存兜底存储（进程内 LRU + ★ B12 TTL；Redis 命中后回填，加速同实例后续命中）
        self._store: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._stats = {"hits": 0, "misses": 0, "error": 0}
        # ★ B12：上次全量清扫时刻（访问时惰性触发 60s 周期清扫）
        self._last_sweep = time.monotonic()

    # ---- key 管理 ----

    def _ns(self) -> str:
        return f"{_SEMCACHE_PREFIX}:{self.version}"

    def _entry_key(self, query_hash: str) -> str:
        return f"{self._ns()}:{query_hash}"

    def _index_key(self) -> str:
        return f"{self._ns()}:keys"

    # ---- 内存 LRU + TTL（★ W27-D6 B12） ----

    def _touch(self, key: str) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        if len(self._order) > self.max_size:
            old = self._order.pop(0)
            self._store.pop(old, None)
        # 活跃命中刷新 stored_at（LRU 语义：最近用过的条目不过期）
        entry = self._store.get(key)
        if entry is not None:
            entry["stored_at"] = time.monotonic()

    def _sweep_if_due(self) -> None:
        """60s 周期清扫（访问时惰性触发）：物理移除内存中过期的条目。

        无后台线程：在 lookup/put 入口检查距上次清扫是否 ≥60s，到期才全量扫一遍。
        """
        now = time.monotonic()
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        expired = [
            k
            for k, e in self._store.items()
            if now - (e.get("stored_at") or now) > _MEM_TTL_SECONDS
        ]
        for k in expired:
            self._store.pop(k, None)
            if k in self._order:
                self._order.remove(k)

    # ---- 指标埋点（W26 Day1，fail-open） ----

    @staticmethod
    def _inc_metric(kind: str) -> None:
        """把 hit/miss 计数写入 Prometheus（观测旁路，失败静默）。"""
        try:
            from app.shared.obs.metrics import inc_semcache_hit, inc_semcache_miss
            if kind == "hit":
                inc_semcache_hit()
            else:
                inc_semcache_miss()
        except Exception:
            pass

    # ---- 命中搜索（双闸门） ----

    def _search(self, qv: np.ndarray, query: str,
                entries: dict[str, dict[str, Any]]) -> dict | None:
        """在给定条目集中找最相似且双闸门达标者。"""
        best_key, best_sim, best_overlap = None, -1.0, 0.0
        now = time.monotonic()
        for key, entry in entries.items():
            if entry.get("version") != self.version:
                continue  # 版本不符不参与匹配
            # ★ W27-D6 (B12)：内存条目过期即弃（get 时惰性失效，不等周期清扫）；
            #   Redis 条目无 stored_at 字段 → `or now` 折算为 0 永不过期
            if now - (entry.get("stored_at") or now) > _MEM_TTL_SECONDS:
                continue
            # ★ 维度防护：查询向量与条目向量维度不符（跨 embedder/换模型后残留的
            #   污染条目）→ 跳过该条。不加此闸时 np.dot 抛 ValueError 会被 lookup
            #   的 fail-open 吞掉——一条脏数据让整池 Redis 条目全部降级 miss。
            vec = entry.get("vec")
            if not isinstance(vec, list) or len(vec) != qv.shape[0]:
                continue
            try:
                sim = float(np.dot(qv, np.asarray(vec, dtype=np.float32)))
            except (ValueError, TypeError):
                continue  # 条目内容损坏（非数值）同样只跳过，不炸整池
            if sim > best_sim:
                best_sim, best_key = sim, key
        if best_key is not None and best_sim >= self.threshold:
            matched_query = entries[best_key].get("query", "")
            best_overlap = _char_overlap(query, matched_query)
            if best_overlap >= _OVERLAP_MIN:
                self._touch(best_key)
                return {
                    "source": "cache",
                    "query_hash": _query_hash(query),
                    "matched_query": matched_query[:60],
                    "sim": round(best_sim, 4),
                    "char_overlap": round(best_overlap, 4),
                    "answer": entries[best_key]["answer"],
                    "citations": entries[best_key].get("citations") or [],
                    "version": self.version,
                }
        return None

    # ---- Redis 交互 ----

    def _redis_all(self) -> dict[str, dict[str, Any]]:
        """拉取当前版本全部条目（跨实例共享视图）。失败/空 → {}。"""
        if not self.rc.available:
            return {}
        keys = self.rc.smembers(self._index_key())
        if not keys:
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for h in keys:
            raw = self.rc.get(self._entry_key(h))
            if not raw:
                continue
            try:
                entries[h] = json.loads(raw)
            except (ValueError, TypeError):
                continue
        return entries

    def _redis_put(self, query_hash: str, entry: dict[str, Any]) -> None:
        if not self.rc.available:
            return
        try:
            self.rc.set(self._entry_key(query_hash), json.dumps(entry, ensure_ascii=False),
                        ex=_REDIS_TTL)
            self.rc.sadd(self._index_key(), query_hash)
        except Exception:
            pass  # fail-open：Redis 写失败不影响内存兜底

    # ---- 对外接口 ----

    def lookup(self, query: str) -> dict | None:
        """语义查询：内存 → Redis 两级搜索，双闸门达标才命中。

        ★ W26 Day1：命中/未命中同步记录 Prometheus Counter
        （scm_semcache_hit_total / scm_semcache_miss_total）——Grafana
        "语义缓存" 面板命中率曲线数据源。
        """
        self._sweep_if_due()  # ★ W27-D6 (B12)：访问时惰性触发 60s 周期清扫
        if not query.strip():
            self._stats["misses"] += 1
            self._inc_metric("miss")
            return None
        try:
            qv = self.embedder.embed_query(query)
            # 1) 内存快路径（本实例已命中过的条目）
            hit = self._search(qv, query, self._store)
            if hit:
                self._stats["hits"] += 1
                self._inc_metric("hit")
                return hit
            # 2) Redis 跨实例路径（其他实例写入的条目）
            redis_entries = self._redis_all()
            if redis_entries:
                hit = self._search(qv, query, redis_entries)
                if hit:
                    self._stats["hits"] += 1
                    self._inc_metric("hit")
                    # 回填内存（后续同实例命中走快路径；★ B12：从回填时刻起计内存 TTL）
                    now = time.monotonic()
                    for k, e in redis_entries.items():
                        e.setdefault("stored_at", now)
                        self._store.setdefault(k, e)
                    return hit
            self._stats["misses"] += 1
            self._inc_metric("miss")
            return None
        except Exception as e:  # 缓存挂掉/embedding 失败 → fail-open，不阻塞主链路
            self._stats["error"] += 1
            self._inc_metric("miss")  # 异常降级按 miss 计（面板口径：非命中=未命中）
            print(f"[semantic_cache] lookup 异常降级: {type(e).__name__}: {str(e)[:80]}")
            return None

    def put(self, query: str, answer: str, citations: list | None = None) -> None:
        """写入缓存（内存 + Redis 双写）。异常静默降级。"""
        if not query.strip() or not answer:
            return
        try:
            vec = _vec_to_list(self.embedder.embed_query(query))
            query_hash = _query_hash(query)
            entry = {"vec": vec, "answer": answer, "citations": citations or [],
                     "version": self.version, "query": query,
                     "stored_at": time.monotonic()}  # ★ B12：内存条目 TTL 起点
            # 内存
            key = f"{self.version}:{query_hash}"
            self._store[key] = entry
            self._touch(key)
            # Redis（跨实例共享）
            self._redis_put(query_hash, entry)
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
            "redis_available": self.rc.available,
        }

    def clear(self) -> None:
        """清空当前版本缓存（内存 + Redis）。"""
        self._store.clear()
        self._order.clear()
        if self.rc.available:
            keys = self.rc.smembers(self._index_key())
            self.rc.delete_many([self._entry_key(h) for h in keys])
            self.rc.delete(self._index_key())

    def invalidate(self, new_version: str | None = None) -> int:
        """版本失效：bump 版本号并清空旧条目（知识库更新后调用）。返回清空条数。"""
        n = len(self._store)
        self._store.clear()
        self._order.clear()
        # 清理旧版本 Redis 键（当前实例已知的）
        if self.rc.available:
            keys = self.rc.smembers(self._index_key())
            self.rc.delete_many([self._entry_key(h) for h in keys])
            self.rc.delete(self._index_key())
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
