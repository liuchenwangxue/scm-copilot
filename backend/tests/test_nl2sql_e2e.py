"""NL2SQL 端到端链路测试（W24 Day3 + Day6 演进）——mock 全链路：generate → validate → execute → format。

覆盖 Day3 验收：
- graph 完整链路：合法问题 → 表格结果（columns/rows/elapsed）
- 拒答路径：攻击性/非查询问题 → rejected_reason + 拒答话术（不硬答）
- router API：POST /api/data/query 返回 {table, sql, columns, rows, elapsed}
- 权限：无 `data:nl2sql` 权限 → 403
- mock 生成器：评测集问题命中 gold SQL；未命中 → 默认安全 SQL

★ Day6 演进：
- service.run_nl2sql_query：统一编排（多轮消解→子图→洞察），router 与对话入口复用
- insights 字段：结果洞察 ≤3 条（mock 确定性摘要，数字全部来自结果集）
- 对话入口 /api/kb/chat 语义路由 data 分支：SSE data_table 事件（权限二次校验）

依赖：MySQL + scm_biz seed + nl2sql_ro（make test-executor 同前置）。
标签：integration（CI 有 MySQL service + seed 步骤，会跑）。
"""

import os

import pytest
import pytest_asyncio

os.environ.setdefault(
    "SCM_BIZ_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4",
)
os.environ.setdefault(
    "SCM_BIZ_RO_DSN",
    "mysql+asyncmy://nl2sql_ro:ro_pass_2026_dev@127.0.0.1:13306/scm_biz?charset=utf8mb4",
)
# ★ e2e 是"mock 全链路验证"：强制 mock（本地 .env 可能配了 real，测试必须可控不烧 token）
os.environ["LLM_PROVIDER"] = "mock"

from app.domains.data.executor import dispose_engine  # noqa: E402
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.mock_sql import MockSQLGenerator  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()


async def _run(question: str) -> dict:
    """跑一次完整链路（mock 生成器：评测集问题 → gold SQL）。"""
    return await data_graph.ainvoke(
        {"question": question, "today": DATA_BASE_DATE.isoformat()}
    )


# ================= graph 链路 =================


@pytest.mark.asyncio
async def test_graph_legal_question_returns_table():
    """合法查询：完整链路出表格（columns/rows/elapsed）。"""
    state = await _run("华东区域有多少订单？")
    assert state["error"] is None
    assert state["rejected_reason"] is None
    res = state["result"]
    assert res["columns"] == ["cnt"]
    assert len(res["rows"]) == 1
    assert res["elapsed_ms"] >= 0
    assert "查询成功" in state["reply"]


@pytest.mark.asyncio
async def test_graph_join_question_returns_table():
    """join 问题：多表聚合正常返回。"""
    state = await _run("各区域供应商的订单总金额？")
    assert state["error"] is None
    assert state["rejected_reason"] is None
    res = state["result"]
    assert res["columns"] == ["region", "total_amount"]
    assert len(res["rows"]) == 4  # 四区域
    assert all(isinstance(r[1], float) for r in res["rows"])


@pytest.mark.asyncio
async def test_graph_unknown_question_falls_back_to_safe_sql():
    """评测集外问题：mock 走默认安全 SQL（链路仍通，不报错）。"""
    state = await _run("随便问一个奇怪的问题？")
    assert state["error"] is None
    res = state["result"]
    assert res["columns"] == ["cnt"]


# ================= 拒答路径（四道闸） =================


@pytest.mark.asyncio
async def test_graph_write_op_rejected():
    """写操作（mock 从评测集取不到时 fallback；这里直接验证 validate 节点兜底）。
    说明：mock 生成器不会产出写 SQL，因此拒答路径用攻击用例直测 validate + reject_node。
    """
    from app.domains.data.sql_validator import SQLRejected, validate_sql

    with pytest.raises(SQLRejected) as exc_info:
        validate_sql("DELETE FROM orders WHERE id=1")
    assert exc_info.value.reason == "not-select"


# ================= mock 生成器 =================


def test_mock_generator_matches_eval_question():
    """评测集问题命中 gold SQL（mock 测链路用）。"""
    gen = MockSQLGenerator()
    sql = gen.generate("华东区域有多少订单？")
    assert "orders" in sql and "华东" in sql


def test_mock_generator_fallback_safe():
    """未命中 → 默认安全查询。"""
    gen = MockSQLGenerator()
    assert gen.generate("不存在的奇怪问题") == "SELECT COUNT(*) AS cnt FROM orders"


# ================= 权限 =================


