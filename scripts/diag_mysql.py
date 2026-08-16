"""W23-D1 诊断脚本：验证 MySQL 连通性 + 时区 + 字符集（CI 复现用）。

用法：python scripts/diag_mysql.py [DSN]
默认 DSN 指向 mysql:3306（GitHub Actions service container 场景）。
"""
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DSN = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "mysql+asyncmy://root:root123@mysql:3306/scm_platform?charset=utf8mb4"
)


async def main() -> None:
    print(f"DSN: {DSN}")
    engine = create_async_engine(DSN)
    try:
        async with engine.connect() as conn:
            print("SELECT 1 =", await conn.scalar(text("SELECT 1")))
            print(
                "tz_offset =",
                await conn.scalar(text("SELECT TIMESTAMPDIFF(HOUR, UTC_TIMESTAMP(), NOW())")),
            )
            print(
                "charset =",
                await conn.scalar(text("SELECT @@character_set_server")),
            )
    except Exception as e:  # noqa: BLE001
        print("CONN FAIL:", repr(e))
        raise
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
