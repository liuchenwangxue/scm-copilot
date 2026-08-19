"""SCM Copilot 平台入口（W23）。

lifespan：建 async engine + session 工厂（SQLAlchemy 2.0 async / asyncmy）
- GET /health：存活探针 + DB 连通状态（deploy compose healthcheck 用）
- 白名单端点：/health /docs /metrics（其余路由挂全局 JWT，见 global_auth）

W23 Day3：审计中间件 + /api/auth/* 认证路由 + 全局 JWT 门禁
W23 Day4：双域并入——挂载 /api/kb（知识问答域，承 stage3-a）与
          /api/ops（业务操作域，承 stage3-b），旧 109 项回归保持全绿
"""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import MutableHeaders

from app.domains.admin import apikey_api as admin_apikey_api
from app.domains.admin import scheduler_api as admin_scheduler_api
from app.domains.data import router as data_router
from app.domains.kb import router as kb_router
from app.domains.ops import router as ops_router
from app.platform import auth, rbac, schemas
from app.platform.apikeys import authenticate_credentials
from app.platform.audit import AuditMiddleware
from app.platform.errors import Err, ErrorCode, register_error_handlers
from app.platform.models import User
from app.platform.scheduler import PlatformScheduler
from app.platform.settings import settings
from app.shared.obs.metrics import MetricsMiddleware
from app.shared.obs.metrics import render as render_metrics

# 全局放行路径（不校验 JWT）——对齐《02》4 节"放行清单：/health /docs /metrics"
WHITELIST_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/metrics")

# 认证端点自身不需要 access token（login 换取、refresh 用 refresh token），全局门禁跳过
# ★ W25 Day4：API 版本化后全局放行路径跟随 /api/v1 前缀
OPEN_AUTH_PATHS = ("/api/v1/auth/login", "/api/v1/auth/refresh")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★ W23 Day6：双实例 40 并发压测暴露的配置缺口——SQLAlchemy 默认连接池
    #   pool_size=5 在并发下排队（每次 POST 叠加审计写 + 会话落库 + 业务审计，
    #   单请求峰值 2–3 个并发 session）。扩容到 40+20/实例，
    #   MySQL command 加 --max-connections=500（compose）承接双实例 120 连接
    engine = create_async_engine(
        settings.platform_dsn,
        pool_pre_ping=True,
        pool_size=40,
        max_overflow=20,
        pool_timeout=30,
    )
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # ★ W25 Day1：调度器随进程启动（双实例全跑，任务级互斥靠 leader 锁）。
    #   job store 在 MySQL（任务定义重启不丢）；start() 失败 → fail-open 降级
    #   （scheduler_enabled 默认开；CI 纯单测环境可用 SCM_SCHEDULER_ENABLED=0 关闭）
    app.state.scheduler = None
    if settings.scheduler_enabled:
        scheduler = PlatformScheduler(
            jobstore_dsn=settings.jobstore_dsn,
            session_factory=app.state.session_factory,
            instance_id=settings.instance_id,
            timezone=settings.scheduler_timezone,
        )
        try:
            scheduler.start()
            app.state.scheduler = scheduler
        except Exception:  # noqa: BLE001  # 调度是旁路能力，启动失败不阻塞主服务
            import logging

            logging.getLogger("scm.platform.scheduler").exception(
                "scheduler start failed, degrade to no-scheduler"
            )
    yield
    if app.state.scheduler is not None:
        app.state.scheduler.shutdown(wait=False)
    await engine.dispose()


