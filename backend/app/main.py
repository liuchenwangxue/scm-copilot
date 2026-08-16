"""SCM Copilot 平台入口（W23 Day1 最小骨架）。

- lifespan：建 async engine + session 工厂（SQLAlchemy 2.0 async / asyncmy）
- GET /health：存活探针 + DB 连通状态（deploy compose healthcheck 用）
- 白名单端点：/health /docs /metrics（其余路由 W23 Day3 起挂 JWT）
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.platform.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★ 手册坑：AsyncMySQLSaver 等需在 lifespan 里 setup，别在 import 时连库
    engine = create_async_engine(settings.platform_dsn, pool_pre_ping=True)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """存活探针：返回服务与数据库连通状态。"""
    db_status = "up"
    try:
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "down"
    return {"status": "ok" if db_status == "up" else "degraded", "db": db_status}
