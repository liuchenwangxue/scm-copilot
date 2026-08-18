"""多轮会话上下文与指代消解测试（W24 Day5）。

覆盖 Day5 验收：
- mock 规则消解：区域/时间/状态替换与补插、"各区域"聚合对比、组合追问（12 条模式）
- 首轮原样返回；record/recent 上下文存取；全局注册表
- 消解 prompt 内容（单独一次 LLM 调用，不塞进 SQL 生成）
- 多轮全链路（integration，需 MySQL + seed）：消解 → NL2SQL 图 → 结果与 gold 一致

标签：纯逻辑单测无需 DB；全链路 integration。
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
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.mock_sql import clear_mock_sql_registry, register_mock_sql  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.session_ctx import (  # noqa: E402
    _clean_resolved,
    _mock_resolve,
    build_resolve_messages,
    clear_sessions,
    get_session,
)

TODAY = DATA_BASE_DATE.isoformat()


# ==================== mock 规则消解（12 条模式） ====================

MOCK_RESOLVE_CASES: list[tuple[str, str, str]] = [
    # 时间替换
    ("近7天创建了多少订单？", "那近30天呢？", "近30天创建了多少订单？"),
    # 区域 → 各区域
    ("华东区域订单总金额是多少？", "那各区域呢？", "各区域订单总金额是多少？"),
    # 区域替换（带时间+状态延续）
    ("近30天华东区域已支付的订单有多少？", "那华北呢？", "近30天华北区域已支付的订单有多少？"),
    # 连续区域替换
    ("华东区域有多少订单？", "华北呢？", "华北区域有多少订单？"),
    ("华北区域有多少订单？", "华南呢？", "华南区域有多少订单？"),
    # 状态替换
    ("已取消的订单有多少？", "那已完成的呢？", "已完成的订单有多少？"),
    # 时间补插
    ("延迟发货的订单有多少？", "那近30天呢？", "近30天延迟发货的订单有多少？"),
    # 状态补插（保留时间+分组）
    ("近30天各区域的订单数量？", "只算已支付的呢？", "近30天各区域的已支付的订单数量？"),
    # 区域替换（供应商维度）
    ("华东区域的供应商有多少？", "那华南呢？", "华南区域的供应商有多少？"),
    # 各区域 → 单区域
    ("各区域的订单数量？", "西南呢？", "西南区域的订单数量？"),
    # 状态补插（TOP N）
    ("金额最高的前5个订单的订单号和金额？", "只看已完成的呢？", "金额最高的前5个已完成的订单的订单号和金额？"),
    # 时间补插（组合追问）
    ("金额最高的前5个已完成的订单的订单号和金额？", "近30天呢？", "近30天金额最高的前5个已完成的订单的订单号和金额？"),
]


@pytest.mark.parametrize("prev,q,expected", MOCK_RESOLVE_CASES)
def test_mock_resolve(prev: str, q: str, expected: str):
    assert _mock_resolve(prev, q) == expected


def test_mock_resolve_unmatched_returns_original():
    assert _mock_resolve("华东区域有多少订单？", "华东区域订单总金额是多少？") == "华东区域订单总金额是多少？"


# ==================== 消解 prompt ====================


def test_build_resolve_messages_contains_context():
    msgs = build_resolve_messages("上个月华东的延迟订单有多少", "SELECT ...", "那华南呢？")
    joined = "\n".join(m["content"] for m in msgs)
    assert "上个月华东的延迟订单有多少" in joined
    assert "那华南呢？" in joined
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"


def test_clean_resolved():
    assert _clean_resolved('"华东区域有多少订单？"') == "华东区域有多少订单？"
    assert _clean_resolved("```text\n近30天创建了多少订单？\n```") == "近30天创建了多少订单？"


# ==================== 会话上下文 ====================


async def test_first_turn_unchanged():
    ctx = get_session("s1")
    assert await ctx.resolve("华东区域有多少订单？", TODAY) == "华东区域有多少订单？"


async def test_record_recent_and_resolve_with_context():
    ctx = get_session("s2")
    await ctx.resolve("华东区域有多少订单？", TODAY)  # 首轮不消解
    ctx.record("华东区域有多少订单？", "SELECT COUNT(*) FROM orders WHERE region='华东'", ["orders"])
    assert ctx.recent()["question"] == "华东区域有多少订单？"
    assert await ctx.resolve("那华北呢？", TODAY) == "华北区域有多少订单？"


def test_session_registry_dedup_and_clear():
    clear_sessions()
    a = get_session("s3")
    b = get_session("s3")
    assert a is b
    clear_sessions()
    c = get_session("s3")
    assert c is not a


# ==================== 多轮全链路（integration，需 MySQL + seed） ====================


@pytest_asyncio.fixture(autouse=True)
async def _dispose():
    yield
    await dispose_engine()
    clear_mock_sql_registry()


@pytest.mark.integration
async def test_multiturn_full_chain_resolution_and_execution():
    """消解 → NL2SQL 图 → 结果与 gold 一致（mock 链路：注册每轮问题 → gold SQL）。"""
    from app.domains.data.session_ctx import SessionContext

    case = [
        ("华东区域有多少订单？", "华东区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='华东'"),
        ("那华北呢？", "华北区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='华北'"),
        ("那西南呢？", "西南区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='西南'"),
    ]
    clear_mock_sql_registry()
    for _q, resolved, gold in case:
        register_mock_sql(resolved, gold)  # 注册"消解后的完整问题"（mock 确定性链路）

    ctx = SessionContext("eval")
    prev: str | None = None
    for q, expected_resolved, gold_sql in case:
        resolved = await ctx.resolve(q, TODAY)
        if prev is not None:
            assert resolved != q, f"追问应被消解: {q}"
        assert resolved == expected_resolved, f"消解结果不符: {resolved!r} != {expected_resolved!r}"

        state = await data_graph.ainvoke({"question": resolved, "today": TODAY})
        res = state.get("result") or {}
        assert state["error"] is None and state["rejected_reason"] is None
        gold_res = await execute_sql(gold_sql)
        assert sorted(map(tuple, res["rows"])) == sorted(map(tuple, gold_res["rows"]))

        ctx.record(resolved, res["sql"], [])
        prev = resolved
