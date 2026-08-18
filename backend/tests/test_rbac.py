"""W23 Day3 RBAC 权限矩阵测试。

覆盖（对照手册 Day3：4 角色 × 12 权限码矩阵全断言，防"漏配权限"）：
- 矩阵正确性：每个角色登录后的 JWT claims.permissions 与 seed 定义一致
- 越权拦截：用 probe 端点验证"无权限 → 403 / 有权限 → 200"正交覆盖关键格
"""

import pytest

from tests.conftest import PLAIN_PASSWORD, tenant_user

# 与 scripts/seed_platform.py 的 ROLE_PERMISSION_MAP 逐字对齐（单一事实来源）
# ★ W25 Day5：admin 新增 admin:apikey:manage（机器身份管理，全量 13 条）
EXPECTED_PERMISSIONS = {
    "admin": {
        "kb:chat", "kb:read", "kb:feedback",
        "ops:order:read", "ops:order:update", "ops:approval:manage", "ops:tool:execute",
        "data:nl2sql", "data:feedback",
        "admin:user:manage", "admin:audit:read", "admin:scheduler:manage",
        "admin:apikey:manage",
    },
    "operator": {"kb:chat", "kb:read", "kb:feedback", "ops:order:read",
                 "ops:order:update", "ops:approval:manage", "ops:tool:execute"},
    "analyst": {"kb:chat", "kb:read", "data:nl2sql", "data:feedback"},
    "viewer": {"kb:chat", "kb:read"},
}

ALL_PERMISSIONS = EXPECTED_PERMISSIONS["admin"]


def _login_permissions(client, role: str) -> set[str]:
    """登录并从 access token 的 JWT claims 解出权限（直接验证签发内容）。"""
    import jwt

    from app.platform.settings import settings

    resp = client.post(
        "/api/v1/auth/login",
        json={"username": tenant_user(role), "password": PLAIN_PASSWORD},
    )
    assert resp.status_code == 200, f"{role} 登录失败: {resp.text}"
    payload = jwt.decode(
        resp.json()["access_token"], settings.jwt_secret, algorithms=["HS256"]
    )
    return set(payload.get("permissions", []))


# ==================== 矩阵正确性（claims 与 seed 一致） ====================


@pytest.mark.integration
@pytest.mark.parametrize("role", ["admin", "operator", "analyst", "viewer"])
def test_role_permission_matrix_from_claims(client, role):
    """每个角色登录后，claims 里的权限集合与 seed 定义完全一致。"""
    actual = _login_permissions(client, role)
    assert actual == EXPECTED_PERMISSIONS[role], f"{role} 权限不符: 缺{EXPECTED_PERMISSIONS[role]-actual} 多{actual-EXPECTED_PERMISSIONS[role]}"


@pytest.mark.integration
def test_all_permissions_seeded_unique(client):
    """13 个权限码在 claims 层不重不漏（与 seed 对齐；★ W25 Day5 admin 13）。"""
    admin = _login_permissions(client, "admin")
    assert admin == ALL_PERMISSIONS
    assert len(admin) == 13


# ==================== 越权拦截（有权限 200 / 无权限 403） ====================


@pytest.mark.integration
@pytest.mark.parametrize("role,perm,expected", [
    # admin 全量
    ("admin", "admin:user:manage", 200),
    ("admin", "data:nl2sql", 200),
    # operator 有 ops 无 admin
    ("operator", "ops:order:update", 200),
    ("operator", "admin:user:manage", 403),
    ("operator", "data:nl2sql", 403),
    # analyst 有 data 无 ops/admin
    ("analyst", "data:nl2sql", 200),
    ("analyst", "ops:order:update", 403),
    ("analyst", "admin:user:manage", 403),
    # viewer 只有 kb 只读
    ("viewer", "kb:chat", 200),
    ("viewer", "ops:order:read", 403),
    ("viewer", "data:nl2sql", 403),
])
def test_permission_gate_allow_deny(client, role, perm, expected):
    """对指定权限码挂 probe 端点，验证命中/未命中 → 200/403。"""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.main import app as main_app
    from app.platform import rbac
    from app.platform.models import User

    probe = FastAPI()
    probe.include_router(main_app.router)
    probe.state.engine = main_app.state.engine
    probe.state.session_factory = main_app.state.session_factory

    @probe.get("/probe/perm")
    async def probe_perm(_: User = Depends(rbac.require_permission(perm))):
        return {"ok": True}

    with TestClient(probe) as c:
        login = c.post(
            "/api/v1/auth/login",
            json={"username": tenant_user(role), "password": PLAIN_PASSWORD},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        resp = c.get("/probe/perm", headers=headers)
        assert resp.status_code == expected, f"{role} @ {perm} 期望{expected} 实际{resp.status_code} {resp.text}"


@pytest.mark.integration
def test_require_any_of_admin_or_data(client):
    """任一权限放行：analyst 有 data 无 admin 也能过 any-of(admin,data)。"""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.main import app as main_app
    from app.platform import rbac
    from app.platform.models import User

    probe = FastAPI()
    probe.include_router(main_app.router)
    probe.state.engine = main_app.state.engine
    probe.state.session_factory = main_app.state.session_factory

    @probe.get("/probe/any")
    async def probe_any(_: User = Depends(rbac.require_any_of("admin:user:manage", "data:nl2sql"))):
        return {"ok": True}

    with TestClient(probe) as c:
        an = c.post(
            "/api/v1/auth/login",
            json={"username": tenant_user("analyst"), "password": PLAIN_PASSWORD},
        )
        assert c.get("/probe/any", headers={"Authorization": f"Bearer {an.json()['access_token']}"}).status_code == 200
        vw = c.post(
            "/api/v1/auth/login",
            json={"username": tenant_user("viewer"), "password": PLAIN_PASSWORD},
        )
        assert c.get("/probe/any", headers={"Authorization": f"Bearer {vw.json()['access_token']}"}).status_code == 403
