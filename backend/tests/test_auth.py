"""W23 Day3 认证链路测试——三态（401 / 403 / 200）+ 审计落库。

覆盖（对照手册 Day3 下午）：
- 401：无 token / 过期 / 篡改签名 / 错账号密码 / 吊销后访问 / refresh 当 access 用
- 403：viewer 调需权限的受保护端点
- 200：admin 合法登录 + 受保护端点
- 审计：登录成功/失败都落 audit_logs
"""

import jwt
import pytest

from tests.conftest import PLAIN_PASSWORD, TENANTS, tenant_user

# access 有效期秒数（与 settings 默认 15min 一致；测试里造过期 token 用）
ACCESS_TTL = 15 * 60


# ==================== 200 路径 ====================


@pytest.mark.integration
def test_login_success_and_me(client):
    """admin 登录 → 双令牌 + /api/auth/me 200 返回身份。"""
    resp = client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": PLAIN_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] == ACCESS_TTL

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["username"] == tenant_user("admin")
    assert "kb:chat" in me_body["permissions"]
    assert "admin:user:manage" in me_body["permissions"]


@pytest.mark.integration
def test_all_tenants_can_login(client):
    """3 租户 × admin 都能登录（种子完整性）。"""
    for tenant in TENANTS:
        resp = client.post(
            "/api/auth/login",
            json={"username": tenant_user("admin", tenant), "password": PLAIN_PASSWORD},
        )
        assert resp.status_code == 200, f"{tenant} 登录失败: {resp.text}"


# ==================== 401 路径 ====================


