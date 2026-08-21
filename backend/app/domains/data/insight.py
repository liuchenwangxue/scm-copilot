"""★ 结果洞察（W24 Day6）——LLM 对查询结果集生成 ≤3 条业务摘要（禁止编造数字）。

对应《W24学习执行手册》Day6 上午 +《03》1.4 节"结果呈现"：
- 输入：查询结果 {columns, rows}（executor 已把 Decimal→float、datetime→isoformat）
  + 原问题 + 实际执行的 SQL；
- 输出：≤3 条自然语言业务洞察（"TOP1 供应商占 32%，前五集中度 71%" 类）；
- **禁止编造数字**（手册坑）：prompt 给结果集 JSON 并硬性要求引用行数据 + 输出后
  做**数字溯源校验**（`verify_insight_digits`）——摘要中每个数字必须能在结果集的
  数值单元格中找到（支持 %/万/千/亿 量纲换算与四舍五入容差），找不到的整条丢弃。
  双保险：prompt 约束 + 确定性校验兜底；
- mock 双路径（手册坑"mock 测链路、real 测效果"）：
  - provider=mock → 确定性规则摘要（`_mock_insights`：只描述行数/字段/首行，
    数字全部来自结果集 → 必然过校验），只测链路不算效果；
  - provider=real → `build_insight_messages` + 模型池生成 → 数字溯源校验 → 清理。

接口：
    build_insight_messages(question, columns, rows, sql) -> list[dict]
    async generate_insights(question, columns, rows, sql, max_items=3) -> list[str]
    verify_insight_digits(text, numeric_values) -> bool   # 数字溯源校验（单测用）
    _serialize_result(columns, rows, max_rows=10) -> str   # 结果集 JSON（prompt 注入）
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.shared.llm import get_provider

MAX_INSIGHTS = 3
RESULT_JSON_MAX_ROWS = 10  # prompt 注入结果集最多前 10 行（控 token）

# ==================== 结果集序列化（prompt 注入） ====================


def _serialize_result(
    columns: list[str], rows: list[list[Any]], max_rows: int = RESULT_JSON_MAX_ROWS
) -> str:
    """结果集转 JSON：每行 {列名: 值} 列表，值一律 str（防非 JSON 类型）。"""
    data = [
        dict(zip(columns, [str(v) for v in r], strict=False))
        for r in rows[:max_rows]
    ]
    return json.dumps(data, ensure_ascii=False)


# ==================== 数字溯源校验（禁止编造数字的确定性兜底） ====================

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|万|千|亿)?")
# 量纲换算：'32%' → 0.32、'123.4万' → 1_234_000（与结果集数值同口径比较）
_UNIT = {"%": 0.01, "万": 10_000, "千": 1_000, "亿": 100_000_000}
# 单数字（≤10）视为"非业务数字"豁免溯源：行数/序号/排名（"4 行"、"TOP1"、"第2条"）。
# 编造风险集中在多位数业务数字（金额/数量/百分比），单数字误拦会丢掉合法洞察。
_SMALL_DIGIT = 10.0


def _numeric_tokens(text: str) -> list[tuple[float, float]]:
    """提取文本中的数字 token：(数值, 量纲乘子)；无数字返回空列表。

    支持数字与量纲间空格（'123.46 万' 与 '123.46万' 都命中）。
    """
    out: list[tuple[float, float]] = []
    for m in _NUM_RE.finditer(text or ""):
        val = float(m.group(1))
        unit = _UNIT.get(m.group(2) or "", 1.0)
        out.append((val, unit))
    return out


# 日期时间格式（如 '2026-08-18 10:00:00' / '2026-08-18'）——字符串里的数字不溯源，
# 否则"编造 2026 单"会撞上日期里的 2026，校验形同虚设。
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
# ★ 修复：文本侧的全局日期模式（洞察句内"订单集中在 2026-08-18"中的 2026/18
#   也是日期数字，不应强制溯源——与结果集侧 _DATE_RE 豁免口径对齐，否则含日期
#   的合法洞察被误杀）
_TEXT_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _result_numeric_values(columns: list[str], rows: list[list[Any]]) -> list[float]:
    """可溯源数字集合：数值单元格值 + 业务字符串中的数字（排除日期/时间串）。

    ★ W24 Day6 实测修复：供应商名称含编号（"华东宏图44有限公司"），LLM 摘要引用
    公司名时其中的数字（44）必须可溯源，否则合法洞察被误杀；但日期串（2026-08-18）
    的数字不溯源，防编造撞上。
    """
    vals: list[float] = []
    for r in rows[:RESULT_JSON_MAX_ROWS]:
        for v in r:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
            elif isinstance(v, str) and not _DATE_RE.match(v.strip()):
                # 业务字符串（公司名/承运商名等）里的数字：如 "华东宏图44" → 44
                for m in re.findall(r"\d+(?:\.\d+)?", v):
                    vals.append(float(m))
    return vals


def _near(a: float, b: float) -> bool:
    """数字近似匹配：绝对容差 0.005 或相对 1%（支持模型四舍五入/取整转写）。"""
    if b == 0:
        return abs(a) < 0.005
    return abs(a - b) <= max(0.005, abs(b) * 0.01)


def verify_insight_digits(text: str, numeric_values: list[float]) -> bool:
    """摘要中每个**业务数字**（经量纲换算）都必须在结果集数值中找到，否则视为编造。

    - 带量纲（%/万/千/亿）或 >10 的数字 → 强制溯源校验；
    - 单数字（≤10，行数/序号/排名）→ 豁免（"4 行"、"TOP1"、"第2条" 不算业务数字）；
    - 结果集无数值（如纯名称 TOP 列表）→ 带量纲/大数必被拦；
    - 空文本直接拒绝（不产出空洞察）。
    """
    if not (text or "").strip():
        return False
    # ★ 修复：先剥离日期子串再提取数字（日期里的年/日数字不参与溯源校验）
    text = _TEXT_DATE_RE.sub(" ", text)
    for val, unit in _numeric_tokens(text):
        target = val * unit
        if unit == 1.0 and abs(val) <= _SMALL_DIGIT:
            continue  # 单数字豁免：行数/序号/排名
        if not any(_near(target, r) for r in numeric_values):
            return False
    return True


# ==================== prompt 构建（real 模式） ====================


def build_insight_messages(
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    sql: str,
    max_rows: int = RESULT_JSON_MAX_ROWS,
) -> list[dict[str, str]]:
    """洞察 prompt：给结果集 JSON，要求生成 ≤3 条摘要并**只引用结果集中的数字**。

    硬性规则（手册坑：prompt 约束 + 结果集引用双保险的第一层）：
    1. 严禁编造数字：只能引用【查询结果 JSON】中出现过的数值；
    2. 每条摘要对应结果集中的具体行/数值（模型有据可依）；
    3. 输出格式：每条一行、'- ' 开头、不超过 3 条。
    """
    result_json = _serialize_result(columns, rows, max_rows)
    system = (
        "你是供应链数据分析师。根据用户问题、SQL 和查询结果，生成业务洞察摘要。\n"
        "硬性规则：\n"
        "1. **严禁编造数字**：只允许引用【查询结果 JSON】中出现过的数值；"
        "每个数字必须能在结果 JSON 的某一行的某个值中找到（可做百分比/万位等合理换算）；\n"
        "2. 摘要要有业务视角：如 'TOP1 供应商占 32%（前 5 行）'、'延迟发货共 545 单'、"
        "'四区域中订单金额最高的是华东'；\n"
        "3. 输出 ≤3 条，每条一行、以 '- ' 开头；不要输出任何解释或 Markdown。"
    )
    user = (
        f"## 用户问题\n{question}\n\n"
        f"## 实际执行的 SQL\n{sql}\n\n"
        f"## 查询结果 JSON（前 {max_rows} 行）\n{result_json}\n\n"
        "## 请生成不超过 3 条洞察摘要"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ==================== mock 确定性摘要（测链路，不算效果） ====================


def _mock_insights(
    question: str, columns: list[str], rows: list[list[Any]], sql: str
) -> list[str]:
    """确定性规则摘要：只描述行数/字段/首行（数字全部来自结果集 → 必然过校验）。

    mock 的职责是验证"洞察链路"（prompt 构建 → 生成 → 校验 → 输出）可跑通，
    不产生真实业务洞察；效果数字只来自 real。
    """
    if not rows:
        return ["查询无结果（0 行）"]
    items = [f"查询返回 {len(rows)} 行结果（字段：{', '.join(columns)}）"]
    if rows:
        items.append(f"首行数据：{rows[0]}")
    return items[:MAX_INSIGHTS]


# ==================== 生成入口（mock/real 双路径） ====================


def _clean_insights(
    raw: str, columns: list[str], rows: list[list[Any]], max_items: int
) -> list[str]:
    """清洗 LLM 输出：按行拆分 → 逐条数字溯源校验 → 取前 max_items 条。"""
    numeric_values = _result_numeric_values(columns, rows)
    items: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-·•").strip()
        if not line:
            continue
        if not verify_insight_digits(line, numeric_values):
            continue  # 编造数字 → 整条丢弃（prompt + 校验双保险）
        items.append(line)
        if len(items) >= max_items:
            break
    return items


async def generate_insights(
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    sql: str,
    max_items: int = MAX_INSIGHTS,
) -> list[str]:
    """生成结果洞察（≤max_items 条）。

    - provider=mock：确定性规则摘要（测链路）；
    - provider=real：模型池生成 → 数字溯源校验 → 清理（效果只算 real）。
    空结果集直接返回空列表（无数据可洞察，不硬编）。
    """
    if not rows:
        return []
    provider = get_provider()
    if provider.name == "mock":
        return _mock_insights(question, columns, rows, sql)[:max_items]

    messages = build_insight_messages(question, columns, rows, sql)
    raw = await provider.generate(messages, max_tokens=512, temperature=0.0)
    return _clean_insights(raw, columns, rows, max_items)
