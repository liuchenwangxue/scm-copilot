"""sql_validator 四道闸单测（W24 Day2）。

覆盖 Day2 验收：
- 四道闸每闸 ≥5 单测分支（堆叠/写根/伪装嵌写/危险函数）
- 混淆变体：大小写 SlEeP / 注释截断 / UNION 探测 / 锁读
- 合法查询（join/聚合/子查询/CTE/UNION ALL/已带 LIMIT/OFFSET）0 误拦
- 白名单越权拦截：非业务表 / 跨库查询
- 强制 LIMIT：无 LIMIT 才加，不重复包一层

纯逻辑单测，无需数据库（sqlglot AST 解析即可）。
"""

import pytest

from app.domains.data.sql_validator import (
    SCM_BIZ_TABLES,
    SQLRejected,
    validate_sql,
)


def rejected_reason(sql: str) -> str:
    """返回 SQL 被拒绝的 reason；未拒绝则抛断言失败。"""
    with pytest.raises(SQLRejected) as exc_info:
        validate_sql(sql)
    return exc_info.value.reason


# ==================== 闸1：防堆叠（multi-statement） ====================


class TestGate1MultiStatement:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE orders;",
            "SELECT 1; SELECT 2",
            "SELECT 1; DELETE FROM orders WHERE id=1",
            "SELECT 1; -- 注释\nDROP TABLE orders",  # 分号堆叠 + 注释换行（3 条）
            "SELECT * FROM orders; SELECT * FROM products",
        ],
    )
    def test_multi_statement_rejected(self, sql: str):
        assert rejected_reason(sql) == "multi-statement"

    def test_trailing_semicolon_is_single(self):
        """单个尾分号算单语句（MySQL 习惯），不误拦。"""
        out = validate_sql("SELECT 1;")
        assert "SELECT 1" in out

    def test_comment_then_newline_drop(self):
        """注释截断变体：`; -- 注释` 后换行再 DROP——3 条语句，闸1 拦截。

        注意 sqlglot 30.x：`SELECT 1 -- 注释\\nDROP`（无分号）解析抛 ParseError
        （同样拒绝，reason=parse-error）；带分号则明确是 3 条 → multi-statement。
        两种形式都 100% 拦截（攻击意图的 DROP 永远不会被放行）。
        """
        assert rejected_reason("SELECT 1; -- 注释\nDROP TABLE orders") == "multi-statement"
        assert rejected_reason("SELECT 1 -- 注释\nDROP TABLE orders") in (
            "multi-statement",
            "parse-error",
        )


# ==================== 闸2：根节点仅 SELECT / UNION ====================


class TestGate2RootType:
    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE orders SET status='cancelled' WHERE id=1",
            "DELETE FROM orders WHERE id=1",
            "DROP TABLE orders",
            "CREATE TABLE evil (id INT)",
            "ALTER TABLE orders ADD COLUMN x INT",
            "INSERT INTO orders (order_no) VALUES ('SO-X')",
            "GRANT SELECT ON scm_biz.* TO 'evil'@'%'",
        ],
    )
    def test_write_root_rejected(self, sql: str):
        assert rejected_reason(sql) == "not-select"

    def test_union_root_allowed(self):
        """UNION / UNION ALL 根节点放行。"""
        for sql in ["SELECT 1 UNION SELECT 2", "SELECT 1 UNION ALL SELECT 2"]:
            out = validate_sql(sql)
            assert "UNION" in out

    def test_plain_select_root_allowed(self):
        out = validate_sql("SELECT 1")
        assert out.startswith("SELECT")


# ==================== 闸3：子句级伪装嵌写 ====================


class TestGate3EmbeddedWrite:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (DELETE FROM orders)",  # 30.x 解析失败 → parse-error（天然兜底）
            "SELECT * FROM orders WHERE id IN (UPDATE orders SET status='x')",
            "WITH x AS (DELETE FROM orders) SELECT * FROM x",  # CTE 嵌写 → write-op
            "WITH x AS (UPDATE orders SET status='x') SELECT * FROM x",
        ],
    )
    def test_embedded_write_rejected(self, sql: str):
        reason = rejected_reason(sql)
        assert reason in ("parse-error", "write-op"), f"unexpected reason: {reason}"

    def test_select_wrapped_in_subquery_without_write_allowed(self):
        """子查询内纯 SELECT 不误拦。"""
        out = validate_sql(
            "SELECT * FROM (SELECT region, COUNT(*) FROM orders GROUP BY region) sub"
        )
        assert "sub" in out


# ==================== 闸4：危险函数黑名单 ====================


class TestGate4DangerousFuncs:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT sleep(5)",
            "SELECT SlEeP(5)",  # 大小写混淆
            "SELECT BENCHMARK(1e7, MD5('x'))",  # benchmark 拖库
            "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT * FROM orders WHERE id = sleep(1)",
            "SELECT SLEEP/**/(5)",  # 注释混淆
        ],
    )
    def test_dangerous_func_rejected(self, sql: str):
        assert rejected_reason(sql) == "dangerous-func"

    def test_safe_aggregate_funcs_allowed(self):
        """聚合函数 COUNT/SUM/AVG/ROUND/MIN/MAX 不误拦。"""
        out = validate_sql(
            "SELECT region, COUNT(*) AS cnt, ROUND(AVG(amount),2) AS avg_amt "
            "FROM orders GROUP BY region"
        )
        assert "COUNT" in out and "ROUND" in out


