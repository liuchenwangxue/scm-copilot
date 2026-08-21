"""daily_brief：经营日报（工作日 08:00，W25 Day3 实现）。

cron: 0 8 * * 1-5
作用：三条 NL2SQL（昨日 GMV / 延迟发货率 / TOP5 供应商）→ 模板渲染 brief
（含 SQL 链接可回溯）→ 写 daily_briefs 表 + 推送订阅用户（站内通知，3 测试用户）。

设计（对照手册 Day3 上午）：
- **走 W24 NL2SQL 链路**：三个固定模板问题经 `run_nl2sql_query` 完整执行
  （四道闸 + 只读沙箱），mock 模式下先 `register_mock_sql` 注册固定 SQL——
  这样"日报数字来自真实执行结果"而非手填，SQL 100% 可回溯。
- **昨日口径**：SQL 内写死 `CURDATE() - INTERVAL 1 DAY`（手册坑：跨月/跨年让 MySQL
  处理，别 Python 拼日期字符串）；`today` 只用于幂等键与记录归属日。
- **幂等键**：`brief:{date}`（Redis SETNX，TTL 26h 覆盖当日窗口）——重复执行直接跳；
  失败时删除幂等键允许重试（★ 归档任务的教训：失败不能残留永久跳过标记）。
- **推送**：站内通知先落表（非目标清单：不接邮件/IM），订阅用户 = analyst/admin 前 3 名。

返回结构：{brief_date, metrics, sqls, notified, status}
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select

from app.platform.models import DailyBrief, Notification, Role, User, UserRole
from app.shared.reliability.redis_client import get_redis_client

logger = logging.getLogger("scm.scheduler.jobs.daily_brief")

CRON = "0 8 * * 1-5"

# 三条固定模板问题（★ 走 W24 NL2SQL 链路；SQL 为 mock 链路注册的确定性来源，
# real 模式由 LLM 生成——二者在四道闸 + 只读沙箱下执行路径完全一致）
BRIEF_QUESTIONS: dict[str, str] = {
    "gmv": "昨日订单总金额（GMV）是多少？",
    "delay_rate": "昨日延迟发货率是多少？",
    "top_suppliers": "昨日订单金额 TOP5 的供应商是哪些？",
}

# mock 链路注册的固定 SQL（与评测无关，仅让 mock 生成器命中；口径统一 CURDATE()-1）
_BRIEF_SQLS: dict[str, str] = {
    "gmv": (
        "SELECT SUM(amount) AS gmv FROM orders "
        "WHERE created_at >= CURDATE() - INTERVAL 1 DAY AND created_at < CURDATE()"
    ),
    "delay_rate": (
        "SELECT ROUND(SUM(CASE WHEN sh.delay_days > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) "
        "AS delay_rate FROM shipments sh JOIN orders o ON sh.order_no = o.order_no "
        "WHERE o.created_at >= CURDATE() - INTERVAL 1 DAY AND o.created_at < CURDATE()"
    ),
    "top_suppliers": (
        "SELECT s.name AS supplier, SUM(o.amount) AS gmv FROM orders o "
        "JOIN suppliers s ON o.supplier_id = s.id "
        "WHERE o.created_at >= CURDATE() - INTERVAL 1 DAY AND o.created_at < CURDATE() "
        "GROUP BY s.name ORDER BY gmv DESC LIMIT 5"
    ),
}

# 订阅用户数（测试用户 3 名）
SUBSCRIPTION_LIMIT = 3
# 幂等键 TTL：26h 覆盖"当日窗口 + 次日补跑"（跨日仍不重复）
_IDEM_TTL_SECONDS = 26 * 3600


# ==================== 核心流程 ====================


async def run() -> dict:
    """生成当日经营日报（工作日 08:00 触发）。

    - 幂等：`brief:{date}` SETNX 未命中直接跳过；失败删键可重试
    - 三条 NL2SQL → 模板渲染 → daily_briefs 表 → 订阅用户站内通知
    """
    today = date.today()
    brief_date = today.isoformat()
    rc = get_redis_client()

    # ---- 幂等键（第一道保险；数据库 unique 约束是第二道）----
    # ★ 修复：RedisClient.set_nx 在 Redis 不可用时同样返回 False——原实现把
    #   "键已存在"与"Redis 挂了"重载成同一语义，Redis 宕机期间日报被静默
    #   skipped 且误报 already-generated。不可用时直接放行执行，
    #   由 daily_briefs 表 (brief_date unique) 兜底幂等。
    idem_key = f"brief:{brief_date}"
    redis_ok = rc.available
    if redis_ok and not rc.set_nx(idem_key, "1", ex=_IDEM_TTL_SECONDS):
        return {
            "job": "daily_brief",
            "status": "skipped",
            "brief_date": brief_date,
            "reason": "already-generated",
        }

    try:
        result = await _generate_brief(today)
        return result
    except Exception:
        # 失败删除幂等键（允许次日/手动重试），不残留永久跳过标记
        logger.exception("daily_brief failed for %s, releasing idem key", brief_date)
        if redis_ok:
            rc.delete(idem_key)
        raise


async def _generate_brief(today: date) -> dict:
    """执行三条 NL2SQL → 渲染 → 落库 → 推送。"""
    brief_date = today.isoformat()

    # ---- 1) 注册 mock SQL（幂等：重复注册为覆盖）----
    from app.domains.data.mock_sql import register_mock_sql

    for key, question in BRIEF_QUESTIONS.items():
        register_mock_sql(question, _BRIEF_SQLS[key])

    # ---- 2) 逐条走 NL2SQL 完整链路（四道闸 + 只读沙箱，SQL 可回溯）----
    from app.domains.data.service import run_nl2sql_query

    metrics: dict[str, Any] = {}
    sqls: list[dict[str, Any]] = []
    for key, question in BRIEF_QUESTIONS.items():
        res = await run_nl2sql_query(question, today=brief_date, with_insights=False)
        sqls.append(
            {
                "key": key,
                "question": question,
                "sql": res.get("sql") or "",
                "columns": res.get("columns", []),
                "rows": res.get("rows", []),
                "elapsed": res.get("elapsed", 0.0),
                "rejected_reason": res.get("rejected_reason"),
            }
        )
        metrics[key] = _extract_metric(key, res)
        logger.info("brief[%s] %s -> %s", brief_date, key, json.dumps(metrics[key], ensure_ascii=False)[:120])

    # ---- 3) 模板渲染（含 SQL 可回溯，数字点开可见）----
    title = f"供应链经营日报 {brief_date}"
    summary = _render_brief(brief_date, metrics, sqls)

    # ---- 4) 落 daily_briefs（brief_date unique；已存在则跳过——幂等双保险）----
    from app.platform.scheduler import _runtime

    session_factory = _runtime.session_factory  # ★ W27-D6 B10：RuntimeContext 字段
    if session_factory is None:
        raise RuntimeError("scheduler runtime not initialized (session_factory is None)")

    existing_id: int | None = None
    async with session_factory() as session:
        existing = await session.scalar(
            select(DailyBrief.id).where(DailyBrief.brief_date == brief_date)
        )
        if existing is not None:
            existing_id = existing
        else:
            session.add(
                DailyBrief(
                    brief_date=brief_date,
                    title=title,
                    summary=summary,
                    metrics=metrics,
                    sqls=sqls,
                    status="generated",
                )
            )
            await session.commit()
            existing_id = (await session.scalar(
                select(DailyBrief.id).where(DailyBrief.brief_date == brief_date)
            ))

    # ---- 5) 订阅推送（analyst/admin 前 3 名 → 站内通知）----
    notified = await _push_to_subscribers(brief_date, title, summary, metrics)

    # 推送成功后标记 status=pushed
    async with session_factory() as session:
        brief = await session.get(DailyBrief, existing_id)
        if brief is not None:
            brief.status = "pushed"
            brief.notified_users = notified
            await session.commit()

    return {
        "job": "daily_brief",
        "status": "success",
        "brief_date": brief_date,
        "metrics": metrics,
        "sqls": sqls,
        "notified": notified,
    }


# ==================== 指标提取 ====================


def _extract_metric(key: str, res: dict[str, Any]) -> Any:
    """从 NL2SQL 结果中提取日报关键数字。

    - gmv：单值（SUM）→ 数字；结果空 → None
    - delay_rate：单值百分比 → 数字
    - top_suppliers：多行 → [{supplier, gmv}]（限 TOP5）
    """
    if res.get("rejected_reason") or not res.get("rows"):
        return None
    rows = res.get("rows", [])
    columns = res.get("columns", [])
    if key in ("gmv", "delay_rate"):
        try:
            return float(rows[0][0])
        except (TypeError, ValueError, IndexError):
            return None
    if key == "top_suppliers":
        idx_supplier = columns.index("supplier") if "supplier" in columns else 0
        idx_gmv = columns.index("gmv") if "gmv" in columns else -1
        items = []
        for row in rows[:5]:
            try:
                items.append(
                    {
                        "supplier": str(row[idx_supplier]),
                        "gmv": float(row[idx_gmv]) if idx_gmv >= 0 else None,
                    }
                )
            except (TypeError, ValueError, IndexError):
                continue
        return items
    return None


# ==================== 模板渲染 ====================


def _render_brief(brief_date: str, metrics: dict[str, Any], sqls: list[dict[str, Any]]) -> str:
    """Markdown 模板渲染：每个数字带对应 SQL 原文（可回溯，点开可见）。"""
    lines = [f"# 供应链经营日报 {brief_date}", ""]

    def _fmt(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, (int, float)):
            return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
        return str(value)

    gmv = metrics.get("gmv")
    delay = metrics.get("delay_rate")
    lines.append("## 核心指标")
    lines.append("")
    lines.append(f"- **昨日 GMV**：{_fmt(gmv)} 元" + ("" if gmv is not None else "（无数据）"))
    lines.append(f"- **昨日延迟发货率**：{_fmt(delay)}%" + ("" if delay is not None else "（无数据）"))
    lines.append("")
    lines.append("## TOP5 供应商（按昨日订单金额）")
    lines.append("")
    tops = metrics.get("top_suppliers") or []
    if tops:
        lines.append("| 排名 | 供应商 | 金额(元) |")
        lines.append("|---|---|---|")
        for i, t in enumerate(tops, 1):
            lines.append(f"| {i} | {t.get('supplier', '—')} | {_fmt(t.get('gmv'))} |")
    else:
        lines.append("（无数据）")
    lines.append("")
    lines.append("## SQL 回溯（数字可验证）")
    lines.append("")
    for s in sqls:
        key = s.get("key", "")
        lines.append(f"### {key}: {s.get('question', '')}")
        if s.get("rejected_reason"):
            lines.append(f"> 被安全闸拒绝：{s.get('rejected_reason')}")
        elif s.get("sql"):
            lines.append(f"```sql\n{s['sql']}\n```")
        else:
            lines.append("> 无 SQL")
        lines.append("")
    return "\n".join(lines)


# ==================== 订阅推送 ====================


async def _push_to_subscribers(
    brief_date: str, title: str, summary: str, metrics: dict[str, Any]
) -> list[str]:
    """向订阅用户推送日报（站内通知表；analyst/admin 角色前 3 名）。

    幂等：按 (user_id, type, title) 查重，已推送不重复写（锁失效双实例兜底）。
    """
    from app.platform.scheduler import _runtime

    session_factory = _runtime.session_factory  # ★ W27-D6 B10：RuntimeContext 字段
    if session_factory is None:
        return []

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(User.id, User.username)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code.in_(["analyst", "admin"]))
                .order_by(User.id)
                .limit(SUBSCRIPTION_LIMIT)
            )
        ).all()

    notified: list[str] = []
    async with session_factory() as session:
        for user_id, username in rows:
            dup = await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id,
                    Notification.type == "daily_brief",
                    Notification.title == title,
                )
            )
            if dup:
                continue
            session.add(
                Notification(
                    user_id=user_id,
                    type="daily_brief",
                    title=title,
                    content=summary[:500],
                    link="/api/v1/admin/scheduler/jobs/daily_brief",
                )
            )
            notified.append(str(username))
        if notified:
            await session.commit()
    return notified
