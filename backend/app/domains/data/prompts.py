"""NL2SQL prompt 模板 v1/v2（W24 Day3 + Day4 演进）——全 schema 注入 vs Schema Linking 召回。

对应《W24学习执行手册》Day3 上午 + Day4：
- v1【全 schema 注入】：六表完整 DDL + 5 条 few-shot（单表过滤/两表 join/聚合分组/时间窗/排序）；
- v2【Schema Linking 召回】：`PROMPT_VERSION=v2` 时只注入召回 Top-3 表的 DDL 片段 +
  **精选 few-shot**（few-shot 与召回表联动：涉及表 ⊆ 召回表才注入——
  手册坑"join few-shot 只在召回 ≥2 表时注入，否则干扰单表问题"）；
- 日期口径：**时间窗示例由 `today` 驱动生成显式日期**（如 `created_at >= '2026-07-19'`），
  不用 `CURDATE() - INTERVAL 7 DAY`——评测集数据基准 BASE_DATE=2026-08-18（固定 seed），
  若用 CURDATE() 则运行日漂移导致空结果集被误判为 SQL 错（手册 Day1 坑）。
  评测传 today=BASE_DATE（数据基准日），生产传实际当天，few-shot 自动对齐；
- 规则强调：只读、必须 LIMIT、时间列用 created_at、今日日期显式给出。
- ★ Day3 欠账修复（#44 缺名称列）：v2 few-shot 增加 `orders JOIN suppliers GROUP BY s.name`
  示例（按供应商名称分组聚合——治"各供应商平均金额缺名称列"）。
- ★ Day4 修复（#46）：few-shot 增加 `shipments JOIN orders` 双条件过滤示例
  （"已发货订单中延迟发货"→ status + delay_days 都要过滤）；
  few-shot 按"与召回表重叠度"动态排序（join 类问题优先选到最相关示例）。

接口：
    build_nl2sql_messages(question, today="2026-08-18") -> list[dict]
        system 指令 + user（schema + few-shot + 问题），符合 shared.llm messages 契约。
        内部按环境变量 PROMPT_VERSION（v1|v2，默认 v1）分发；
    build_nl2sql_messages_v1 / _v2(question, today, tables=None)
        v2 可传入已召回表列表（tables）跳过内部召回（评测 A/B 复用同一召回结果）；
    estimate_prompt_tokens(messages) -> int
        A/B 用 prompt token 估算（中文 1 token≈1.5 字、英文/数字/符号 1 token≈4 字符）。
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any

# ---- 数据基准日：与 scripts/seed_biz.py BASE_DATE 对齐（评测时 today 传它）----
DATA_BASE_DATE = date(2026, 8, 18)

# ==================== 全 schema（v1） ====================
# 单一数据源：DDL 片段在 schema_linker.TABLE_DDL（v2 召回注入同一份），
# 避免两份 schema 描述漂移。拼接后与 Day3 SCHEMA_TEXT 输出完全一致。
from app.domains.data.schema_linker import RELATIONSHIPS_TEXT, TABLE_DDL  # noqa: E402

# 表注入顺序（v1 全量按此顺序拼接；与 Day3 一致）
_TABLE_ORDER = ["orders", "order_items", "products", "suppliers", "inventory", "shipments"]

SCHEMA_TEXT = (
    "## 数据库 schema（业务库 scm_biz，六表）\n\n"
    + "\n\n".join(TABLE_DDL[t] for t in _TABLE_ORDER)
    + "\n\n" + RELATIONSHIPS_TEXT
)

# ==================== few-shot（v1/v2 共用定义，v2 按涉及表联动裁剪） ====================

# 中文字符正则（token 估算用）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """prompt token 粗略估算：中文按 ~1.5 字/token，其余按 ~4 字符/token。

    A/B 对比用（v1 vs v2 同口径，相对降幅可信）；非精确计数。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4.0) + 1


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """A/B 用：整组 messages 的 prompt token 估算（v1 vs v2 对比）。"""
    return sum(estimate_tokens(m.get("content") or "") for m in messages)


def _date_ago(today: date, days: int) -> str:
    """today 往前 N 天的 ISO 日期字符串（few-shot/提示里的显式日期口径）。"""
    return (today - timedelta(days=days)).isoformat()


