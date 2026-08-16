"""W23 Day3 认证/RBAC 测试共享夹具。

关键点：
- 在导入 app 前设置 SCM_PLATFORM_DSN / SCM_JWT_SECRET（settings 在 import 时读取 env）
  —— DSN 复用 SCM_TEST_DSN（CI 用 service 容器 3306，本地默认 compose 13306）
- 认证端点依赖真实 MySQL（校验用户存在性 + 写审计/吊销），故测试打 integration tag
"""

import os

import pytest

# 优先用 CI 提供的测试 DSN，本地兜底 compose 的 13306
TEST_DSN = os.environ.get(
    "SCM_TEST_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4",
)
# 固定测试签名密钥（区别于 dev 默认值，避免测试污染生产 jwt_secret 语义）
TEST_JWT_SECRET = os.environ.get("SCM_JWT_SECRET", "test-secret-for-day3")

# 必须在 import app.* 之前写入环境变量（settings 是模块级单例）
os.environ["SCM_PLATFORM_DSN"] = TEST_DSN
os.environ["SCM_JWT_SECRET"] = TEST_JWT_SECRET

# seed 用户的固定测试凭证（与 scripts/seed_platform.py 一致）
PLAIN_PASSWORD = "Passw0rd!"

# 各租户默认用户名（{role}_{tenant}）
TENANTS = ["t_huadong", "t_huabei", "t_huanan"]


def tenant_user(role: str, tenant: str = "t_huadong") -> str:
    return f"{role}_{tenant}"


@pytest.fixture
def client():
    """FastAPI TestClient：进入即触发 lifespan（建 engine/session_factory）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client):
    """登录 admin 用户，返回带 access token 的 Authorization 头。"""

    def _make(username: str | None = None):
        resp = client.post(
            "/api/auth/login",
            json={"username": username or tenant_user("admin"), "password": PLAIN_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _make
