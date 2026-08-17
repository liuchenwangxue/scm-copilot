"""业务库 scm_biz 的独立 Alembic 迁移环境（W24 Day1）。

设计决策（对应手册 Day1 坑）：
- 独立迁移链：与 platform 链完全隔离（独立 ini + 独立 versions/ 目录），
  避免共享版本树导致 `upgrade head` 交叉应用两个库的表（更稳妥）
- 连接串来源：app.platform.settings.settings.biz_dsn（env 驱动：SCM_BIZ_DSN）
- target_metadata：app.domains.data.models_biz.BizBase.metadata
- 使用 `alembic -c alembic_biz.ini upgrade head` 运行（在 backend/ 目录下）
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 确保 backend 在 import path
from app.domains.data import models_biz  # noqa: F401  # 注册所有表到 metadata
from app.domains.data.models_biz import BizBase
from app.platform.settings import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 连接串唯一来源 = settings（env 覆盖：SCM_BIZ_DSN）
config.set_main_option("sqlalchemy.url", settings.biz_dsn)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = BizBase.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
