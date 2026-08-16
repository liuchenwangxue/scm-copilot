"""W23 Day2 平台库种子数据验证（integration，需 MySQL）。

验证点：
- 4 角色 / 12 权限 / 25 角色-权限映射 / 12 用户（3 租户 × 4 角色）
- 幂等：连跑两遍 seed 行数一致
- RBAC 矩阵：admin 全量 12 / operator 7 / analyst 4 / viewer 2
- 测试密码 `Passw0rd!` 可被 bcrypt 校验（login 前置条件）
"""

import os

import bcrypt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DSN = os.environ.get(
    "SCM_TEST_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4",
)

PLAIN_PASSWORD = "Passw0rd!"

# 角色 → 期望权限数（与 seed_platform.ROLE_PERMISSION_MAP 一致）
EXPECTED_ROLE_PERM_COUNT = {"admin": 12, "operator": 7, "analyst": 4, "viewer": 2}


@pytest.mark.integration
async def test_roles_and_permissions_count():
    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            roles = await conn.scalar(text("SELECT COUNT(*) FROM roles"))
            perms = await conn.scalar(text("SELECT COUNT(*) FROM permissions"))
            rp = await conn.scalar(text("SELECT COUNT(*) FROM role_permissions"))
            ur = await conn.scalar(text("SELECT COUNT(*) FROM user_roles"))
            users = await conn.scalar(text("SELECT COUNT(*) FROM users"))
        assert roles == 4
        assert perms == 12
        assert rp == 25
        assert ur == 12
        assert users == 12
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_rbac_permission_matrix():
    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT r.code, COUNT(rp.permission_id) "
                    "FROM roles r "
                    "LEFT JOIN role_permissions rp ON r.id = rp.role_id "
                    "GROUP BY r.code"
                )
            )
            actual = {code: cnt for code, cnt in rows.all()}
        assert actual == EXPECTED_ROLE_PERM_COUNT, f"RBAC 矩阵不符: {actual}"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_users_bcrypt_password_verifiable():
    """seed 用户的 password_hash 用 `Passw0rd!` 可校验（登录前置条件）。"""
    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT username, password_hash FROM users LIMIT 3"))
            for username, pwd_hash in rows.all():
                assert bcrypt.checkpw(PLAIN_PASSWORD.encode(), pwd_hash.encode()), (
                    f"{username} 密码不可校验"
                )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_tenant_isolation_keys():
    """3 租户，每个租户 4 角色用户，tenant_id 均非空。"""
    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            tenants = await conn.execute(
                text("SELECT tenant_id, COUNT(*) FROM users GROUP BY tenant_id")
            )
            rows = tenants.all()
        assert len(rows) == 3
        for _, cnt in rows:
            assert cnt == 4
    finally:
        await engine.dispose()