@pytest.mark.integration
def test_no_token_401(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.integration
def test_bad_credentials_401(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": "WrongPassw0rd!"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


@pytest.mark.integration
def test_unknown_user_401(client):
    resp = client.post(
        "/api/auth/login", json={"username": "ghost_user", "password": "whatever"}
    )
    assert resp.status_code == 401


@pytest.mark.integration
def test_tampered_token_401(client, auth_headers):
    """篡改签名段（第二个点之后的字符）→ 签名校验失败 401。

    改最后一个字符不可靠：base64url 尾部位可能因 padding 解码成相同字节，签名仍匹配。
    可靠做法：改签名段中间的字符，必然破坏 HMAC。
    """
    headers = auth_headers()
    token = headers["Authorization"][len("Bearer ") :]
    header, payload, signature = token.split(".")
    # 翻转签名段中间某字符（保持 base64url 合法字符集）
    mid = len(signature) // 2
    orig = signature[mid]
    replacement = "A" if orig != "A" else "B"
    tampered_signature = signature[:mid] + replacement + signature[mid + 1 :]
    tampered = f"{header}.{payload}.{tampered_signature}"
    assert tampered != token, "篡改后 token 不应与原文相同"
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401


@pytest.mark.integration
def test_expired_token_401(client, auth_headers):
    """用过期时间造一个已过期但签名正确的 access token。"""
    from app.platform.settings import settings

    headers = auth_headers()
    token = headers["Authorization"][len("Bearer ") :]
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    payload["exp"] = -1  # 已过期
    expired = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"]


@pytest.mark.integration
def test_refresh_token_not_valid_as_access_401(client):
    """refresh token 当 access 用应 401。"""
    login = client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": PLAIN_PASSWORD},
    )
    refresh = login.json()["refresh_token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401
    assert "not access" in resp.json()["detail"]


@pytest.mark.integration
def test_logout_revokes_access_token(client, auth_headers):
    """登出后旧 access token 应 401（吊销名单生效）。"""
    headers = auth_headers()
    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    # 再访问受保护端点 → 已吊销
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"]


# ==================== 403 路径 ====================


@pytest.mark.integration
def test_viewer_forbidden_on_admin_perm(client):
    """viewer 登录（只有 kb:chat/kb:read），访问需 admin 权限的端点应 403。

    用 /api/auth/me 的 kb:chat 作对照：viewer 能过；构造一个需 admin 权限的
    依赖在测试里无法直接挂路由，这里验证 rbac.require_permission 对缺失权限抛 403。
    """
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.main import app as main_app
    from app.platform import rbac
    from app.platform.models import User

    # 临时挂一个需 admin:user:manage 的端点做越权验证
    probe = FastAPI()
    probe.include_router(main_app.router)

    @probe.get("/probe/admin")
    async def probe_admin(_: User = Depends(rbac.require_permission("admin:user:manage"))):
        return {"ok": True}

    # 复用主 app 的 state（engine/session_factory）——同 TestClient 进程内共享
    probe.state.engine = main_app.state.engine
    probe.state.session_factory = main_app.state.session_factory

    with TestClient(probe) as c:
        viewer = c.post(
            "/api/auth/login",
            json={"username": tenant_user("viewer"), "password": PLAIN_PASSWORD},
        )
        vh = {"Authorization": f"Bearer {viewer.json()['access_token']}"}
        assert c.get("/probe/admin", headers=vh).status_code == 403
        # viewer 用 kb:chat 权限访问 -> 200
        assert c.get("/api/auth/me", headers=vh).status_code == 200


@pytest.mark.integration
def test_analyst_cannot_access_admin(client):
    """analyst（kb+data）访问 admin 权限端点 403；访问 data 权限端点应 200。"""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.main import app as main_app
    from app.platform import rbac
    from app.platform.models import User

    probe = FastAPI()
    probe.include_router(main_app.router)

    @probe.get("/probe/nl2sql")
    async def probe_nl2sql(_: User = Depends(rbac.require_permission("data:nl2sql"))):
        return {"ok": True}

    @probe.get("/probe/admin")
    async def probe_admin(_: User = Depends(rbac.require_permission("admin:user:manage"))):
        return {"ok": True}

    probe.state.engine = main_app.state.engine
    probe.state.session_factory = main_app.state.session_factory

    with TestClient(probe) as c:
        an = c.post(
            "/api/auth/login",
            json={"username": tenant_user("analyst"), "password": PLAIN_PASSWORD},
        )
        ah = {"Authorization": f"Bearer {an.json()['access_token']}"}
        assert c.get("/probe/nl2sql", headers=ah).status_code == 200
        assert c.get("/probe/admin", headers=ah).status_code == 403


# ==================== refresh 流程 ====================


@pytest.mark.integration
def test_refresh_rotation_invalidates_old_refresh(client):
    """刷新后旧 refresh 作废（rotation），再用旧 refresh 应 401。"""
    login = client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": PLAIN_PASSWORD},
    )
    old_refresh = login.json()["refresh_token"]

    refresh = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh.status_code == 200
    new_tokens = refresh.json()
    assert new_tokens["access_token"]

    # 旧 refresh 已吊销
    replay = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401
    assert "revoked" in replay.json()["detail"]


# ==================== 审计落库 ====================


@pytest.mark.integration
async def test_login_audit_logged(client):
    """登录成功/失败都写 audit_logs。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import TEST_DSN

    # 成功登录
    client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": PLAIN_PASSWORD},
    )
    # 失败登录
    client.post(
        "/api/auth/login",
        json={"username": tenant_user("admin"), "password": "WrongPassw0rd!"},
    )

    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            success = await conn.scalar(
                text("SELECT COUNT(*) FROM audit_logs WHERE event = 'auth.login.success'")
            )
            failed = await conn.scalar(
                text("SELECT COUNT(*) FROM audit_logs WHERE event = 'auth.login.failed'")
            )
    finally:
        await engine.dispose()

    assert success >= 1
    assert failed >= 1


@pytest.mark.integration
def test_audit_middleware_captures_non_get(client):
    """审计中间件：非 GET 写操作（非认证端点）自动落 audit_logs，actor 取自 Bearer claims。

    用一个 probe POST 端点触发中间件；GET 不落审计。
    """
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.main import app as main_app
    from app.platform import rbac
    from app.platform.audit import AuditMiddleware
    from app.platform.models import User

    probe = FastAPI()
    probe.add_middleware(AuditMiddleware)  # 复用与主 app 相同的审计中间件
    probe.include_router(main_app.router)
    probe.state.engine = main_app.state.engine
    probe.state.session_factory = main_app.state.session_factory

    @probe.post("/probe/write")
    async def probe_write(_: User = Depends(rbac.require_permission("admin:user:manage"))):
        return {"ok": True}

    with TestClient(probe) as c:
        admin = c.post(
            "/api/auth/login",
            json={"username": tenant_user("admin"), "password": PLAIN_PASSWORD},
        )
        headers = {"Authorization": f"Bearer {admin.json()['access_token']}"}
        assert c.post("/probe/write", headers=headers).status_code == 200
        # GET 不落审计
        c.get("/probe/write", headers=headers)

        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine

        from tests.conftest import TEST_DSN

        async def _check():
            engine = create_async_engine(TEST_DSN)
            try:
                async with engine.connect() as conn:
                    write_count = await conn.scalar(
                        text("SELECT COUNT(*) FROM audit_logs WHERE event = 'POST /probe/write'")
                    )
                    get_count = await conn.scalar(
                        text("SELECT COUNT(*) FROM audit_logs WHERE event = 'GET /probe/write'")
                    )
                    return write_count, get_count
            finally:
                await engine.dispose()

        write_count, get_count = asyncio.run(_check())

    assert write_count >= 1
    assert get_count == 0
