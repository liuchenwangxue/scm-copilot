"""平台审计（W23 Day3）——ASGI 中间件 + 写审计辅助。

对应手册：写操作 100% 落 `audit_logs`（event=method+path / actor=sub / trace_id / status）。

设计要点：
- 中间件只审计【非 GET】请求（写操作才留痕，读操作不进审计日志）
- actor 尽力从 Bearer claims.sub 解析（中间件不校验 JWT，避免与认证耦合；解析失败留空）
- 登录/刷新/登出三个认证端点跳过（它们各自显式 write_audit，避免双重落账）
- 中间件不阻断请求（审计失败只记日志不抛错，审计系统故障不拖垮主流程）
"""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.models import AuditLog

logger = logging.getLogger("scm.platform.audit")

# 认证端点已自行落账，中间件跳过防重复
SKIP_AUDIT_PATHS = {"/api/auth/login", "/api/auth/refresh", "/api/auth/logout"}


async def write_audit(
    session: AsyncSession,
    *,
    event: str,
    actor: str | None = None,
    target: str | None = None,
    status: int = 200,
    detail: dict[str, Any] | None = None,
) -> None:
    """显式写一条审计日志（事务随调用方 session 提交）。"""
    session.add(
        AuditLog(
            event=event, actor=actor, trace_id=None, target=target, detail=detail, status=status
        )
    )


def extract_actor_from_auth(authorization: str | None) -> str | None:
    """从 Bearer 头尽量解析出 actor（sub）。解析失败返回 None，不阻断。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    try:
        import jwt

        from app.platform.settings import settings

        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], options={"verify_exp": False}
        )
        return str(payload.get("sub")) if payload.get("type") == "access" else None
    except Exception:  # noqa: BLE001  # 审计尽力而为，token 脏不影响业务
        return None


class AuditMiddleware:
    """ASGI 中间件：非 GET 请求落审计。

    中间件位于路由之前，无法复用 FastAPI 依赖，因此只做"尽力解析 actor"；
    严格的身份与权限校验仍由路由层的 JWT 依赖负责——审计是记录，不是门禁。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if method == "GET" or path in SKIP_AUDIT_PATHS or path.startswith(
            ("/docs", "/openapi.json", "/redoc")
        ):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        actor = extract_actor_from_auth(headers.get("authorization"))

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 异步写审计：新开会话独立提交，不污染请求事务；失败仅记日志
            try:
                import asyncio

                factory = scope["app"].state.session_factory
                async with factory() as session:
                    await write_audit(
                        session,
                        event=f"{method} {path}",
                        actor=actor,
                        target=path,
                        status=status_code,
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("audit write failed for %s %s", method, path)
