"""NL2SQL 只读沙箱执行器（W24 Day2）——`nl2sql_ro` 账号 + 三重资源约束。

设计（对应《W24学习执行手册》Day2 下午 + 纵深防御叙事）：
- **独立 engine**：与平台库隔离，只连业务库 `scm_biz`（最小权限，横向移动面收敛）；
- **3s 超时**：`asyncio.wait_for` 包裹连接执行（防 sleep 拖库/慢查询占用连接池）；
- **行数上限**：结果集最多 `MAX_ROWS`（200）行，超出截断并标记 `truncated=True`；
- **结果集截断**：序列化字节 > `MAX_RESULT_BYTES`（1MB）时停止取行，防大结果拖垮响应；
- **审计**：执行事件（SQL 原文 + 结果摘要/错误）写 `audit_logs`（取证可回放）。

安全模型：SQL 必须先过 `sql_validator.validate_sql` 四道闸，再由本执行器在只读账号下执行——
即使闸有未知绕过，MySQL 权限层兜底拒绝写操作（ERROR 1142，Day1 已验证）。

对外返回结构（供结果表格化/评测脚本复用）：
    {
      "sql": str,                    # 实际执行的 SQL（已强制 LIMIT）
      "columns": list[str],          # 列名
      "rows": list[list],            # 行数据（类型已规范化：Decimal→float, datetime→isoformat）
      "truncated": bool,             # 是否因行数/字节上限被截断
      "elapsed_ms": float,
      "error": str | None,
    }
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domains.data.config import EXEC_TIMEOUT_SECONDS, MAX_RESULT_BYTES, MAX_ROWS
from app.platform.settings import settings

logger = logging.getLogger("scm.data.executor")

# 三重资源约束（★ W27-D6 B6：常量收敛到 data/config.py 单一来源；
# 模块属性保留，既有 `from executor import MAX_ROWS` 引用不破）

# 审计回调：{event, sql, status, error, elapsed_ms, rows} → 由调用方注入写 audit_logs 的实现
# （域间解耦：data 域不 import platform 内部模块，只依赖这个回调契约）
AuditEvent = dict[str, Any]
_AuditSink = Callable[[AuditEvent], Awaitable[None]]



class ExecutionError(Exception):
    """执行失败（超时 / 语法错误 / 只读权限拒绝等），携带 message 供自修复回喂。"""


class QueryTimeoutError(ExecutionError):
    """执行超过 3s 超时。"""


class _ExecutorEngine:
    """`nl2sql_ro` 只读连接池（模块级单例，与平台 engine 隔离）。"""

    _engine: AsyncEngine | None = None

    @classmethod
    def get(cls) -> AsyncEngine:
        if cls._engine is None:
            cls._engine = create_async_engine(
                settings.biz_ro_dsn,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=5,
                pool_recycle=3600,
            )
        return cls._engine

    @classmethod
    async def dispose(cls) -> None:
        if cls._engine is not None:
            await cls._engine.dispose()
            cls._engine = None


def _normalize(value: Any) -> Any:
    """结果值类型规范化：Decimal→float、datetime/date→isoformat，保证 JSON 可序列化。

    mypy 坑：`date.isoformat()` 无 `sep` 参数（仅 datetime 有），分开调用。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _result_bytes(row: Sequence[Any]) -> int:
    return sum(len(str(v)) for v in row)


async def execute_sql(
    sql: str,
    *,
    timeout: float = EXEC_TIMEOUT_SECONDS,
    max_rows: int = MAX_ROWS,
    max_bytes: int = MAX_RESULT_BYTES,
    audit: _AuditSink | None = None,
) -> dict[str, Any]:
    """在只读沙箱执行 SQL，返回结构化结果（见模块 docstring）。

    - 超时抛 `QueryTimeoutError`；语法/权限等 DB 错误抛 `ExecutionError`（message 可回喂自修复）。
    - 行数/字节超限截断但不报错，返回 `truncated=True`。
    - `audit`：可选审计回调（域间解耦：data 域不 import platform 内部模块，由调用方注入
      写 `audit_logs` 的实现，事件含 SQL 原文 + 结果/错误摘要，取证可回放）。
    """
    engine = _ExecutorEngine.get()
    start = time.perf_counter()
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    status = "ok"
    error_msg: str | None = None

    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                result = await conn.execute(text(sql))
                columns = list(result.keys())
                total_bytes = 0
                for row in result:
                    normalized = [_normalize(v) for v in row]
                    total_bytes += _result_bytes(normalized)
                    if len(rows) >= max_rows or total_bytes > max_bytes:
                        truncated = True
                        break
                    rows.append(normalized)
    except TimeoutError as exc:  # asyncio.timeout() 超时抛内置 TimeoutError
        status = "timeout"
        error_msg = f"query timeout after {timeout:.1f}s"
        raise QueryTimeoutError(error_msg) from exc
    except Exception as exc:  # noqa: BLE001  # DB 错误（语法/权限/表不存在）统一转 ExecutionError
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"
        raise ExecutionError(error_msg) from exc
    finally:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        if audit is not None:
            try:
                await audit(
                    {
                        "event": "data:nl2sql:execute",
                        "sql": sql,
                        "status": status,
                        "error": error_msg,
                        "elapsed_ms": elapsed_ms,
                        "rows": len(rows),
                    }
                )
            except Exception:  # noqa: BLE001  # 审计失败不阻断主流程（与平台 AuditMiddleware 同纪律）
                logger.exception("audit write failed for nl2sql execute")

    return {
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "error": None,
    }


async def dispose_engine() -> None:
    """关闭只读 engine（测试/优雅停机用）。"""
    await _ExecutorEngine.dispose()
