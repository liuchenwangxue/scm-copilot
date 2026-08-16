"""SCM Copilot 平台入口（W23）。

lifespan：建 async engine + session 工厂（SQLAlchemy 2.0 async / asyncmy）
- GET /health：存活探针 + DB 连通状态（deploy compose healthcheck 用）
- 白名单端点：/health /docs /metrics（其余路由挂全局 JWT，见 global_auth）

W23 Day3 新增：
- 审计中间件：非 GET 请求落 audit_logs
- /api/auth/* 认证路由（login / refresh / logout / me）
- 全局 JWT 门禁：除白名单外所有请求校验 Bearer access token（含吊销名单 + 用户存活）
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.platform import auth, rbac, schemas
from app.platform.audit import AuditMiddleware
from app.platform.models import User
from app.platform.settings import settings

# 全局放行路径（不校验 JWT）——对齐《02》4 节"放行清单：/health /docs /metrics"
WHITELIST_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/metrics")

# 认证端点自身不需要 access token（login 换取、refresh 用 refresh token），全局门禁跳过
OPEN_AUTH_PATHS = ("/api/auth/login", "/api/auth/refresh")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(settings.platform_dsn, pool_pre_ping=True)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield
    await engine.dispose()


async def global_auth(request: Request) -> User | None:
    """全局 JWT 门禁（FastAPI 全局依赖，所有路由生效）。

    - 白名单路径 / 认证端点 → 放行（返回 None）
    - 其余路径 → 手动解析 Bearer 头 → 完整 JWT 校验（签名/类型/吊销/用户存活）
      失败抛 401；成功返回用户（丢弃，门禁只保证"有有效身份"）
    """
    path = request.url.path
    if path.startswith(WHITELIST_PREFIXES) or path.startswith(OPEN_AUTH_PATHS):
        return None

    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        credentials: HTTPAuthorizationCredentials | None = None
    else:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:])

    factory = request.app.state.session_factory
    async with factory() as session:
        return await auth.get_current_user(credentials=credentials, session=session)


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
    # 全局门禁：除白名单外，所有请求必须有有效 access token
    dependencies=[Depends(global_auth)],
)

# 审计中间件：非 GET 写操作全覆盖（登录/刷新/登出端点自身已落账，中间件跳过）
app.add_middleware(AuditMiddleware)


# ==================== 路由 ====================


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


@app.get("/api/auth/me", response_model=schemas.UserOut, tags=["auth"])
async def me(current: User = Depends(rbac.require_permission("kb:chat"))) -> schemas.UserOut:
    """返回当前登录用户（权限来自 JWT claims，不查库）——受保护端点示例。"""
    return schemas.UserOut(
        id=current.id,
        username=current.username,
        tenant_id=current.tenant_id,
        permissions=sorted(rbac.current_permissions(current)),
    )


app.include_router(auth.router)
