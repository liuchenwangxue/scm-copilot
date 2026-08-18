"""★ 结果洞察测试（W24 Day6）——禁止编造数字的双保险（prompt + 数字溯源校验）。

覆盖 Day6 上午验收：
- prompt 构建：包含结果集 JSON + "严禁编造数字"硬性规则
- 数字溯源校验 `verify_insight_digits`：
    · 结果集内的数字 → 通过（含百分比/万位换算、四舍五入容差）
    · 编造数字（不在结果集中）→ 拒绝
    · 日期字符串里的数字（'2026-08-18'）不算业务数字 → 编造撞不上
- mock 确定性摘要：行数/字段/首行，数字全部来自结果集（必然过校验）
- generate_insights mock 路径：返回 ≤3 条
- _clean_insights：LLM 输出中编造数字的整条被丢弃（real 链路清洗逻辑）

纯逻辑单测（无需 DB / 模型），CI 可跑。
"""

import os

os.environ["LLM_PROVIDER"] = "mock"

import pytest

from app.domains.data.insight import (  # noqa: E402
    MAX_INSIGHTS,
    _clean_insights,
    _mock_insights,
    _result_numeric_values,
    _serialize_result,
    build_insight_messages,
    generate_insights,
    verify_insight_digits,
)

# 典型结果集：区域 × 订单金额（aggregation 类）
COLS = ["region", "total_amount"]
ROWS = [
    ["华东", 1_234_567.89],
    ["华北", 980_000.0],
    ["华南", 1_050_000.0],
    ["西南", 760_000.0],
]
NUMERIC = _result_numeric_values(COLS, ROWS)


# ==================== prompt 构建 ====================


def test_build_insight_messages_contains_result_and_forbids_fabrication():
    msgs = build_insight_messages("各区域的订单总金额？", COLS, ROWS, "SELECT region, SUM(amount)...")
    joined = "\n".join(m["content"] for m in msgs)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    assert "严禁编造数字" in joined  # 硬性规则
    assert "1234567.89" in joined  # 结果集 JSON 注入
    assert "各区域的订单总金额" in joined  # 原问题
    assert "SELECT region" in joined  # 实际 SQL


def test_serialize_result_str_values():
    """序列化：值转 str（防非 JSON 类型），列名作 key。"""
    js = _serialize_result(COLS, ROWS)
    assert '"region": "华东"' in js
    assert '"total_amount": "1234567.89"' in js


def test_serialize_result_max_rows_cap():
    many = [[f"x{i}", float(i)] for i in range(20)]
    js = _serialize_result(["a", "b"], many, max_rows=5)
    assert js.count('"a"') == 5  # 只注入前 5 行


# ==================== 数字溯源校验 ====================


def test_verify_real_digit_from_result_passes():
    assert verify_insight_digits("华东订单金额最高，为 1234567.89", NUMERIC)


def test_verify_fabricated_digit_rejected():
    """编造数字（不在结果集）→ 拒绝。"""
    assert not verify_insight_digits("华东订单金额 99999999", NUMERIC)


def test_verify_percentage_conversion():
    """'32%' → 0.32，与结果集中 0.32 同口径。"""
    assert verify_insight_digits("占比 32%", [0.32])


def test_verify_wan_unit_conversion():
    """'123.46万' → 1_234_600，与结果集 1234567.89 容差内通过。"""
    assert verify_insight_digits("约 123.46 万", [1234567.89])


def test_verify_rounded_digit_tolerance():
    """四舍五入转写（1234568 vs 1234567.89，相对差 <1%）→ 通过。"""
    assert verify_insight_digits("1234568", [1234567.89])


def test_verify_date_digits_not_counted_as_business():
    """日期字符串里的数字不算业务数值 → 编造'2026单'查无出处被拦。"""
    cols = ["created_at"]
    rows = [["2026-08-18 10:00:00"]]
    nums = _result_numeric_values(cols, rows)
    assert nums == []  # 日期字符串被排除（防编造撞上）
    assert not verify_insight_digits("共 2026 单", nums)


def test_verify_biz_string_digits_traceable():
    """★ 业务字符串（供应商名含编号）里的数字可溯源 → 合法洞察不被误杀。

    W24 Day6 real 实测：LLM 引用公司名"华东宏图44有限公司"时，44 必须在溯源集合里，
    否则合法摘要被整条丢弃。
    """
    cols = ["name", "cnt"]
    rows = [["华东宏图44有限公司", 361], ["华东众联95有限公司", 346]]
    nums = _result_numeric_values(cols, rows)
    assert 44.0 in nums and 95.0 in nums and 361.0 in nums
    assert verify_insight_digits("华东宏图44有限公司共 361 单", nums)


def test_verify_empty_text_rejected():
    assert not verify_insight_digits("", NUMERIC)
    assert not verify_insight_digits("   ", NUMERIC)


# ==================== mock 确定性摘要 ====================


def test_mock_insights_describe_result_without_fabrication():
    items = _mock_insights("q", COLS, ROWS, "sql")
    assert len(items) <= MAX_INSIGHTS
    assert "4 行" in items[0]
    assert "region" in items[0]
    assert items[1].startswith("首行数据")
    # 每条数字必然来自结果集（mock 摘要本身引用行数/首行 → 溯源必过）
    for it in items:
        assert verify_insight_digits(it, NUMERIC)


def test_mock_insights_empty_result():
    assert _mock_insights("q", ["a"], [], "sql") == ["查询无结果（0 行）"]


# ==================== generate_insights（mock 路径） ====================


@pytest.mark.asyncio
async def test_generate_insights_mock_path():
    items = await generate_insights("各区域的订单总金额？", COLS, ROWS, "sql")
    assert 1 <= len(items) <= MAX_INSIGHTS
    assert "4 行" in items[0]


@pytest.mark.asyncio
async def test_generate_insights_empty_rows_returns_empty():
    items = await generate_insights("q", COLS, [], "sql")
    assert items == []


# ==================== _clean_insights（real 清洗逻辑） ====================


def test_clean_insights_drops_fabricated_lines():
    raw = (
        "- 华东区域订单金额最高，为 1234567.89\n"
        "- 编造的 99999999 元不存在\n"
        "- 华北区域金额约 980000\n"
    )
    items = _clean_insights(raw, COLS, ROWS, MAX_INSIGHTS)
    assert len(items) == 2  # 中间一条（编造数字）被丢弃
    assert "编造" not in items[0] and "编造" not in items[1]
    assert "华东区域订单金额最高" in items[0]


def test_clean_insights_strips_dash_prefix():
    items = _clean_insights("- 华东 1234567.89", COLS, ROWS, MAX_INSIGHTS)
    assert items and not items[0].startswith("- ")


def test_clean_insights_respects_max_items():
    raw = "\n".join(f"- 第{i}条 1234567.89" for i in range(5))
    items = _clean_insights(raw, COLS, ROWS, 3)
    assert len(items) == 3
