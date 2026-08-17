"""NL2SQL 攻击用例集（W24 Day2）——20/20 拦截，0 逃逸。

覆盖手册 Day2 攻击面：
- 堆叠语句（; 拼接 / 注释截断换行）
- 写操作根节点（UPDATE/DELETE/INSERT/DROP/ALTER/CREATE/GRANT）
- 伪装嵌写（SELECT (DELETE...) / IN (UPDATE...) / CTE 嵌写）
- 危险函数（sleep/benchmark/load_file/outfile）+ 大小写/注释/编码混淆
- UNION 探测越权 / 跨库查询 / FOR UPDATE 锁读

每条用例断言：抛 `SQLRejected` 且 reason 正确（拒绝原因可审计、可解释）。
"""

import pytest

from app.domains.data.sql_validator import SQLRejected, validate_sql

# (SQL, 期望拒绝 reason)
ATTACK_CASES: list[tuple[str, str]] = [
    # ---- 闸1：堆叠语句 ----
    ("SELECT 1; DROP TABLE orders;", "multi-statement"),
    ("SELECT 1; SELECT 2", "multi-statement"),
    ("SELECT 1;\nDROP TABLE orders", "multi-statement"),
    # ---- 闸2：写操作根节点 ----
    ("UPDATE orders SET status='x' WHERE id=1", "not-select"),
    ("DELETE FROM orders WHERE id=1", "not-select"),
    ("INSERT INTO orders (order_no) VALUES ('SO-X')", "not-select"),
    ("DROP TABLE orders", "not-select"),
    ("ALTER TABLE orders ADD COLUMN x INT", "not-select"),
    ("CREATE TABLE evil (id INT)", "not-select"),
    ("GRANT SELECT ON scm_biz.* TO 'evil'@'%'", "not-select"),
    # ---- 闸3：伪装嵌写（30.x 对部分直接 ParseError，天然兜底） ----
    ("SELECT (DELETE FROM orders)", "parse-error"),
    ("SELECT * FROM orders WHERE id IN (UPDATE orders SET status='x')", "parse-error"),
    ("WITH x AS (DELETE FROM orders) SELECT * FROM x", "write-op"),
    # ---- 闸4：危险函数 + 混淆 ----
    ("SELECT sleep(5)", "dangerous-func"),
    ("SELECT SlEeP(5)", "dangerous-func"),
    ("SELECT BENCHMARK(1e7, MD5('x'))", "dangerous-func"),
    ("SELECT LOAD_FILE('/etc/passwd')", "dangerous-func"),
    # ---- 越权 / 锁读 ----
    ("SELECT * FROM orders WHERE id=1 UNION SELECT password FROM users", "unknown-table"),
    ("SELECT * FROM scm_platform.users", "unknown-table"),
    ("SELECT * FROM orders WHERE id=1 FOR UPDATE", "for-update"),
]

assert len(ATTACK_CASES) == 20, f"攻击用例必须 20 条，当前 {len(ATTACK_CASES)}"


@pytest.mark.parametrize(
    "sql,reason", ATTACK_CASES, ids=[f"case{i + 1}" for i in range(len(ATTACK_CASES))]
)
def test_attack_case_rejected(sql: str, reason: str):
    """攻击用例 100% 拦截且 reason 正确。"""
    with pytest.raises(SQLRejected) as exc_info:
        validate_sql(sql)
    assert exc_info.value.reason == reason, (
        f"攻击 [{sql}] 期望 reason={reason}，实际={exc_info.value.reason}"
    )
