"""NL2SQL 错误自修复测试（W24 Day5）。

覆盖 Day5 验收：
- 修复 prompt：含原问题/坏 SQL/报错原文 + "不改语义"硬性规则
- mock 修复生成器：gold 模式按问题返回评测集 gold SQL；fail 模式原样返回（降级路径）
- 图路由：可修复闸拒（parse-error/unknown-table）→ repair；安全类闸拒 → reject/degrade
- 修复链（integration，需 MySQL + seed）：
    错列名 / 错表名 / 语法错 三类坏 SQL → 救回且结果与 gold 一致
    两次失败 → 降级话术（不硬答）；修复产物仍必须过四道闸（安全不豁免）
- 回归：合法问题不触发修复（repair_attempts=0）

标签：纯逻辑单测无需 DB；链路类 integration（CI 有 MySQL service + seed）。
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
os.environ["LLM_PROVIDER"] = "mock"

from app.domains.data.executor import dispose_engine, execute_sql  # noqa: E402
from app.domains.data.graph import (  # noqa: E402
    data_graph,
    route_after_execute,
    route_after_repair,
    route_after_validate,
)
from app.domains.data.mock_repair import MockRepairGenerator  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.repair import (  # noqa: E402
    MAX_REPAIR_ATTEMPTS,
    REPAIRABLE_REASONS,
    build_repair_messages,
)

TODAY = DATA_BASE_DATE.isoformat()

# 评测集 #1：合法 gold（三类坏 SQL 的修复目标）
GOLD_1 = "SELECT COUNT(*) AS cnt FROM orders WHERE region='华东'"
GOLD_2 = "SELECT order_no, amount FROM orders ORDER BY amount DESC LIMIT 5"


# ==================== 修复 prompt ====================


def test_build_repair_messages_contains_error_context():
    msgs = build_repair_messages("华东区域有多少订单？", "SELECT * FROM orderss", "Table doesn't exist", TODAY)
    joined = "\n".join(m["content"] for m in msgs)
    assert "华东区域有多少订单" in joined
    assert "orderss" in joined
    assert "Table doesn't exist" in joined  # 报错原文完整给到


def test_build_repair_messages_forbids_semantic_change_and_write():
    msgs = build_repair_messages("q", "s", "e", TODAY)
    joined = "\n".join(m["content"] for m in msgs)
    # 不改语义（手册坑：防止"能跑但答非所问"的 SQL）
    assert "业务语义" in joined and "GROUP BY" in joined
    # 安全不豁免：严禁写操作
    assert "INSERT" in joined and "UPDATE" in joined and "DELETE" in joined
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"


# ==================== mock 修复生成器 ====================


def test_mock_repair_gold_mode_returns_eval_gold():
    gen = MockRepairGenerator(mode="gold")
    sql = gen.generate("华东区域有多少订单？", "SELECT * FROM orderss")
    assert sql == GOLD_1  # 评测集问题命中 → gold SQL（救回路径）


def test_mock_repair_gold_mode_unknown_question_returns_input():
    gen = MockRepairGenerator(mode="gold")
    broken = "SELECT * FROM ord"
    assert gen.generate("评测集外的问题", broken) == broken  # 未命中 → 原样（继续失败测降级）


def test_mock_repair_fail_mode_returns_input():
    gen = MockRepairGenerator(mode="fail")
    broken = "SELECT * FROM orderss"
    assert gen.generate("华东区域有多少订单？", broken) == broken


# ==================== 图路由（纯逻辑，无需 DB） ====================


def test_route_validate_pass_goes_execute():
    assert route_after_validate({"rejected_reason": None}) == "execute"


def test_route_validate_repairable_reason_goes_repair():
    for reason in REPAIRABLE_REASONS:
        assert route_after_validate({"rejected_reason": reason}) == "repair"


def test_route_validate_security_reason_rejects_first_time():
    assert route_after_validate({"rejected_reason": "write-op"}) == "reject"
    assert route_after_validate({"rejected_reason": "not-select"}) == "reject"


def test_route_validate_security_during_repair_degrades():
    """修复循环中产物再被安全闸拒 → 直接降级（安全不豁免）。"""
    assert route_after_validate({"rejected_reason": "write-op", "repair_attempts": 1}) == "degrade"


def test_route_after_execute():
    assert route_after_execute({"error": "Unknown column"}) == "repair"
    assert route_after_execute({"error": None}) == "format"


def test_route_after_repair():
    assert route_after_repair({"repair_exhausted": True}) == "degrade"
    assert route_after_repair({"repair_exhausted": False}) == "validate"


# ==================== 修复链（integration，需 MySQL + seed） ====================


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine():
    yield
    await dispose_engine()


@pytest.mark.integration
async def _rescue_case(question: str, broken_sql: str, gold_sql: str) -> dict:
    state = await data_graph.ainvoke(
        {"question": question, "today": TODAY, "initial_sql": broken_sql}
    )
    res = state.get("result") or {}
    assert state["error"] is None, f"执行报错: {state['error']}"
    assert state["rejected_reason"] is None, f"被拒: {state['rejected_reason']}"
    assert res["columns"], "未返回表格（未救回）"
    assert state["repair_attempts"] >= 1, "应当至少修复 1 次"
    gold_res = await execute_sql(gold_sql)
    assert res["columns"] == gold_res["columns"]
    assert sorted(map(tuple, res["rows"])) == sorted(map(tuple, gold_res["rows"]))
    return state


@pytest.mark.integration
async def test_graph_repair_wrong_column_rescued():
    """错列名：regionx → 执行报 1054 → 修复为 gold → 结果一致。"""
    broken = "SELECT COUNT(*) AS cnt FROM orders WHERE regionx='华东'"
    state = await _rescue_case("华东区域有多少订单？", broken, GOLD_1)
    assert len(state["repair_log"]) == 1
    assert "regionx" in state["repair_log"][0]["failed_sql"]


@pytest.mark.integration
async def test_graph_repair_wrong_table_rescued():
    """错表名：ordersx → 白名单闸拒 unknown-table（可修复）→ 修复为 gold。"""
    broken = "SELECT COUNT(*) AS cnt FROM ordersx WHERE region='华东'"
    state = await _rescue_case("华东区域有多少订单？", broken, GOLD_1)
    # 修复产物必须重新过闸（安全不豁免）：gold 表名合法 → 通过
    assert "orders" in state["repair_log"][0]["repaired_sql"]


@pytest.mark.integration
async def test_graph_repair_syntax_rescued():
    """语法错：末尾追加未闭合括号 → parse-error（可修复）→ 修复为 gold。"""
    broken = "SELECT COUNT(*) AS cnt FROM orders WHERE region='华东'("
    await _rescue_case("华东区域有多少订单？", broken, GOLD_1)


@pytest.mark.integration
async def test_graph_repair_topn_rescued():
    """TOP N 查询错列名（order_no→order_nox）同样可救。"""
    broken = "SELECT order_nox, amount FROM orders ORDER BY amount DESC LIMIT 5"
    await _rescue_case("金额最高的前5个订单的订单号和金额？", broken, GOLD_2)


@pytest.mark.integration
async def test_graph_repair_degrade_after_max_attempts(monkeypatch):
    """修复两次仍失败 → 降级话术（不硬答），不产出表格。"""
    monkeypatch.setenv("MOCK_REPAIR_MODE", "fail")  # 修复永远返回同一坏 SQL
    broken = "SELECT COUNT(*) AS cnt FROM orders WHERE regionx='华东'"
    state = await data_graph.ainvoke(
        {"question": "华东区域有多少订单？", "today": TODAY, "initial_sql": broken}
    )
    assert state["repair_exhausted"] is True
    assert state["repair_attempts"] > MAX_REPAIR_ATTEMPTS
    assert "暂时无法生成有效查询" in state["reply"]  # 降级话术
    assert not (state.get("result") or {}).get("columns")  # 不硬答


@pytest.mark.integration
async def test_graph_security_reject_not_repaired():
    """安全类闸拒永不修复：写操作 → 直接拒答。"""
    state = await data_graph.ainvoke(
        {"question": "删掉所有订单", "today": TODAY,
         "initial_sql": "DELETE FROM orders WHERE 1=1"}
    )
    assert state["rejected_reason"] == "not-select"
    assert state.get("repair_attempts", 0) == 0  # 安全拒绝永不触发修复
    assert "无法执行该查询" in state["reply"]


@pytest.mark.integration
async def test_graph_legit_question_no_repair():
    """合法问题不触发修复（回归 Day3 行为）。"""
    state = await data_graph.ainvoke({"question": "华东区域有多少订单？", "today": TODAY})
    assert state.get("repair_attempts", 0) == 0
    assert state["error"] is None
    assert (state.get("result") or {}).get("columns") == ["cnt"]
