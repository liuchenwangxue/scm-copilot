"""NL2SQL 只读沙箱执行器测试（W24 Day2）。

覆盖 Day2 验收：
- 正常查询返回 {sql, columns, rows, truncated, elapsed_ms}
- 行数上限：结果超过 MAX_ROWS → 截断 + truncated=True
- 结果字节上限：超过 MAX_RESULT_BYTES → 截断
- 3s 超时：`SELECT SLEEP` → QueryTimeoutError（防 sleep 拖库）
- 只读兜底：executor 直连 UPDATE → ExecutionError（DB 权限拒绝，ERROR 1142）
- 语法/表不存在错误 → ExecutionError（message 供自修复回喂）

依赖：MySQL 已起 + scm_biz 已 migrate + seed + nl2sql_ro（make init-biz-db）
标签：integration（CI 有 MySQL service + seed 步骤，会跑）
"""

import os

import pytest
import pytest_asyncio

# ★ 与 conftest 同策略：先写 env 再 import settings（settings 在 import 时读取）
os.environ.setdefault(
    "SCM_BIZ_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4",
)
os.environ.setdefault(
    "SCM_BIZ_RO_DSN",
    "mysql+asyncmy://nl2sql_ro:ro_pass_2026_dev@127.0.0.1:13306/scm_biz?charset=utf8mb4",
)

from app.domains.data.executor import (  # noqa: E402
    MAX_RESULT_BYTES,
    MAX_ROWS,
    ExecutionError,
    QueryTimeoutError,
    dispose_engine,
    execute_sql,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine():
    """每个用例后释放只读 engine（异步 fixture + function loop，见 conftest 教训）。"""
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_normal_query_returns_structured_result():
    res = await execute_sql("SELECT order_no, amount FROM orders LIMIT 3")
    assert res["error"] is None
    assert res["columns"] == ["order_no", "amount"]
    assert len(res["rows"]) == 3
    assert res["truncated"] is False
    assert res["elapsed_ms"] >= 0
    assert isinstance(res["rows"][0][0], str)  # order_no 是字符串


@pytest.mark.asyncio
async def test_row_limit_truncates():
    """行数上限：全表扫描（>200 行）→ 只取前 MAX_ROWS 行 + truncated=True。"""
    res = await execute_sql("SELECT order_no FROM orders")
    assert len(res["rows"]) == MAX_ROWS
    assert res["truncated"] is True


@pytest.mark.asyncio
async def test_max_rows_configurable():
    """max_rows 可配置（供调用方收紧）。"""
    res = await execute_sql("SELECT order_no FROM orders", max_rows=5)
    assert len(res["rows"]) == 5
    assert res["truncated"] is True


@pytest.mark.asyncio
async def test_result_bytes_truncates():
    """字节上限：多行大文本累计超限 → 截断（常数行 UNION，避免全表扫描超时）。"""
    sql = (
        "SELECT REPEAT('x', 60000) AS b UNION ALL "
        "SELECT REPEAT('y', 60000) UNION ALL "
        "SELECT REPEAT('z', 60000)"
    )
    res = await execute_sql(sql, max_bytes=100 * 1024)  # 第二行就累计 >100KB
    assert res["truncated"] is True
    # 超限行不加入结果集：第一行 60KB 被接收，第二行累计 120KB 超限触发截断
    assert len(res["rows"]) == 1


@pytest.mark.asyncio
async def test_timeout_raises_query_timeout():
    """3s 超时：SELECT SLEEP(5) → QueryTimeoutError（防 sleep 拖库）。"""
    with pytest.raises(QueryTimeoutError):
        await execute_sql("SELECT SLEEP(5)", timeout=1.0)


@pytest.mark.asyncio
async def test_syntax_error_raises_execution_error():
    """语法/表不存在 → ExecutionError（message 供自修复回喂，Day5 接 repair）。"""
    with pytest.raises(ExecutionError) as exc_info:
        await execute_sql("SELECT * FROM non_exist_table_xyz")
    assert "non_exist_table_xyz" in str(exc_info.value) or "table" in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_op_denied_by_ro_user():
    """只读兜底：直连 UPDATE 被 MySQL 拒绝（ERROR 1142）——闸若绕过，权限层仍兜底。"""
    with pytest.raises(ExecutionError) as exc_info:
        await execute_sql("UPDATE orders SET amount = 1 WHERE id = 1")
    assert "denied" in str(exc_info.value).lower() or "1142" in str(exc_info.value)


@pytest.mark.asyncio
async def test_decimal_normalized_to_float():
    """DECIMAL 结果规范化：Decimal → float（评测脚本/JSON 可序列化，Day3 依赖）。"""
    res = await execute_sql("SELECT amount FROM orders LIMIT 1")
    assert isinstance(res["rows"][0][0], float)


@pytest.mark.asyncio
async def test_datetime_normalized_to_str():
    """DATETIME 结果规范化：datetime → isoformat 字符串。"""
    res = await execute_sql("SELECT created_at FROM orders LIMIT 1")
    assert isinstance(res["rows"][0][0], str)


@pytest.mark.asyncio
async def test_audit_callback_receives_event():
    """审计钩子：执行成功发出事件（含 SQL 原文，取证可回放）。"""
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    await execute_sql("SELECT order_no FROM orders LIMIT 1", audit=sink)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "data:nl2sql:execute"
    assert "order_no" in ev["sql"]
    assert ev["status"] == "ok"
    assert ev["error"] is None
    assert ev["rows"] == 1
    assert ev["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_audit_callback_receives_error_event():
    """审计钩子：执行失败也发事件（status=error，供排查）。"""
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    with pytest.raises(ExecutionError):
        await execute_sql("SELECT * FROM non_exist_table_xyz", audit=sink)
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error"] is not None


@pytest.mark.asyncio
async def test_audit_failure_does_not_block():
    """审计回调抛错不阻断查询结果（审计系统故障不拖垮主流程）。"""
    async def bad_sink(event: dict) -> None:  # noqa: ARG001
        raise RuntimeError("audit db down")

    res = await execute_sql("SELECT order_no FROM orders LIMIT 1", audit=bad_sink)
    assert res["error"] is None
    assert len(res["rows"]) == 1