# ==================== 白名单越权拦截（UNION 探测 / 跨库） ====================


class TestWhitelist:
    def test_union_probe_to_users_rejected(self):
        """UNION 探测到非业务表（users）→ 越权拦截。"""
        assert (
            rejected_reason("SELECT * FROM orders WHERE id=1 UNION SELECT password FROM users")
            == "unknown-table"
        )

    def test_cross_db_query_rejected(self):
        """跨库查询 scm_platform.* → 拒绝。"""
        assert rejected_reason("SELECT * FROM scm_platform.users") == "unknown-table"

    def test_unknown_table_rejected(self):
        assert rejected_reason("SELECT * FROM secrets") == "unknown-table"

    def test_biz_tables_all_allowed(self):
        """六表白名单全部放行（含 join 组合）。"""
        for table in sorted(SCM_BIZ_TABLES):
            assert validate_sql(f"SELECT * FROM {table}")  # 不抛即为过

    def test_cte_name_not_mistaken_as_table(self):
        """CTE 名不当作真实表（白名单检查跳过 CTE 名）。"""
        out = validate_sql("WITH c AS (SELECT * FROM orders) SELECT region FROM c")
        assert "WITH" in out


# ==================== 合法查询 0 误拦 ====================


class TestLegitQueries:
    @pytest.mark.parametrize(
        "sql",
        [
            # 单表过滤 + 排序
            "SELECT order_no, amount FROM orders WHERE region='华东' AND status='shipped' "
            "ORDER BY created_at DESC",
            # 近 7 天时间窗（手册坑：口径写死在 few-shot）
            "SELECT COUNT(*) FROM orders WHERE created_at >= CURDATE() - INTERVAL 7 DAY",
            # 两表 join：订单 + 供应商
            "SELECT o.order_no, s.name FROM orders o JOIN suppliers s ON o.supplier_id = s.id "
            "WHERE o.status='shipped'",
            # 订单 + 明细聚合
            "SELECT o.order_no, SUM(i.amount) FROM orders o "
            "JOIN order_items i ON o.order_no = i.order_no GROUP BY o.order_no",
            # 商品 + 库存低库存
            "SELECT p.sku, p.name FROM products p JOIN inventory inv ON p.id = inv.product_id "
            "WHERE inv.qty < inv.safety_qty",
            # 聚合 + HAVING
            "SELECT region, COUNT(*) AS cnt FROM orders GROUP BY region HAVING cnt > 10",
            # 子查询（标量子查询）
            "SELECT order_no, amount FROM orders WHERE amount = (SELECT MAX(amount) FROM orders)",
            # UNION ALL 合并合法查询（根节点 Union，闸2 放行）
            "SELECT region FROM orders UNION ALL SELECT region FROM suppliers",
            # 已带 LIMIT（不重复包一层）
            "SELECT order_no FROM orders LIMIT 10",
            # LIMIT + OFFSET（方言解析，read=mysql 必传的验证点）
            "SELECT order_no FROM orders ORDER BY id LIMIT 10 OFFSET 20",
            # DISTINCT
            "SELECT DISTINCT region FROM orders",
        ],
    )
    def test_legit_sql_not_rejected(self, sql: str):
        out = validate_sql(sql)
        assert out  # 不抛即为过；断言有返回

    def test_limit_not_doubled(self):
        """已带 LIMIT 不重复包一层。"""
        out = validate_sql("SELECT order_no FROM orders LIMIT 10")
        assert out.count("LIMIT") == 1

    def test_missing_limit_gets_injected(self):
        """无 LIMIT 时自动注入 LIMIT 200（兜底防全表扫描）。"""
        out = validate_sql("SELECT order_no FROM orders")
        assert "LIMIT 200" in out


# ==================== 锁读拒绝（FOR UPDATE） ====================


class TestLockRead:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE id=1 FOR UPDATE",
            "SELECT * FROM orders WHERE id=1 LOCK IN SHARE MODE",
        ],
    )
    def test_lock_read_rejected(self, sql: str):
        assert rejected_reason(sql) == "for-update"


# ==================== parse-error 天然兜底 ====================


class TestParseError:
    @pytest.mark.parametrize(
        "sql",
        [
            "",  # 空串（sqlglot 解析为 1 个 None 节点 → not-select）
            "SELECT * FROM",  # 语法残缺
            "SLEEP(5)",  # 无 SELECT 前缀（Anonymous 根 → not-select）
            "SELECT \\x73leep(5)",  # 编码混淆（非法标识符）
            "SELECT 0x736c656570(5)",  # 十六进制编码混淆
        ],
    )
    def test_unparseable_rejected(self, sql: str):
        reason = rejected_reason(sql)
        # 空串/SLEEP(5) → not-select；其余 parse-error；无分号注释 → 也可能 multi-statement
        assert reason in ("parse-error", "multi-statement", "not-select")