async def global_auth(request: Request) -> User | None:
    """全局 JWT/API Key 门禁（FastAPI 全局依赖，所有路由生效）。

    - 白名单路径 / 认证端点 → 放行（返回 None）
    - 其余路径 → 手动解析 Bearer 头后统一走 `authenticate_credentials`：
      · `sk-` 前缀 → API Key 机器身份（sha256 查表 + owner 用户存活，★ W25 Day5；
        限速在端点级 `api_key_or_jwt` 恰好一次，门禁只认证避免双计费）
      · 否则 → 完整 JWT 校验（签名/类型/吊销/用户存活）
      失败抛 401；成功返回用户（丢弃，门禁只保证"有有效身份"）
    ★ W27-D6 (B11)：sk- 检测/401 处理已收敛到 apikeys.authenticate_credentials 单处，
    与端点级 api_key_or_jwt 共用同一认证实现。
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
        return await authenticate_credentials(session, credentials)


app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    lifespan=lifespan,
    # 全局门禁：除白名单外，所有请求必须有有效 access token
    dependencies=[Depends(global_auth)],
    # ★ W25 Day4：OpenAPI 统一错误契约——所有 4xx/5xx 响应模型声明为 Err
    responses={
        401: {"model": Err, "description": "未认证 / 凭证无效 / 过期 / 已吊销"},
        403: {"model": Err, "description": "已认证但权限码未命中"},
        404: {"model": Err, "description": "资源不存在"},
        422: {"model": Err, "description": "请求参数校验失败"},
        429: {"model": Err, "description": "限流 / API Key 配额超限"},
        500: {"model": Err, "description": "服务内部错误"},
        503: {"model": Err, "description": "依赖服务不可用（如调度器）"},
    },
)

# ★ W25 Day4：统一错误响应（code / message / trace_id）运行时格式化
register_error_handlers(app)

# ==================== 中间件 ====================


class RequestIdMiddleware:
    """请求贯穿标识（★ W23 Day6）：响应头 X-Request-Id + scope['request_id']。

    双实例下日志会交错，靠 request_id 把一次请求的所有日志/审计串起来排查
    （手册 Day6 坑）。客户端/nginx 已带 X-Request-Id 则透传，否则生成 uuid 前 12 位。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])
        }
        rid = headers.get("x-request-id") or uuid4().hex[:12]
        scope["request_id"] = rid

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-Id", rid)
            await send(message)

        await self.app(scope, receive, send_wrapper)


# 审计中间件：非 GET 写操作全覆盖（登录/刷新/登出端点自身已落账，中间件跳过）
# 中间件顺序（手册坑）：RequestId 最外层（先生成 request_id），审计在其内读 scope
# ★ W25 Day6：Metrics 中间件在最内层（统计真实业务耗时，不含审计/请求头开销）
app.add_middleware(MetricsMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(RequestIdMiddleware)


# ==================== 路由 ====================


@app.get(
    "/health",
    response_model=schemas.HealthOut,
    tags=["ops"],
    summary="存活探针",
    description="返回服务与数据库连通状态 + 调度器状态（deploy compose healthcheck 用）。",
)
async def health() -> schemas.HealthOut:
    """存活探针：返回服务与数据库连通状态 + 调度器状态（W25 Day1）。"""
    db_status = "up"
    try:
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "down"
    scheduler = getattr(app.state, "scheduler", None)
    return schemas.HealthOut(
        status="ok" if db_status == "up" else "degraded",
        db=db_status,
        scheduler="running" if scheduler is not None and scheduler.running else "off",
    )


@app.get(
    "/metrics",
    tags=["ops"],
    summary="Prometheus 指标",
    description=(
        "Prometheus 文本格式指标（QPS/P95/成功率/in-flight）。★ W25 Day6："
        "node-exporter 与 cAdvisor 加入 compose 后，prometheus.yml 抓取三个 job"
        "（backend 双实例 / metrics + 宿主机 + 容器）——双监控面板有数据。"
    ),
    response_class=Response,
)
async def metrics() -> Response:
    """Prometheus 指标端点（白名单；MetricsMiddleware 自动记录每请求）。

    ★ W25 Day6 实测坑：`-> str` 会被 FastAPI 包成 JSON 字符串（带引号 + \\n 转义），
    Prometheus 解析报 `expected a valid start token` → 显式 `Response` + `text/plain`。
    """
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/api/v1/auth/me", response_model=schemas.UserOut, tags=["auth"])
async def me(current: User = Depends(rbac.require_permission("kb:chat"))) -> schemas.UserOut:
    """返回当前登录用户（权限来自 JWT claims，不查库）——受保护端点示例。"""
    return schemas.UserOut(
        id=current.id,
        username=current.username,
        tenant_id=current.tenant_id,
        permissions=sorted(rbac.current_permissions(current)),
    )


app.include_router(auth.router)

# ==================== W23 Day4：双域并入 ====================
# kb（知识问答域）与 ops（业务操作域）以模块化单体方式挂载；
# 每个请求带身份（JWT）、每个状态有归宿（MySQL/Redis）、实例可替换（Day6 无状态）。
app.include_router(kb_router.router)
app.include_router(ops_router.router)
# ==================== W24 Day3：数据分析域（NL2SQL） ====================
app.include_router(data_router.router)
# ==================== W25 Day2：平台管理域（调度面板 API） ====================
app.include_router(admin_scheduler_api.router)
# ==================== W25 Day5：平台管理域（API Key 机器身份管理） ====================
app.include_router(admin_apikey_api.router)
