"""平台认证（W23 Day3）——JWT 双令牌 + MySQL 用户校验 + 吊销名单。

对应《02》4 节 API：`POST /api/auth/login` `/refresh` `/logout`。

设计要点（手册坑逐条落实）：
- `sub` 放 user_id（不是 username），`tenant_id` + `permissions` 进 claims，
  权限判定不每请求查库（Day2 面试题"三级模型多一次 join 的规避"）
- access 15min + refresh 24h 双令牌；refresh 走 rotation（换新 refresh 即吊销旧的）
- logout 落库版吊销名单（`token_blacklist` 表，W25 可迁 Redis）
- bcrypt 的 checkpw 内置恒定时间比较防时序侧信道
- 登录 / 刷新 / 登出各自写 audit_logs（审计中间件会跳过这几个端点，避免重复落账）
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform import schemas
from app.platform.audit import write_audit
from app.platform.models import TokenBlacklist, User
from app.platform.settings import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Bearer 提取器（带 401 语义；`auto_error=False` 时缺头也走 401 处理）
bearer_scheme = HTTPBearer(auto_error=False)

_ALGORITHM = "HS256"


# ==================== 会话 / 数据库依赖 ====================


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：从 app.state.session_factory 取会话，请求结束自动归还。

    依赖注入与 lifespan 分离：单测可直接 override。
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


# ==================== JWT 编解码 ====================


def _create_token(
    user: User, token_type: str, expires_minutes: int, permissions: list[str] | None
) -> tuple[str, str]:
    """签发单令牌，返回 (token, jti)。claims 含 sub/tenant_id/type/jti。

    - access：带 permissions（权限判定零查库）
    - refresh：只带身份，不带权限（权限变更后旧 refresh 换新 access 即生效）
    """
    now = datetime.now(UTC)
    jti = uuid4().hex
    payload: dict = {
        "sub": str(user.id),
        "username": user.username,
        "tenant_id": user.tenant_id,
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    if token_type == "access" and permissions is not None:
        payload["permissions"] = permissions
    token = jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict:
    """解码并校验签名/过期，返回 payload。失败抛 HTTPException(401)。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        ) from exc


async def _load_user_permissions(session: AsyncSession, user_id: int) -> list[str]:
    """从库取用户全部权限码（登录时一次性塞进 access claims）。"""
    rows = await session.execute(
        text(
            "SELECT DISTINCT p.code FROM permissions p "
            "JOIN role_permissions rp ON rp.permission_id = p.id "
            "JOIN user_roles ur ON ur.role_id = rp.role_id "
            "JOIN users u ON u.id = ur.user_id "
            "WHERE u.id = :uid AND u.status = 1"
        ),
        {"uid": user_id},
    )
    return [code for (code,) in rows.all()]


async def _is_blacklisted(session: AsyncSession, jti: str) -> bool:
    """吊销名单命中即返回 True（登录后登出再访问旧 token 应 401）。"""
    exists = await session.scalar(
        text("SELECT 1 FROM token_blacklist WHERE jti = :jti"), {"jti": jti}
    )
    return exists is not None


async def _revoke(session: AsyncSession, payload: dict, user_id: int | None) -> None:
    """把某 token 的 jti 落吊销名单（登出/refresh rotation 共用）。"""
    session.add(
        TokenBlacklist(
            jti=payload["jti"],
            token_type=payload.get("type", "unknown"),
            user_id=user_id,
            expires_at=datetime.fromtimestamp(payload["exp"]),
        )
    )
    await session.commit()


