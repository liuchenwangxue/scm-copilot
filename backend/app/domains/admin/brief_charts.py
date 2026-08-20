"""BI 图表数据 API（★ W28 Day3，C3/B4 项）：经营日报从"数字文本"到"趋势图表"。

数据源：`daily_briefs` 表 metrics/sqls JSON 快照（W25 Day3 起积累）——**图表 = 快照的
可视化，不是新的计算路径**（已固化口径 vs 每次现算的口径漂移风险；面试题，见手册 Day3）。
SQL 原文一并回放 → "数字可回溯"卖点延续到图表层。

设计要点（对照手册 Day3）：
- `GET /api/v1/admin/brief/charts`：近 7 日 GMV 趋势 + 延迟率趋势 + 最近一日 TOP5 供应商
  + 三条模板 SQL 原文，挂 `admin:brief:read`（★ W28 Day3 新增权限码）
- 限流：admin 面 API 也是 API——`rbac.require_permission` 依赖 `api_key_or_jwt`，
  API Key 调用自动过令牌桶（超额 429 + Retry-After），无需额外代码
- 空值兜底（COALESCE 语义）：早期 brief 结构可能缺字段 → `metrics.get()` 返回 None，
  图表整张不挂；无任何记录 → `latest_date=None` + 空数组（前端显示空态提示）
- 延迟率基准虚线：9.91% 为 W25 首份日报实测值（`reports/w25_day3_brief_eval.md`），
  作为趋势图的对比基线（当前值低于/高于基线的可视化判读）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.domains.admin.schemas import (
    BriefChartPoint,
    BriefChartsOut,
    BriefSqlOut,
    TopSupplierItem,
)
from app.platform import rbac
from app.platform.models import DailyBrief, User

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 图表窗口：近 7 日（与手册 Day3"7 日 GMV 趋势"一致）
CHARTS_DAYS = 7
# 延迟率基准（W25 首份日报实测 9.91%，`reports/w25_day3_brief_eval.md`；作对比虚线）
DELAY_RATE_BASELINE = 9.91


@router.get(
    "/brief/charts",
    response_model=BriefChartsOut,
    summary="BI 图表数据（近 7 日经营日报）",
    description=(
        "近 7 日 GMV/延迟率趋势 + 最近一日 TOP5 供应商 + 三条 SQL 原文（可回溯）。"
        "数据来自 daily_briefs 表 metrics 快照（已固化口径，非现算）。需要 admin:brief:read。"
    ),
)
async def get_brief_charts(
    request: Request,
    _: Annotated[User, Depends(rbac.require_permission("admin:brief:read"))],
) -> BriefChartsOut:
    """取近 7 日 daily_briefs → 组装三图数据 + SQL 回溯。"""
    return await _query_charts(request.app.state.session_factory)


async def _query_charts(session_factory) -> BriefChartsOut:
    """从 daily_briefs 表取近 7 条（按日期降序取 7 再反转成时间升序）。"""
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(DailyBrief)
                    .order_by(DailyBrief.brief_date.desc())
                    .limit(CHARTS_DAYS)
                )
            ).all()
        )
    rows = list(reversed(rows))  # 左旧右新（图表时间轴升序）

    latest = rows[-1] if rows else None
    points = [_to_point(r) for r in rows]
    return BriefChartsOut(
        latest_date=latest.brief_date if latest else None,
        points=points,
        top_suppliers=_extract_top_suppliers(latest),
        sqls=_extract_sqls(latest),
        baseline_delay_rate=DELAY_RATE_BASELINE,
    )


def _to_point(brief: DailyBrief) -> BriefChartPoint:
    """单日点：metrics 缺字段（早期结构）→ None（COALESCE 兜底，图表整张不挂）。"""
    metrics = brief.metrics or {}
    return BriefChartPoint(
        date=brief.brief_date,
        gmv=_as_float(metrics.get("gmv")),
        delay_rate=_as_float(metrics.get("delay_rate")),
    )


def _extract_top_suppliers(brief: DailyBrief | None) -> list[TopSupplierItem]:
    """最近一日 TOP5（metrics.top_suppliers 已按金额降序；补 rank 供前端排序）。"""
    if brief is None:
        return []
    items = (brief.metrics or {}).get("top_suppliers") or []
    out: list[TopSupplierItem] = []
    for i, item in enumerate(items[:5], start=1):
        if not isinstance(item, dict):
            continue
        out.append(
            TopSupplierItem(
                rank=i,
                supplier=str(item.get("supplier") or "—"),
                gmv=_as_float(item.get("gmv")),
            )
        )
    return out


def _extract_sqls(brief: DailyBrief | None) -> list[BriefSqlOut]:
    """SQL 回溯：已落库原文（被拒绝的问题也返回，sql 为空串由前端显示占位）。"""
    if brief is None:
        return []
    out: list[BriefSqlOut] = []
    for s in brief.sqls or []:
        if not isinstance(s, dict):
            continue
        out.append(
            BriefSqlOut(
                key=str(s.get("key") or ""),
                question=str(s.get("question") or ""),
                sql=str(s.get("sql") or ""),
            )
        )
    return out


def _as_float(value: Any) -> float | None:
    """数值兜底：None/非数值 → None（前端缺值留空，不炸整张图）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
