"""W26 Day2 故障演练修复的回归测试（防"降级链"被后续改动破坏）。

覆盖本次演练暴露并修复的两处降级行为：
1. auth.get_current_user：MySQL 不可用 → fail-open 信任 JWT claims（不 500）
   —— 已签发 token 在存储故障期间仍可用（权限来自签名 claims，零查库设计）
2. auth.login：MySQL 不可用 → 503 SERVICE_UNAVAILABLE 明确提示（不 500）
   —— 登录依赖存储，明确告知"认证服务暂不可用"，让调用方重试而非误判内部错误

★ CI 可跑（无 MySQL 依赖）：FakeSession 模拟 DB 查询抛 OperationalError。
"""
import jwt
import pytest

from app.platform.models import User
from app.platform.settings import settings

FAKE_CLAIMS = {
    "sub": "1",  # PyJWT 要求 sub 为字符串（与真实登录 str(user.id) 一致）
    "username": "admin_t_huadong",
    "tenant_id": "t_huadong",
    "type": "access",
    "permissions": ["kb:chat", "ops:tool:execute"],
}


def _make_access_token(**overrides) -> str:
    """用测试 secret 签一个 access token（签名本地校验，不依赖 DB）。"""
    payload = {**FAKE_CLAIMS, **overrides}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


class FakeBrokenSession:
    """模拟 MySQL 挂：任何查询/取对象都抛 OperationalError。"""

    def __init__(self):
        import sqlalchemy.exc as exc

        self._err = exc.OperationalError(
            "select 1", {}, Exception("Can't connect to MySQL (refused)")
        )

    async def scalar(self, *a, **kw):
        raise self._err

    async def execute(self, *a, **kw):
        raise self._err

    async def get(self, model, pk):
        raise self._err


class FakeCredentials:
    def __init__(self, token: str):
        self.credentials = token


@pytest.mark.asyncio
async def test_get_current_user_fail_open_when_db_down():
    """MySQL 挂时：已有合法 JWT 仍可通过认证（fail-open 信任 claims），不 500。"""
    from app.platform import auth

    token = _make_access_token()
    user = await auth.get_current_user(  # type: ignore[arg-type]
        credentials=FakeCredentials(token), session=FakeBrokenSession()
    )
    assert isinstance(user, User)
    assert str(user.id) == "1"
    assert user.username == "admin_t_huadong"
    # 权限来自 claims（零查库），fail-open 后仍完整可用
    assert vars(user).get("_jwt_permissions") == FAKE_CLAIMS["permissions"]


@pytest.mark.asyncio
async def test_get_current_user_still_401_for_bad_signature():
    """DB 挂不能放宽签名校验：篡改 token 仍 401（安全边界不被故障削掉）。"""
    from fastapi import HTTPException

    from app.platform import auth

    token = _make_access_token()
    header, payload, sig = token.split(".")
    mid = len(sig) // 2
    sig = sig[:mid] + ("A" if sig[mid] != "A" else "B") + sig[mid + 1 :]
    tampered = f"{header}.{payload}.{sig}"
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(  # type: ignore[arg-type]
            credentials=FakeCredentials(tampered), session=FakeBrokenSession()
        )
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_401_for_refresh_type():
    """refresh token 当 access 用仍 401（类型校验在查库之前，DB 挂也拦截）。"""
    from fastapi import HTTPException

    from app.platform import auth

    token = _make_access_token(type="refresh")
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(  # type: ignore[arg-type]
            credentials=FakeCredentials(token), session=FakeBrokenSession()
        )
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_login_returns_503_when_db_down():
    """MySQL 挂时：登录返回 503 SERVICE_UNAVAILABLE（明确提示），而非 500。"""
    from fastapi import HTTPException

    from app.platform import auth
    from app.platform.schemas import LoginIn

    body = LoginIn(username="admin_t_huadong", password="Passw0rd!")
    with pytest.raises(HTTPException) as ei:
        await auth.login(body=body, session=FakeBrokenSession())  # type: ignore[arg-type]
    assert ei.value.status_code == 503
    assert "暂不可用" in str(ei.value.detail)


def test_error_code_503_defined():
    """错误契约：503 已有机器码（SERVICE_UNAVAILABLE_503），审批降级可复用。"""
    from app.platform.errors import ErrorCode

    assert ErrorCode.SERVICE_UNAVAILABLE == "SERVICE_UNAVAILABLE_503"
