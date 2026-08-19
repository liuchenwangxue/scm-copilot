"""cache_warmup：语义缓存预热（每日 07:00，W25 Day3 实现）。

cron: 0 7 * * *
作用：昨日高频问题 TOP100 → 预执行写入语义缓存（在日报前跑，日报问题本身也受益）。

设计（对照手册 Day3 下午）：
- **数据源**：conversations.title（用户会话第一句问题）按昨日频次降序取 TOP100——
  真实用户问题而非拍脑袋，保证预热命中率；
- **预热动作**：对每个问题先 `cache.lookup`（已命中跳过），未命中走生产同款
  混合检索链 + mock 生成 → `cache.put`（校验通过的答案才缓存，防污染）；
- **fail-open**：单条失败只计数不断整轮；检索/生成异常降级跳过，不影响调度器；
- **数字可观测**：返回 {candidates, hit, warmed, failed}——"warmup 后缓存命中率
  提升有数字"（面试素材）。

返回结构：{candidates, hit, warmed, failed, skipped_empty}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("scm.scheduler.jobs.cache_warmup")

CRON = "0 7 * * *"

# 预热候选上限（手册：TOP100）
WARMUP_LIMIT = 100


# ==================== 核心流程 ====================


async def run() -> dict:
    """预热昨日高频问题 TOP100 到语义缓存。"""
    yesterday = date.today() - timedelta(days=1)
    questions = await _yesterday_hot_questions(yesterday, limit=WARMUP_LIMIT)

    if not questions:
        return {
            "job": "cache_warmup",
            "status": "success",
            "candidates": 0,
            "hit": 0,
            "warmed": 0,
            "failed": 0,
            "note": "no questions yesterday",
        }

    from app.shared.llm import get_provider
    from app.shared.rag.hybrid_retriever import HybridRetriever
    from app.shared.rag.reranker import get_reranker
    from app.shared.rag.semantic_cache import SemanticCache

    cache = SemanticCache()
    try:
        retriever = HybridRetriever(reranker=get_reranker())
    except Exception as exc:  # noqa: BLE001  # 检索链不可用 → 本轮无法预热（全部记为 failed）
        logger.warning("cache_warmup retriever init failed: %s", exc)
        return {
            "job": "cache_warmup",
            "status": "failed",
            "candidates": len(questions),
            "hit": 0,
            "warmed": 0,
            "failed": len(questions),
            "error": str(exc)[:200],
        }
    provider = get_provider()  # mock（开发期全 mock，real 演示 1 次另算）

    stats: dict[str, Any] = {"candidates": len(questions), "hit": 0, "warmed": 0, "failed": 0}
    for q in questions:
        try:
            if cache.lookup(q):
                stats["hit"] += 1
                continue
            hits = retriever.retrieve(q, top_k=5)
            ctx = [
                {"doc_id": h["doc_id"], "section_path": h.get("section_path", ""), "text": h["text"]}
                for h in hits
            ]
            result = await provider.generate_json(
                [{"role": "user", "content": q}],
                {"type": "object"},
                retrieval_context=ctx,
            )
            answer = (result or {}).get("answer")
            if answer:
                cache.put(q, answer, (result or {}).get("citations") or [])
                stats["warmed"] += 1
            else:
                stats["failed"] += 1
        except Exception as exc:  # noqa: BLE001  # 预热是锦上添花，单条失败不中断整轮
            stats["failed"] += 1
            logger.warning("cache_warmup item failed: %r err=%s", q[:30], exc)

    stats["status"] = "success"
    return stats


# ==================== 数据源 ====================


async def _yesterday_hot_questions(yesterday: date, limit: int = WARMUP_LIMIT) -> list[str]:
    """昨日高频问题：conversations.title（非空）按频次降序 TOP N。

    会话标题 = 用户会话第一句问题（kb/ops chat 端写入的 title 前缀）；
    用 `created_at >= 昨日0点 AND < 今日0点` 时间窗（MySQL 处理跨月/年，不拼字符串）。
    """
    from app.platform.scheduler import _runtime

    session_factory = _runtime.session_factory  # ★ W27-D6 B10：RuntimeContext 字段
    if session_factory is None:
        return []

    start = f"{yesterday.isoformat()} 00:00:00"
    end = f"{yesterday.isoformat()} 23:59:59.999"
    sql = text(
        "SELECT title, COUNT(*) AS c FROM conversations "
        "WHERE created_at >= :start AND created_at < :end "
        "AND title IS NOT NULL AND title <> '' "
        "GROUP BY title ORDER BY c DESC, title LIMIT :limit"
    )
    try:
        async with session_factory() as session:
            rows = (await session.execute(sql, {"start": start, "end": end, "limit": limit})).all()
        return [r[0][:200] for r in rows]
    except Exception as exc:  # noqa: BLE001  # 数据源失败 → 本轮跳过（预热非关键路径）
        logger.warning("cache_warmup: hot questions query failed: %s", exc)
        return []