def _few_shot(question: str, sql: str, tables: list[str]) -> dict[str, Any]:
    """few-shot 条目：question/sql + 涉及表标注（v2 联动裁剪用）。"""
    return {"question": question, "sql": sql, "tables": tables}


def build_few_shots(today: date | str) -> list[dict[str, Any]]:
    """few-shot 全集（v1 全注入；v2 按召回表联动裁剪）。

    today：数据基准日（评测传 BASE_DATE；生产传实际当天），时间窗示例自动对齐。
    ★ 第 6 条为 Day3 #44 修复示例（各供应商平均金额 → JOIN suppliers GROUP BY s.name）。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    recent30 = _date_ago(today, 30)

    return [
        # 1. 单表过滤（最普适，排最前——v2 前 N 条截断时优先保留）
        _few_shot(
            "华东区域有多少订单？",
            "SELECT COUNT(*) AS cnt FROM orders WHERE region = '华东'",
            ["orders"],
        ),
        # 2. ★ Day3 #44 修复：订单 × 供应商，按供应商名称分组聚合（治"缺名称列"）
        #    排第 2：join 类问题（orders+suppliers）前 N 条截断时能选到它
        _few_shot(
            "各供应商的订单平均金额最高的前 5 个？",
            "SELECT s.name, AVG(o.amount) AS avg_amount FROM orders o "
            "JOIN suppliers s ON o.supplier_id = s.id "
            "GROUP BY s.name ORDER BY avg_amount DESC LIMIT 5",
            ["orders", "suppliers"],
        ),
        # 3. 时间窗（近 30 天，显式日期口径——避免模型各写各的）
        _few_shot(
            "近 30 天创建了多少订单？",
            f"SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '{recent30}'",
            ["orders"],
        ),
        # 4. 排序 TOP N
        _few_shot(
            "金额最高的前 5 个订单有哪些？",
            "SELECT order_no, amount FROM orders ORDER BY amount DESC LIMIT 5",
            ["orders"],
        ),
        # 5. 聚合分组
        _few_shot(
            "各区域的订单总金额是多少？",
            "SELECT region, SUM(amount) AS total_amount FROM orders "
            "GROUP BY region ORDER BY total_amount DESC",
            ["orders"],
        ),
        # 6. 两表 join（订单明细 × 商品类目）
        _few_shot(
            "电子元件类目商品的累计销售金额是多少？",
            "SELECT SUM(i.amount) AS total_amount FROM order_items i "
            "JOIN products p ON i.product_id = p.id WHERE p.category = '电子元件'",
            ["order_items", "products"],
        ),
        # 7. ★ Day4 #46 修复：发货 × 订单，双条件过滤（"已发货订单中延迟发货"→
        #    status + delay_days 都要过滤，缺一会翻倍/失真）
        _few_shot(
            "已发货订单中延迟发货的有多少？",
            "SELECT COUNT(*) AS cnt FROM shipments sh "
            "JOIN orders o ON sh.order_no = o.order_no "
            "WHERE o.status = 'shipped' AND sh.delay_days > 0",
            ["shipments", "orders"],
        ),
    ]


def _select_few_shots(today: date | str, tables: list[str]) -> list[dict[str, Any]]:
    """v2 few-shot 联动：只注入"涉及表 ⊆ 召回表"的示例 + 按重叠度动态排序。

    - 过滤：涉及表不在召回表内的示例一律不注入（单表问题不被 join 示例干扰）；
    - **动态排序**（★ Day4 A/B 暴露）：重叠比例降序 → 涉及表数降序 → 原顺序。
      效果：
      - shipments+orders 召回 → shipments×orders 双条件示例排最前（#46 修复）；
      - orders+suppliers 召回 → #44 join suppliers 示例排最前（治缺名称列）；
      - 单表 orders 召回 → 单表示例按原顺序（filter / 时间窗）。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    recalled = set(tables)
    all_shots = build_few_shots(today)
    shots = [fs for fs in all_shots if set(fs["tables"]).issubset(recalled)]

    def _key(fs: dict[str, Any]) -> tuple[float, int, int]:
        fs_tables = set(fs["tables"])
        overlap = len(fs_tables & recalled)
        return (-overlap / max(len(fs_tables), 1), -len(fs_tables), all_shots.index(fs))

    return sorted(shots, key=_key)


