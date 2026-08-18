"""eval_nightly：夜间质量回归（每日 02:00，W25 Day3 实现）。

cron: 0 2 * * *
作用：RAG 156 条 + NL2SQL 100 条评测集全量回归（mock，断言结构）→ 报告落
eval_reports 表（各域分数 + 与 7 日均值偏离，劣化 >5pp 标红）。

设计（对照手册 Day3 上午）：
- **全 mock 断言结构**：RAG 走 `EvalRunner`（生产同款 HybridRetriever 检索链，
  mock 生成引用），NL2SQL 走 `eval_nl2sql` 的评测逻辑——守护的是
  "结果格式 / 延迟 / 报错率"，不测语义准确率（那是 W24 real 全量评测的活）。
- **幂等（结果快照）**：以数据库为准——(report_date, domain) 已存在则跳过该域；
  Redis 挂掉也不影响幂等（不依赖 Redis 幂等键）。
- **7 日均值偏离**：读 eval_reports 近 7 天同域记录求主指标均值，
  delta_pp = (今日 - 均值) × 100；劣化 >5pp → regressed=1（标红）。
- 逐条容错：单条评测异常不中断整轮，报错率进 metrics（链路坏了要有数字）。

返回结构：{rag, nl2sql, deviation, status}
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.platform.models import EvalReport

logger = logging.getLogger("scm.scheduler.jobs.eval_nightly")

CRON = "0 2 * * *"

# 评测集目录：backend/evals/
_EVAL_DIR = Path(__file__).resolve().parents[4] / "evals"
_RAG_EVAL_FILE = _EVAL_DIR / "rag_eval_v2.json"  # 156 条（stage3 评测集过滤 feedback 后落库）
_NL2SQL_EVAL_FILE = _EVAL_DIR / "nl2sql_eval_v1.jsonl"  # 100 条三层

# 劣化标红阈值（手册：>5pp 标红）
DEGRADE_PP = 5.0
# 7 日均值窗口
BASELINE_DAYS = 7


# ==================== 核心流程 ====================


async def run() -> dict:
    """夜间全量回归：RAG 156 + NL2SQL 100（mock）→ eval_reports 落库 + 偏离标红。"""
    today = date.today().isoformat()

    rag_metrics, rag_dev = None, None
    nl2sql_metrics, nl2sql_dev = None, None

    rag_metrics = await _eval_rag(today)
    nl2sql_metrics = await _eval_nl2sql(today)

    rag_dev, nl2sql_dev = await _compute_and_store(today, rag_metrics, nl2sql_metrics)

    return {
        "job": "eval_nightly",
        "status": "success",
        "report_date": today,
        "rag": {"metrics": rag_metrics, "deviation": rag_dev},
        "nl2sql": {"metrics": nl2sql_metrics, "deviation": nl2sql_dev},
    }


# ==================== 各域评测 ====================


async def _eval_rag(today: str) -> dict[str, Any]:
    """RAG 156 条 mock 回归：生产同款混合检索链，逐条容错统计报错率。"""
    if not _RAG_EVAL_FILE.exists():
        logger.error("rag eval file missing: %s", _RAG_EVAL_FILE)
        return {"error": "eval file missing", "n": 0, "error_rate": 1.0}

    cases = json.loads(_RAG_EVAL_FILE.read_text(encoding="utf-8"))
    if not cases:
        return {"error": "empty eval set", "n": 0, "error_rate": 1.0}

    from app.domains.kb.eval.metrics import aggregate_metrics
    from app.domains.kb.eval.runner import EvalRunner
    from app.shared.rag.hybrid_retriever import HybridRetriever
    from app.shared.rag.reranker import get_reranker

    try:
        # 与 kb/router.py 生产检索链一致（懒加载：任务运行时才建 BM25/模型）
        retriever = HybridRetriever(reranker=get_reranker())
        runner = EvalRunner(top_k=5, retriever=retriever, provider_name="mock")
    except Exception as exc:  # noqa: BLE001  # 检索链不可用（Qdrant/BM25 缺失）→ 报告标红不中断
        logger.exception("rag eval retriever init failed: %s", exc)
        return {"error": f"retriever init failed: {exc}", "n": len(cases), "error_rate": 1.0}

    results: list[dict[str, Any]] = []
    errors = 0
    retrieve_ms: list[float] = []
    for qa in cases:
        try:
            r = await runner.run_qa(qa)
            r["id"] = qa.get("id")
            r["category"] = qa.get("category", "其他")
            results.append(r)
            retrieve_ms.append(r.get("retrieve_ms", 0.0))
        except Exception as exc:  # noqa: BLE001  # 单条失败不断整轮，报错率进指标
            errors += 1
            logger.warning("rag eval item failed: id=%s err=%s", qa.get("id"), exc)

    metrics: dict[str, Any] = (
        aggregate_metrics(results, len(cases)) if results else {"hit@1": 0.0, "recall@5": 0.0, "n": 0}
    )
    metrics["error_rate"] = round(errors / len(cases), 4)
    metrics["p95_retrieve_ms"] = _pct(retrieve_ms, 0.95) if retrieve_ms else 0.0
    metrics["count"] = len(cases)
    metrics["errors"] = errors
    return metrics


async def _eval_nl2sql(today: str) -> dict[str, Any]:
    """NL2SQL 100 条 mock 回归：复用 W24 评测逻辑（生成→四道闸→执行→比对）。"""
    if not _NL2SQL_EVAL_FILE.exists():
        logger.error("nl2sql eval file missing: %s", _NL2SQL_EVAL_FILE)
        return {"error": "eval file missing", "n": 0, "error_rate": 1.0}

    cases = [
        json.loads(line)
        for line in _NL2SQL_EVAL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        return {"error": "empty eval set", "n": 0, "error_rate": 1.0}

    from app.domains.data.mock_sql import MockSQLGenerator
    from app.domains.data.prompts import DATA_BASE_DATE
    from app.shared.llm import get_provider

    # scripts/ 下的评测脚本以模块导入（不复制逻辑；脚本自身会 sys.path 兜底 backend）
    scripts_dir = Path(__file__).resolve().parents[4] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import eval_nl2sql as nl2sql_eval  # noqa: PLC0415

    provider = get_provider()  # mock（守护任务全 mock，不断言语义准确率）
    mock_gen = MockSQLGenerator(_NL2SQL_EVAL_FILE)

    try:
        payload = await nl2sql_eval.run(
            cases, provider, mock_gen, prompt_version="v1", reuse_tables=False, out_path=None
        )
    except Exception as exc:  # noqa: BLE001  # 整轮失败也要留下数字
        logger.exception("nl2sql eval failed: %s", exc)
        return {"error": str(exc)[:200], "n": len(cases), "error_rate": 1.0}

    summary = payload.get("summary") or {}
    overall = summary.get("overall") or {}
    return {
        "count": summary.get("total", len(cases)),
        "overall": overall.get("accuracy", 0.0),
        "single": (summary.get("single") or {}).get("accuracy", 0.0),
        "join": (summary.get("join") or {}).get("accuracy", 0.0),
        "aggregation": (summary.get("aggregation") or {}).get("accuracy", 0.0),
        "rejected": overall.get("rejected", 0),
        "exec_error": overall.get("exec_error", 0),
        "elapsed_s": summary.get("elapsed_s", 0.0),
        "avg_prompt_tokens": summary.get("avg_prompt_tokens", 0),
        "error_rate": 0.0,
        "base_date": DATA_BASE_DATE.isoformat(),
    }


# ==================== 落库 + 偏离 ====================


async def _compute_and_store(
    today: str,
    rag_metrics: dict[str, Any] | None,
    nl2sql_metrics: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """计算 7 日均值偏离并落库（(report_date, domain) 已存在则跳过——幂等）。"""
    from app.platform.scheduler import _runtime

    session_factory = _runtime["session_factory"]
    if session_factory is None:
        raise RuntimeError("scheduler runtime not initialized (session_factory is None)")

    rag_dev = await _store_domain(session_factory, today, "rag", rag_metrics)
    nl2sql_dev = await _store_domain(session_factory, today, "nl2sql", nl2sql_metrics)
    return rag_dev, nl2sql_dev


async def _store_domain(
    session_factory, today: str, domain: str, metrics: dict[str, Any] | None
) -> dict[str, Any] | None:
    """单域落库：已存在跳过（幂等）；否则算偏离 + 写 eval_reports。"""
    if metrics is None:
        return None
    async with session_factory() as session:
        existing = await session.scalar(
            select(EvalReport.id).where(
                EvalReport.report_date == today, EvalReport.domain == domain
            )
        )
        if existing is not None:
            return {"skipped": True}

        deviation = await _baseline_deviation(session, domain, today, metrics)
        regressed = 1 if (deviation or {}).get("degraded") else 0
        session.add(
            EvalReport(
                report_date=today,
                domain=domain,
                metrics=metrics,
                deviation=deviation,
                regressed=regressed,
            )
        )
        await session.commit()
        return deviation


async def _baseline_deviation(session, domain: str, today: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """与近 7 日均值偏离：主指标 (rag=hit@1 / nl2sql=overall) delta_pp，劣化 >5pp 标红。"""
    key = "hit@1" if domain == "rag" else "overall"
    today_score = metrics.get(key)
    if today_score is None:
        return {"degraded": False, "reason": "missing-today-score"}

    rows = (
        await session.execute(
            select(EvalReport.metrics)
            .where(EvalReport.domain == domain, EvalReport.report_date < today)
            .order_by(EvalReport.report_date.desc())
            .limit(BASELINE_DAYS)
        )
    ).all()
    scores: list[float] = []
    for row in rows:
        m = row[0]
        if not isinstance(m, dict):
            continue
        v = m.get(key)
        if v is None:
            continue
        scores.append(float(v))
    if not scores:
        return {"degraded": False, "samples": 0, "vs_7d_avg": None, "delta_pp": None}

    avg = sum(scores) / len(scores)
    delta_pp = round((today_score - avg) * 100, 2)
    degraded = delta_pp < -DEGRADE_PP
    return {
        "vs_7d_avg": round(avg, 4),
        "delta_pp": delta_pp,
        "degraded": degraded,
        "samples": len(scores),
    }


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return round(s[idx], 2)
