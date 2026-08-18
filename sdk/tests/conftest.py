"""SDK 集成测试夹具：对真实平台登录 admin → 创建 API Key → 用毕吊销。

环境变量：
- SCM_SDK_BASE_URL：平台地址（默认 http://localhost:8000）
- SCM_SDK_ADMIN_USER / SCM_SDK_ADMIN_PASSWORD：seed 管理员凭证（默认 admin_t_huadong / Passw0rd!）
- SCM_SDK_VERIFY：TLS 校验开关（默认 1；本地 mkcert 自签平台设 0）
"""

import contextlib
import os

import pytest

from scm_client import ScmCopilot

BASE_URL = os.environ.get("SCM_SDK_BASE_URL", "http://localhost:8000")
ADMIN_USER = os.environ.get("SCM_SDK_ADMIN_USER", "admin_t_huadong")
ADMIN_PASSWORD = os.environ.get("SCM_SDK_ADMIN_PASSWORD", "Passw0rd!")
VERIFY = os.environ.get("SCM_SDK_VERIFY", "1") == "1"


@pytest.fixture(scope="session")
def platform_url() -> str:
    return BASE_URL


@pytest.fixture(scope="module")
def admin_token() -> str:
    """登录 seed admin，返回 access token（创建/吊销 API Key 用）。"""
    client = ScmCopilot(BASE_URL, verify=VERIFY)
    try:
        resp = client._request(
            "POST", "/api/v1/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        )
    finally:
        client.close()
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def create_key(admin_token):
    """工厂：创建 API Key（owner=admin），返回明文；module 结束统一吊销。"""
    admin = ScmCopilot(BASE_URL, token=admin_token, verify=VERIFY)
    created: list[tuple[int, str]] = []

    def _make(name: str = "sdk-test") -> str:
        resp = admin._request(
            "POST", "/api/v1/admin/apikeys",
            json={"name": name, "owner_username": ADMIN_USER},
        )
        data = resp.json()
        created.append((int(data["key_id"]), data["api_key"]))
        return data["api_key"]

    yield _make

    for key_id, _ in created:
        with contextlib.suppress(Exception):  # 清理尽力而为
            admin._request("DELETE", f"/api/v1/admin/apikeys/{key_id}")
    admin.close()


@pytest.fixture(scope="module")
def api_key(create_key) -> str:
    """十行流程专用 Key。"""
    return create_key("sdk-ten-line")