def _system_prompt(today: date) -> str:
    today_str = today.isoformat()
    return (
        "你是 MySQL 8 专家。根据给定的数据库 schema 和用户问题，生成一条只读 SELECT 查询。\n"
        "硬性规则：\n"
        f"1. 只允许 SELECT（禁止 UPDATE/DELETE/INSERT/DDL）；今日日期为 {today_str}。\n"
        "2. 查询必须带 LIMIT 限制返回行数（默认 LIMIT 200；TOP N 类问题用 ORDER BY ... LIMIT N）。\n"
        "3. 时间类过滤条件一律用 created_at 列；'近 N 天' 写作 created_at >= '<今日-N天>' 的显式日期。\n"
        "4. 只能查询 schema 中出现的表和列；关联用给出的关联关系（order_no/product_id/supplier_id）。\n"
        "5. 只输出一条 SQL，不要输出解释、Markdown 代码块或分号以外的多余内容。"
    )


def _build_user(schema_text: str, few_shots: list[dict[str, Any]], question: str) -> str:
    few_shot_text = "\n".join(
        f"问题：{fs['question']}\nSQL：{fs['sql']}" for fs in few_shots
    )
    return (
        f"{schema_text}\n\n"
        f"## 示例（{len(few_shots)} 条）\n{few_shot_text}\n\n"
        f"## 问题\n{question}"
    )


def build_nl2sql_messages_v1(
    question: str, today: date | str = DATA_BASE_DATE
) -> list[dict[str, str]]:
    """v1：全 schema 注入 + 全部 few-shot（Day3 行为，不变）。"""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    return [
        {"role": "system", "content": _system_prompt(today)},
        {"role": "user", "content": _build_user(SCHEMA_TEXT, build_few_shots(today), question)},
    ]


# v2 few-shot 注入条数：A/B 扫描（90 条评测）选定的最优值——召回表相关示例取前 2 条
# （filter + window/#44），排序/聚合由 system 规则兜底；token 降 51% 且包含率 100%。
V2_FEW_SHOT_MAX = 2


def build_nl2sql_messages_v2(
    question: str,
    today: date | str = DATA_BASE_DATE,
    tables: list[str] | None = None,
) -> list[dict[str, str]]:
    """v2：Schema Linking 召回相关表 → 只注入召回表 DDL + 精选 few-shot。

    tables：可外部传入"已裁剪的注入表"（评测 A/B 复用同一召回，避免重复 embedding）；
    不传则内部走 link_prompt_tables（Top-3 召回 → 0.75 相对裁剪 → 注入表）。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    if not tables:
        from app.domains.data.schema_linker import linker

        tables = linker.link_prompt_tables(question)
    schema_text = _build_recalled_schema(tables)
    few = _select_few_shots(today, tables)[:V2_FEW_SHOT_MAX]
    return [
        {"role": "system", "content": _system_prompt(today)},
        {"role": "user", "content": _build_user(schema_text, few, question)},
    ]


def _build_recalled_schema(tables: list[str]) -> str:
    """召回表精简 DDL + 关联关系（≥2 表才注入关联——单表问题不需要，省 token）。"""
    from app.domains.data.schema_linker import RELATIONSHIPS_TEXT, linker

    text = linker.build_schema_text(tables)
    if len(tables) >= 2:
        text += "\n\n" + RELATIONSHIPS_TEXT
    return text


def build_nl2sql_messages(
    question: str,
    today: date | str = DATA_BASE_DATE,
    tables: list[str] | None = None,
) -> list[dict[str, str]]:
    """构建 NL2SQL 生成的 messages（system + user）。

    版本切换：环境变量 PROMPT_VERSION=v1|v2（默认 v1）。
    - v1 全 schema 注入；v2 Schema Linking 召回注入（tables 可复用召回结果）。
    返回格式符合 shared.llm.LLMProvider messages 契约。
    """
    version = os.getenv("PROMPT_VERSION", "v1").strip().lower()
    if version == "v2":
        return build_nl2sql_messages_v2(question, today, tables)
    return build_nl2sql_messages_v1(question, today)
