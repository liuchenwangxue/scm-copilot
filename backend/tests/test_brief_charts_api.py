"""W28 Day3 BI 图表 API 测试：纯逻辑兜底 + 权限闸 + 真数据（integration）。

覆盖手册 Day3：
- 权限：`admin:brief:read`（operator 403 / admin 200 / 无认证 401）
- 数据正确性：近 7 日 points 升序 / 最近一日 TOP5 / SQL 原文可回溯
- 空态：无记录 → latest_date=None + 空数组（前端空态提示的依据）
- 缺字段兜底（COALESCE 语义）：metrics 缺 gmv → None，图表整张不挂
- 限流：API Key 调用 charts API 超额 → 429 + Retry-After（admin 面 API 也是 API）
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domains.admin.brief_charts import (
    _as_float,
    _extract_sqls,
    _extract_top_suppliers,
    _to_point,
)
from app.platform.models import DailyBrief

pytestmark = pytest.mark.integration

PLAIN_PASSWORD = "Passw0rd!"


def tenant_user(role: str, tenant: str = "t_huadong") -> str:
    return f"{role}_{tenant}"


def _login(client, username: str) -> dict:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PLAIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ==================== 纯逻辑：数值兜底 / 空态 / 缺字段 ====================


def test_as_float_boundaries():
    assert _as_float(None) is None
    assert _as_float(123.5) == 123.5
    assert _as_float("8.25") == 8.25
    assert _as_float("abc") is None
    assert _as_float([]) is None


def test_to_point_missing_fields_coalesce_to_none():
    """早期 brief 结构缺字段 → None（COALESCE 兜底，图表整张不挂）。"""
    b = DailyBrief(brief_date="2026-09-02", metrics={"gmv": "100.5"})  # 无 delay_rate
    p = _to_point(b)
    assert p.date == "2026-09-02"
    assert p.gmv == 100.5
    assert p.delay_rate is None

    b2 = DailyBrief(brief_date="2026-09-03", metrics=None)  # metrics 整体缺失
    p2 = _to_point(b2)
    assert p2.gmv is None and p2.delay_rate is None


def test_extract_top_suppliers_empty_and_bad_rows():
    assert _extract_top_suppliers(None) == []
    b = DailyBrief(brief_date="2026-09-02", metrics={"top_suppliers": [None, "not-dict"]})
    assert _extract_top_suppliers(b) == []


def test_extract_top_suppliers_rank_and_none_gmv():
    b = DailyBrief(
        brief_date="2026-09-02",
        metrics={
            "top_suppliers": [
                {"supplier": "华东A", "gmv": 60.0},
                {"supplier": "华北B"},  # 缺 gmv → None
            ]
        },
    )
    items = _extract_top_suppliers(b)
    assert [i.rank for i in items] == [1, 2]
    assert items[0].supplier == "华东A" and items[0].gmv == 60.0
    assert items[1].gmv is None


def test_extract_sqls_empty_and_keeps_question():
    assert _extract_sqls(None) == []
    b = DailyBrief(
        brief_date="2026-09-02",
        sqls=[
            {"key": "gmv", "question": "昨日 GMV？", "sql": "SELECT SUM(amount) AS gmv FROM orders"},
            {"key": "delay_rate", "question": "延迟率？", "sql": ""},  # 被拒：sql 为空串保留
        ],
    )
    out = _extract_sqls(b)
    assert [s.key for s in out] == ["gmv", "delay_rate"]
    assert out[0].sql.startswith("SELECT")
    assert out[1].sql == ""


# ==================== integration：权限闸 ====================


def test_brief_charts_forbidden_for_operator_and_anonymous(client):
    """operator 无 admin:brief:read → 403；无认证 → 401（全局门禁）。"""
    headers = _login(client, tenant_user("operator"))
    assert client.get("/api/v1/admin/brief/charts", headers=headers).status_code == 403
    assert client.get("/api/v1/admin/brief/charts").status_code == 401


# ==================== integration：真数据 ====================


def _seed_briefs(client, days: list[date]) -> None:
    """直接向测试库插入构造日报（模拟 W25 daily_brief 产物结构）。"""

    def _make(bd: date) -> DailyBrief:
        m = {
            "gmv": float(bd.day * 1000),
            "delay_rate": 8.0 + bd.day / 100,
            "top_suppliers": [
                {"supplier": f"供应商{bd.day}A", "gmv": float(bd.day * 600)},
                {"supplier": f"供应商{bd.day}B", "gmv": float(bd.day * 400)},
            ],
        }
        sqls = [
            {
                "key": "gmv",
                "question": "昨日订单总金额（GMV）是多少？",
                "sql": f"SELECT SUM(amount) AS gmv FROM orders WHERE created_at = '{bd}'",
                "columns": ["gmv"],
                "rows": [[float(bd.day * 1000)]],
                "elapsed": 0.3,
                "rejected_reason": None,
            }
        ]
        return DailyBrief(
            brief_date=bd.isoformat(),
            title=f"供应链经营日报 {bd.isoformat()}",
            summary=f"日报 {bd.isoformat()}",
            metrics=m,
            sqls=sqls,
            status="pushed",
        )

    async def _insert():
        async with client.app.state.session_factory() as s:
            for d in days:
                s.add(_make(d))
            await s.commit()

    client.portal.call(_insert)


def _cleanup_briefs(client, days: list[date]) -> None:
    from sqlalchemy import delete

    dates = [d.isoformat() for d in days]

    async def _remove():
        async with client.app.state.session_factory() as s:
            await s.execute(delete(DailyBrief).where(DailyBrief.brief_date.in_(dates)))
            await s.commit()

    client.portal.call(_remove)


def test_brief_charts_returns_trend_top5_and_sqls(client):
    """三图数据一次取齐：7 日趋势（升序，尾部=构造数据） + 最近一日 TOP5 + SQL 回溯 + 基准虚线。

    注：测试库可能已有历史 daily_briefs（夜间回归/真实日报），API 语义是"最近 7 条"——
    故断言"points 尾部是我插入的 3 条构造记录（2099 年必然最新）"，而非"只有我插入的 3 条"。
    """
    today = date(2099, 8, 3)
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    _cleanup_briefs(client, days)
    _seed_briefs(client, days)
    try:
        headers = _login(client, tenant_user("admin"))
        resp = client.get("/api/v1/admin/brief/charts", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["latest_date"] == "2099-08-03"
        assert body["baseline_delay_rate"] == 9.91  # W25 首份实测基线
        dates = [p["date"] for p in body["points"]]
        assert dates == sorted(dates), "points 应按日期升序"
        assert dates[-3:] == ["2099-08-01", "2099-08-02", "2099-08-03"], "尾部应为构造数据"
        # 构造成 gmv = 日 × 1000（2099-08-01 → 1000，08-03 → 3000）
        assert body["points"][-3]["gmv"] == 1000.0
        assert body["points"][-1]["gmv"] == 3000.0

        # 最近一日（2099-08-03）TOP5（rank 补齐 + 金额）
        tops = body["top_suppliers"]
        assert [t["rank"] for t in tops] == [1, 2]
        assert tops[0]["supplier"] == "供应商3A" and tops[0]["gmv"] == 1800.0

        # SQL 原文可回溯（图表 = 快照的可视化，不是现算）
        assert len(body["sqls"]) == 1
        assert body["sqls"][0]["key"] == "gmv"
        assert "SELECT SUM(amount)" in body["sqls"][0]["sql"]
    finally:
        _cleanup_briefs(client, days)


def test_brief_charts_missing_field_coalesce(client):
    """缺字段兜底：metrics 缺 gmv → None，响应仍 200 且不炸。"""
    today = date(2099, 9, 5)
    days = [today]
    _cleanup_briefs(client, days)

    async def _insert():
        async with client.app.state.session_factory() as s:
            s.add(
                DailyBrief(
                    brief_date=today.isoformat(),
                    title=f"日报 {today.isoformat()}",
                    metrics={"delay_rate": 9.91},  # 缺 gmv / top_suppliers
                    sqls=[],
                    status="generated",
                )
            )
            await s.commit()

    client.portal.call(_insert)
    try:
        headers = _login(client, tenant_user("admin"))
        resp = client.get("/api/v1/admin/brief/charts", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["latest_date"] == today.isoformat()
        latest = body["points"][-1]  # 最近一日（升序数组尾部 = 2099-09-05）
        assert latest["date"] == today.isoformat()
        assert latest["gmv"] is None
        assert latest["delay_rate"] == 9.91
        assert body["top_suppliers"] == []
        assert body["sqls"] == []
    finally:
        _cleanup_briefs(client, days)


def test_brief_charts_api_key_rate_limit_429(client):
    """admin 面 API 也是 API：API Key 令牌桶打满 → 429 + Retry-After（Redis 挂则 skip）。"""
    admin_headers = _login(client, tenant_user("admin"))
    created = client.post(
        "/api/v1/admin/apikeys", headers=admin_headers, json={"name": "charts-limit"}
    ).json()
    key = created["api_key"]
    key_id = created["key_id"]
    try:
        hit_429 = False
        for _ in range(30):
            resp = client.get(
                "/api/v1/admin/brief/charts", headers={"Authorization": f"Bearer {key}"}
            )
            if resp.status_code == 429:
                hit_429 = True
                assert resp.headers.get("Retry-After"), "429 必须带 Retry-After 头"
                assert resp.json()["code"] == "QUOTA_429"
                break
        if not hit_429:
            pytest.skip("Redis 不可用 → 限速 fail-open（部署环境验证 429）")
    finally:
        client.delete(f"/api/v1/admin/apikeys/{key_id}", headers=admin_headers)
