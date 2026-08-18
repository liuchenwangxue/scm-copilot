"""NL2SQL 错误自修复（W24 Day5）——执行报错/可修复闸拒 → 带错回喂重写 ≤2 次 → 降级话术。

对应《W24学习执行手册》Day5 上午 +《03》1.4 节：
- 捕获执行错误：executor 已把 ProgrammingError/OperationalError 归一如 `ExecutionError`
  （message 含 errno 与报错原文，如 `Unknown column 'amunt'` / `Table doesn't exist`）；
- 修复 prompt：原问题 + 原 SQL + 报错原文 → "仅修复语法/列名/表名错误，不改业务语义"；
- 循环 ≤2 次（`MAX_REPAIR_ATTEMPTS`）；修复后的 SQL **仍必须过四道闸**（安全不豁免）；
- 两次失败 → 降级话术："暂时无法生成有效查询，建议：{改写建议}"——**不硬答**。

修复触发面（graph 路由决定，见 route_after_validate / route_after_execute）：
1. execute 报错（state.error 非空）→ 修复；
2. 可修复闸拒（`REPAIRABLE_REASONS`：parse-error 语法残缺 / unknown-table 表名写错）→ 修复；
   安全类闸拒（write-op / not-select / multi-statement / dangerous-func / for-update）→ **永不修复**
   （首次 → reject_node 拒答；修复循环中出现 → 直接降级，安全不豁免）。

mock 双路径（手册坑"mock 测链路、real 测效果"）：
- provider=mock → `MockRepairGenerator`（确定性：评测集问题返回 gold SQL，测链路救回；未命中/`fail` 模式测降级）；
- provider=real → `build_repair_messages` + 模型池（真实修复能力，救回率只算 real）。

对外接口：
    clean_sql(raw) -> str                            # LLM 输出清洗（graph/评测共用）
    build_repair_messages(question, sql, failure, today) -> list[dict]
    async repair_sql(question, sql, failure, today) -> str   # 修复一次（mock/real 双路径）
"""

from __future__ import annotations

import re
from datetime import date

import sqlglot
from sqlglot import exp

from app.domains.data.mock_repair import MockRepairGenerator
from app.domains.data.prompts import DATA_BASE_DATE
from app.domains.data.schema_linker import RELATIONSHIPS_TEXT, TABLE_DDL_COMPACT
from app.shared.llm import get_provider

# 修复循环上限：≤2 次（两次失败 → 降级话术，不硬答）
MAX_REPAIR_ATTEMPTS = 2

# 可修复闸拒原因：语法残缺 / 表名写错（模型"诚实犯错"可救）；
# 其余安全类拒绝（write-op/not-select/multi-statement/dangerous-func/for-update）永不修复。
REPAIRABLE_REASONS = frozenset({"parse-error", "unknown-table"})


def _tables_in_sql(sql: str) -> list[str]:
    """从坏 SQL 提取涉及的业务表（按出现顺序去重）；解析失败返回空（调用方回退全表）。

    仅匹配六表白名单（CTE/子查询别名天然排除——不是真实业务表名）。
    """
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001  # 语法已坏（parse-error 场景）→ 回退全表
        return []
    tables: list[str] = []
    for tab in tree.find_all(exp.Table):
        if tab.name in TABLE_DDL_COMPACT and tab.name not in tables:
            tables.append(tab.name)
    return tables

# LLM 输出的 SQL 清洗（graph.py 原 `_clean_sql` 上移为公共函数，graph/repair 共用）
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.S)


def clean_sql(raw: str) -> str:
    """清洗 LLM 输出：去 ```sql ... ``` 代码块围栏 / 首尾空白 / 尾分号。"""
    m = _SQL_FENCE_RE.search(raw or "")
    text = m.group(1).strip() if m else (raw or "").strip()
    return text.rstrip(";").strip()


def _today_param(today: date | str) -> date:
    if isinstance(today, str):
        return date.fromisoformat(today)
    return today


def build_repair_messages(
    question: str,
    sql: str,
    failure: str,
    today: date | str = DATA_BASE_DATE,
) -> list[dict[str, str]]:
    """修复 prompt：原问题 + 原 SQL + 报错原文 → 仅修语法/列名/表名，不改业务语义。

    关键约束（手册 Day5 坑）：
    - **不改语义**：不得改动过滤条件/分组聚合/排序/LIMIT，不得新增条件；
      否则模型会把"查不了"改成"能跑但答非所问"的 SQL（比报错更危险）；
    - **安全不豁免**：修复产物仍是 SELECT，严禁变成任何写操作；
    - 报错原文（errno + message）必须完整给到，模型才可能定位根因。
    """
    today = _today_param(today)
    system = (
        "你是 MySQL 8 专家，负责修复一条有问题的只读查询 SQL。\n"
        "硬性规则：\n"
        f"1. 只修复【语法错误、错误的列名、错误的表名】，今日日期为 {today.isoformat()}。\n"
        "2. **严禁改变查询的业务语义**：不得改动 WHERE 过滤条件、**不得改动 JOIN 子句的"
        "任何部分（包括关联列 ON ... = ...）**、GROUP BY/聚合逻辑、ORDER BY 排序、LIMIT；"
        "不得新增或删除条件、不得增删 JOIN。\n"
        "   只修复【报错信息明确指向】的错误（缺失的语法/报错中提到的列或表）；"
        "   其它部分即使看起来可疑但未在报错中出现，也必须原样保留。\n"
        "3. 保持只读：输出必须是一条 SELECT；严禁出现 INSERT/UPDATE/DELETE/DDL 等任何写操作。\n"
        "4. 表名/列名必须与【相关表结构】清单【逐字准确】一致（严禁单复数/变体/同义猜测，"
        "例如发货表是 shipments 不是 shipment、订单号列是 order_no 不是 order_id）。\n"
        "5. 只输出修复后的一条 SQL，不要输出解释、Markdown 代码块或分号以外的内容。"
    )
    # ★ 注入相关表结构 + 关联关系：模型必须能查到"真实存在的列/关联键"，禁止凭空猜
    tables = _tables_in_sql(sql) or list(TABLE_DDL_COMPACT)
    schema_text = "\n\n".join(TABLE_DDL_COMPACT[t] for t in tables)
    schema_text += "\n\n" + RELATIONSHIPS_TEXT  # 关联键（JOIN ON 正确写法的权威来源）
    user = (
        f"## 相关表结构（合法表与列的权威清单）\n{schema_text}\n\n"
        f"## 原始问题\n{question}\n\n"
        f"## 有问题的 SQL\n{sql}\n\n"
        f"## 报错信息 / 校验失败原因\n{failure}\n\n"
        "## 要求\n只输出修复后的一条 SELECT 语句。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def repair_sql(
    question: str,
    sql: str,
    failure: str,
    today: date | str = DATA_BASE_DATE,
) -> str:
    """修复一次：带错误上下文重写 SQL（graph.repair_node 调用）。

    - provider=mock：`MockRepairGenerator` 确定性修复（gold=评测集 gold SQL / fail=原样）；
    - provider=real：`build_repair_messages` → 模型池生成 → 清洗。
    """
    provider = get_provider()
    if provider.name == "mock":
        return MockRepairGenerator().generate(question, sql)
    messages = build_repair_messages(question, sql, failure, today)
    raw = await provider.generate(messages, max_tokens=1024, temperature=0.0)
    return clean_sql(raw)
