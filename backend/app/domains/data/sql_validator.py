"""NL2SQL 安全四道闸（W24 Day2）——sqlglot AST 确定性校验，安全边界不依赖模型"听话"。

对应《03》1.1 节权威实现 + 手册 Day2 坑位：

1. **闸1 防堆叠**：`len(sqlglot.parse(sql, read="mysql")) == 1`，`;` 拼接多语句直接拒绝。
2. **闸2 仅 SELECT**：根节点必须是 `exp.Select / exp.Union`（含 UNION ALL，天然覆盖）。
3. **闸3 写操作拦截**：`tree.find(Insert/Update/Delete/Drop/Alter/Create/Grant)`——子句级兜底，
   拦截 `SELECT (DELETE FROM ...)`、`WITH x AS (DELETE ...) SELECT * FROM x` 等伪装嵌写。
4. **闸4 危险函数黑名单**：`sleep / benchmark / load_file / outfile`（大小写/注释混淆归一后匹配）。
5. **兜底强制 LIMIT**：无 `LIMIT` 才加，别重复包一层；Union 根节点用 `set("limit", ...)`（30.x
   `tree.limit()` 对 Union 无效）。
6. **额外防线**：
   - `FOR UPDATE / LOCK IN SHARE MODE`（`exp.Lock`）锁读拒绝——读锁语义不在只读分析范围内；
   - **表名白名单**（业务库六表，可配置）——UNION 探测到 `users` 等非业务表、跨库查询直接拒绝
     （手册 Day2 攻击用例含 UNION 探测，四道闸拦不住 SELECT UNION，需白名单补越权拦截）。

拒绝时抛 `SQLRejected(reason)`，reason 落审计（取证可回放）。

实现坑（实测 sqlglot 30.x）：
- `read="mysql"` 必传，否则 `LIMIT 200 OFFSET 10` 等方言解析漂移。
- 函数名取法：Anonymous 类（`sleep` 等非内置函数）用 `.name`；命名函数（如 `MD5`）用 `.sql_name()`。
- CTE 名会出现在 `find_all(exp.Table)`（`WITH c AS (...) SELECT * FROM c` 的 `c`），白名单检查须排除。
- `SELECT (DELETE FROM ...)` 等在 30.x 直接抛 `ParseError` → 一律按 `parse-error` 拒绝（天然兜底）。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# 闸2：允许的根节点（UNION ALL 也是 Union，天然覆盖）
ALLOWED_ROOT = (exp.Select, exp.Union)

# 闸3：子句级写操作类型
FORBIDDEN_CLAUSE = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Grant,
)

# 闸4：危险函数黑名单（拉库/读文件/写文件）
FORBIDDEN_FUNCS = {"sleep", "benchmark", "load_file", "outfile"}

# 表名白名单：业务库 scm_biz 六表（越权拦截；可配置覆盖）
SCM_BIZ_TABLES = {
    "orders",
    "order_items",
    "products",
    "suppliers",
    "inventory",
    "shipments",
}

DEFAULT_MAX_ROWS = 200


class SQLRejected(Exception):
    """SQL 被安全闸拒绝。

    reason 是机器可读的拒绝原因（落审计用），message 供前端/日志展示。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"SQL rejected: {reason}")
        self.reason = reason


def _func_name(func: exp.Func) -> str:
    """提取函数名：Anonymous（sleep 等）用 .name，命名函数（MD5 等）用 .sql_name()。"""
    if isinstance(func, exp.Anonymous):
        return func.name
    return func.sql_name()


def _collect_cte_names(tree: exp.Expression) -> set[str]:
    """收集 WITH 子句的 CTE 名（白名单检查需排除，否则合法 CTE 被误拒）。

    注意：sqlglot AST 中 WITH 子句挂在 `args["with_"]`（带下划线），非 `args["with"]`。
    """
    with_clause = tree.args.get("with_")
    if not with_clause:
        return set()
    return {cte.alias for cte in with_clause.expressions}


def validate_sql(
    sql: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    allowed_tables: set[str] | None = None,
) -> str:
    """对 LLM 生成的 SQL 做确定性安全校验，通过则返回（强制 LIMIT 后的）规范化 SQL。

    - 校验失败抛 `SQLRejected(reason)`，reason 为机器可读原因；
    - 通过后返回的 SQL 可直接交给只读沙箱执行（双保险：闸 + nl2sql_ro 权限）。
    """
    tables = allowed_tables if allowed_tables is not None else SCM_BIZ_TABLES

    # ---- 闸1：单语句（防 ; 堆叠；解析失败也一律拒绝）----
    try:
        stmts = sqlglot.parse(sql, read="mysql")
    except ParseError as exc:
        raise SQLRejected("parse-error") from exc
    if len(stmts) != 1:
        raise SQLRejected("multi-statement")
    tree = stmts[0]

    # ---- 闸2：根节点仅 SELECT / UNION（含 UNION ALL；CTE 根节点仍是 Select）----
    if not isinstance(tree, ALLOWED_ROOT):
        raise SQLRejected("not-select")

    # ---- 锁读拒绝：FOR UPDATE / LOCK IN SHARE MODE（读锁不在只读分析范围内）----
    if tree.find(exp.Lock):
        raise SQLRejected("for-update")

    # ---- 闸3：子句级写操作拦截（伪装嵌写：SELECT (DELETE ...) / WITH x AS (DELETE ...)）----
    if tree.find(*FORBIDDEN_CLAUSE):
        raise SQLRejected("write-op")

    # ---- 闸4：危险函数黑名单（大小写/注释混淆归一后匹配）----
    for func in tree.find_all(exp.Func):
        if _func_name(func).lower() in FORBIDDEN_FUNCS:
            raise SQLRejected("dangerous-func")

    # ---- 表名白名单（越权拦截：非业务表 / 跨库查询）----
    if tables is not None:
        cte_names = _collect_cte_names(tree)
        for tab in tree.find_all(exp.Table):
            if tab.name in cte_names:
                continue  # CTE 名不是真实表，跳过
            if tab.name not in tables:
                raise SQLRejected("unknown-table")

    # ---- 兜底：强制 LIMIT（无 limit 才加；Union 根节点用 set，Select 也兼容）----
    if tree.args.get("limit") is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return tree.sql(dialect="mysql")
