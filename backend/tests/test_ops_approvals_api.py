"""W25 Day5 审批列表 API 测试：SDK approvals.list_pending() 的数据源。

- `GET /api/v1/ops/approvals`：待审批列表（含 HITL 恢复上下文 session_id）
- 权限：ops:approval:manage（operator/admin 有；analyst 403）
"""

import pytest

pytestmark = pytest.mark.integration

PLAIN_PASSWORD = "Passw0rd!"


def tenant_user(role: str, tenant: str = "t_huadong") -> str:
    return f"{role}_{tenant}"


def _login(client, username: str) -> dict:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PLAIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_approvals_list_forbidden_for_analyst(client):
    """analyst 无 ops:approval:manage → 403（权限闸）。"""
    headers = _login(client, tenant_user("analyst"))
    resp = client.get("/api/v1/ops/approvals", headers=headers)
    assert resp.status_code == 403


def test_approvals_list_ok_for_operator(client):
    """operator 有 ops:approval:manage → 200 + approvals 数组（结构契约）。"""
    headers = _login(client, tenant_user("operator"))
    resp = client.get("/api/v1/ops/approvals", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "approvals" in body and "total" in body
    assert isinstance(body["approvals"], list)
    # 契约字段：SDK ApprovalItem.from_payload 依赖这些键
    for item in body["approvals"]:
        assert item["approval_id"]
        assert "session_id" in item
        assert "operation" in item and "order_id" in item
        assert "diff" in item and isinstance(item["diff"], list)


def test_approvals_list_forbidden_without_auth(client):
    """未认证 → 401（全局门禁）。"""
    resp = client.get("/api/v1/ops/approvals")
    assert resp.status_code == 401
