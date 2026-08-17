"""scm_biz 库与只读账号初始化脚本（W24 Day1）。

作用（幂等，可反复执行）：
1. `CREATE DATABASE IF NOT EXISTS scm_biz`（utf8mb4_unicode_ci）
2. `nl2sql_ro` 只读用户：仅 `GRANT SELECT ON scm_biz.*`
3. `FLUSH PRIVILEGES`

与 `deploy/initdb/01_create_ro_user.sql` 同源——SQL 版用于 compose 新数据卷首次初始化；
本脚本用于：
- 已存在数据卷的环境（本地：docker compose 后手动跑一次）
- CI（service MySQL 容器无法挂载 initdb——会污染工作区目录所有权导致 checkout 失败，
  改在 job 里显式调用本脚本）

用法：
  python -X utf8 scripts/init_biz_db.py            # 连 settings.biz_dsn 的宿主端口
  SCM_BIZ_DSN="mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4" \
    python scripts/init_biz_db.py                  # 显式覆盖 DSN
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

import asyncmy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.platform.settings import settings

# 只读账号（与 deploy/initdb/01_create_ro_user.sql 一致）
RO_USER = "nl2sql_ro"
RO_PASSWORD = "ro_pass_2026_dev"


async def main() -> None:
    dsn = settings.biz_dsn
    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 3306
    user = parts.username or "root"
    password = parts.password or "root123"
    print(f"init scm_biz + ro user @ {user}@{host}:{port}")

    # 1) 建库 + 建用户（root 连接，无库名）
    # 先查后建：IF NOT EXISTS 会触发 MySQL warning（1007 / 1396）污染 stderr，先查保证幂等且输出干净
    conn = await asyncmy.connect(host=host, port=port, user=user, password=password)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='scm_biz'")
            has_db = (await cur.fetchone())[0]
            if not has_db:
                await cur.execute(
                    "CREATE DATABASE scm_biz "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                print("  已创建 scm_biz 库")
            else:
                print("  scm_biz 库已存在，跳过")

            await cur.execute(
                f"SELECT COUNT(*) FROM mysql.user WHERE User='{RO_USER}' AND Host='%'"
            )
            has_user = (await cur.fetchone())[0]
            if not has_user:
                await cur.execute(
                    f"CREATE USER '{RO_USER}'@'%' IDENTIFIED BY '{RO_PASSWORD}'"
                )
                print(f"  已创建 {RO_USER} 用户")
            else:
                print(f"  {RO_USER} 用户已存在，跳过")

            await cur.execute(f"GRANT SELECT ON scm_biz.* TO '{RO_USER}'@'%'")
            await cur.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()

    # 2) 验证只读账号 SELECT 正常（连 scm_biz）
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        print("  只读账号 SELECT OK")
    finally:
        await engine.dispose()
    print("init_biz_db 完成（幂等，可重跑）")


if __name__ == "__main__":
    asyncio.run(main())