# ==================== 当前用户依赖 ====================


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """全局认证依赖：Bearer access token → 校验签名/类型/吊销 → 返回 User。

    注意返回的是轻量 ORM 对象（仅 id/username/tenant_id 被用到）；权限在
    claims 里，`require_permission` 直接读，不在这里查库。
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token"
        )
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token type not access"
        )
    if await _is_blacklisted(session, payload["jti"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token revoked"
        )
    user = await session.get(User, int(payload["sub"]))
    if user is None or user.status != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user disabled or missing"
        )
    # 权限放 claims，暂存到对象上供 `require_permission` 零查库读取。
    # `_jwt_permissions` 未在 ORM 模型声明（运行时附加），用 setattr 避免 mypy 报未知属性
    setattr(user, "_jwt_permissions", payload.get("permissions", []))  # noqa: B010
    return user


# ==================== 端点 ====================


@router.post(
    "/login",
    response_model=schemas.TokenOut,
    summary="登录（JWT 双令牌）",
    description="bcrypt 校验用户名密码 → 签发 access（15min）+ refresh（24h）双令牌，登录成功/失败均写审计。",
)
async def login(
    body: schemas.LoginIn, session: AsyncSession = Depends(get_session)
) -> schemas.TokenOut:
    """登录：bcrypt 校验 → 签双令牌 → 登录事件写审计。

    actor 用 username（此刻还没有 user_id 对应的可审计身份之外的东西，
    claims 未签发，审计中间件跳过本端点，这里显式落账）。
    """
    user = await session.scalar(select(User).where(User.username == body.username))
    if user is None or not bcrypt.checkpw(
        body.password.encode(), user.password_hash.encode()
    ):
        await write_audit(
            session,
            event="auth.login.failed",
            actor=body.username,
            target="/api/v1/auth/login",
            status=status.HTTP_401_UNAUTHORIZED,
            detail={"reason": "bad_credentials"},
        )
        await session.commit()  # 失败登录也要留痕，先提交再抛 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    if user.status != 1:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user disabled")

    permissions = await _load_user_permissions(session, user.id)
    access, _ = _create_token(user, "access", settings.jwt_access_minutes, permissions)
    refresh, _ = _create_token(user, "refresh", settings.jwt_refresh_minutes, None)

    await write_audit(
        session,
        event="auth.login.success",
        actor=str(user.id),
        target="/api/v1/auth/login",
        status=status.HTTP_200_OK,
        detail={"username": user.username, "tenant": user.tenant_id},
    )
    await session.commit()
    return schemas.TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_minutes * 60,
    )


@router.post(
    "/refresh",
    response_model=schemas.TokenOut,
    summary="刷新令牌（rotation）",
    description="refresh token 换取新双令牌；旧 refresh 立即吊销（rotation），回放旧 refresh 应 401。",
)
async def refresh(
    body: schemas.RefreshIn, session: AsyncSession = Depends(get_session)
) -> schemas.TokenOut:
    """刷新：refresh token → rotation（吊销旧 refresh，签发新双令牌）。

    旧 access 有效期很短，无需主动吊销；但为保险，把旧 refresh 的 jti 落名单。
    """
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not a refresh token"
        )
    if await _is_blacklisted(session, payload["jti"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token revoked"
        )
    user = await session.get(User, int(payload["sub"]))
    if user is None or user.status != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user disabled")

    # rotation：旧 refresh 作废
    await _revoke(session, payload, user.id)

    permissions = await _load_user_permissions(session, user.id)
    access, _ = _create_token(user, "access", settings.jwt_access_minutes, permissions)
    refresh, _ = _create_token(user, "refresh", settings.jwt_refresh_minutes, None)

    await write_audit(
        session,
        event="auth.refresh",
        actor=str(user.id),
        target="/api/v1/auth/refresh",
        status=status.HTTP_200_OK,
    )
    await session.commit()
    return schemas.TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_minutes * 60,
    )


@router.post(
    "/logout",
    response_model=schemas.LogoutOut,
    summary="登出（吊销当前 token）",
    description="吊销当前 access token（jti 落名单，剩余生命周期内不可再用），写审计。",
)
async def logout(
    current: User = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> schemas.LogoutOut:
    """登出：吊销当前 access token（jti 落名单，剩余生命周期内不可再用）。"""
    # `get_current_user` 已保证存在有效 token，故 credentials 必非 None（类型收窄）
    assert credentials is not None
    payload = decode_token(credentials.credentials)
    await _revoke(session, payload, current.id)
    await write_audit(
        session,
        event="auth.logout",
        actor=str(current.id),
        target="/api/v1/auth/logout",
        status=status.HTTP_200_OK,
    )
    await session.commit()
    return schemas.LogoutOut(detail="logged out")