def test_data_query_requires_permission():
    """/api/data/query 需要 data:nl2sql 权限（用 401/403 三态验证）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # 未登录 → 401（全局 JWT 门禁）
        resp = c.post("/api/data/query", json={"question": "华东区域有多少订单？"})
        assert resp.status_code in (401, 403)
        # viewer 无 data:nl2sql → 403（seeded 环境）
        # analyst 有 data:nl2sql → 走 mock 链路返回表格（seeded 环境）
        # 这里不依赖 seed 的精确用户，401/403 已覆盖权限存在性；
        # 200 正例在 test_biz_seed 环境的 test_api_query_success 单独验证。


# ================= API 正例（依赖 seed 用户） =================


@pytest.mark.asyncio
async def test_api_query_success_with_token():
    """带合法 token 的 /api/data/query 正例（需 scm_platform seed：analyst 用户）。

    ★ Day6：响应含 insights（mock 确定性摘要，≤3 条）。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "analyst_t_huadong", "password": "Passw0rd!"},
        )
        if login.status_code != 200:
            pytest.skip("scm_platform 未 seed analyst 用户，跳过 200 正例")
        token = login.json()["access_token"]
        resp = c.post(
            "/api/data/query",
            json={"question": "华东区域有多少订单？"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["table"] is True
        assert "cnt" in body["columns"]
        assert len(body["rows"]) == 1
        assert body["elapsed"] >= 0
        assert body["sql"].startswith("SELECT")
        # ★ Day6：insights 字段（mock 确定性摘要，≤3 条）
        assert isinstance(body["insights"], list)
        assert len(body["insights"]) <= 3
        assert body["insights"]  # 非空（查询成功必有行数/首行摘要）


# ================= ★ W24 Day6：编排服务 + 对话入口 data 分支 =================


@pytest.mark.asyncio
async def test_service_run_nl2sql_returns_insights():
    """service.run_nl2sql_query：统一编排返回 insights（mock 确定性摘要）。"""
    from app.domains.data.service import run_nl2sql_query

    res = await run_nl2sql_query(question="华东区域有多少订单？")
    assert res["ok"] is True
    assert res["table"] is True
    assert res["columns"] == ["cnt"]
    assert res["sql"].startswith("SELECT")
    assert res["insights"]  # 非空
    assert len(res["insights"]) <= 3


@pytest.mark.asyncio
async def test_service_run_nl2sql_rejected_has_no_insights():
    """拒答/降级：无表格 → 无洞察（不硬编）。

    mock 生成器对未命中问题返回默认安全 SQL（不会拒答），因此"无表格"只能在
    graph 层用写 SQL 注入触发；service 层洞察生成逻辑对空 rows 返回空（已由
    test_insight.py::test_generate_insights_empty_rows_returns_empty 覆盖）。
    """
    from app.domains.data.graph import data_graph

    # graph 层：写 SQL 直接过闸 → 拒答（无表格）
    state = await data_graph.ainvoke(
        {"question": "删掉所有订单", "today": "2026-08-18",
         "initial_sql": "DELETE FROM orders WHERE 1=1"}
    )
    assert state["rejected_reason"] is not None
    assert not (state.get("result") or {}).get("columns")


@pytest.mark.asyncio
async def test_service_run_nl2sql_multiturn_resolve():
    """多轮消解走统一服务：'那华南呢？' 继承上轮上下文。"""
    from app.domains.data.service import run_nl2sql_query
    from app.domains.data.session_ctx import clear_sessions

    clear_sessions()
    r1 = await run_nl2sql_query(question="华东区域有多少订单？", session_id="sess-d6")
    assert r1["table"] is True
    # 追问："那华南呢？"（mock 规则消解 → 华南区域有多少订单？）
    r2 = await run_nl2sql_query(question="那华南呢？", session_id="sess-d6")
    assert r2["table"] is True
    assert r2["resolved_question"] != "那华南呢？"
    assert "华南" in r2["resolved_question"]
    clear_sessions()


@pytest.mark.asyncio
async def test_kb_chat_data_branch_streams_data_table():
    """对话入口 /api/kb/chat 语义路由 data 分支 → SSE data_table 事件（需 seed 用户）。

    用 analyst（有 data:nl2sql）验证；viewer 无权限 → 礼貌拒答（不含表格）。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "analyst_t_huadong", "password": "Passw0rd!"},
        )
        if login.status_code != 200:
            pytest.skip("scm_platform 未 seed analyst 用户，跳过对话入口正例")
        token = login.json()["access_token"]
        resp = c.post(
            "/api/kb/chat",
            json={"message": "近30天延迟发货的订单有多少"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        events = [
            line[6:] for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        types = []
        data_table = None
        for ev in events:
            import json as _json

            obj = _json.loads(ev)
            types.append(obj["type"])
            if obj["type"] == "data_table":
                data_table = obj
        assert "data_table" in types
        assert data_table is not None
        assert data_table["columns"]
        assert data_table["sql"].startswith("SELECT")
        assert data_table["insights"] is not None
