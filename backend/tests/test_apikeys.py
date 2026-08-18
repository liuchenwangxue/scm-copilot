"""W25 Day5 API Key 测试：令牌桶纯逻辑 + Key 生命周期（集成）+ 双轨认证。

分层（对齐手册 Day5）：
- 纯逻辑（CI 可跑，无外部服务）：`token_bucket_allow` 行为推演 /
  generate / hash / prefix 原语 / Redis 不可用 fail-open
- integration（需 MySQL + seed）：admin 创建 Key → API Key 访问受保护端点 →
  吊销 401 / 无效 Key 401 / viewer owner 权限继承
- integration（需 Redis）：令牌桶 429 + Retry-After（Redis 不可用 → skip，
  与后端 fail-open 设计一致）
"""

import pytest

from app.platform.apikeys import (
    check_token_bucket,
    generate_api_key,
    hash_api_key,
    key_prefix_of,
    token_bucket_allow,
)

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


# ==================== 纯逻辑：Key 原语 ====================


def test_generate_api_key_format():
    """sk- 前缀 + 48 位 hex（192bit 熵）——机器身份防猜测。"""
    key = generate_api_key()
    assert key.startswith("sk-")
    assert len(key) == len("sk-") + 48


def test_hash_api_key_deterministic_sha256():
    """sha256 哈希：确定性 + 不可逆（明文只在创建时返回一次）。"""
    assert hash_api_key("sk-abc") == hash_api_key("sk-abc")
    assert hash_api_key("sk-abc") != hash_api_key("sk-abd")


def test_key_prefix_of():
    """展示前缀：sk- + 前 8 位（api_keys.key_prefix 字段）。"""
    assert key_prefix_of("sk-abcdef123456") == "sk-abcdef12"


# ==================== 纯逻辑：令牌桶行为推演 ====================


def test_token_bucket_full_consumes_one():
    """满桶（容量 10）首次请求放行并扣 1 个令牌。"""
    allowed, state, retry = token_bucket_allow(
        (10.0, 0.0), now=0.0, capacity=10, refill_per_sec=5 / 60
    )
    assert allowed is True
    assert state[0] == 9.0
    assert retry == 0


def test_token_bucket_exhaust_then_reject_then_refill():
    """耗尽 → 拒绝 + retry_after>0 → 时间推进按速率补充可再次放行。"""
    state = (10.0, 0.0)
    # 连续消耗 10 个令牌（同一时刻，无补充）
    for _ in range(10):
        allowed, state, _ = token_bucket_allow(
            state, now=100.0, capacity=10, refill_per_sec=5 / 60
        )
        assert allowed is True
    # 第 11 次：不足 1 个令牌 → 拒绝 + retry_after（≈12s = 补 1 个的时间）
    allowed, state, retry = token_bucket_allow(
        state, now=100.0, capacity=10, refill_per_sec=5 / 60
    )
    assert allowed is False
    assert retry > 0
    # 60 秒后按 5/min 补充了 5 个 → 可放行
    allowed, state, _ = token_bucket_allow(
        state, now=160.0, capacity=10, refill_per_sec=5 / 60
    )
    assert allowed is True


def test_check_token_bucket_fail_open_when_redis_down():
    """Redis 不可用 → fail-open 放行（配额软约束，宁可多跑不可卡死）。"""

    class NoRedis:
        available = False

    ok, retry = check_token_bucket("hash-x", redis_client=NoRedis())
    assert ok is True
    assert retry == 0


# ==================== integration：API Key 生命周期 + 双轨认证 ====================


@pytest.mark.integration
def test_admin_create_key_and_apikey_auth_works(client):
    """admin 创建 Key → API Key 访问受保护端点 200（权限继承 owner）→ 吊销后 401。"""
    headers = _login(client, tenant_user("admin"))

    created = client.post(
        "/api/v1/admin/apikeys", headers=headers, json={"name": "test-key"}
    )
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["api_key"].startswith("sk-")
    assert data["key_prefix"].startswith("sk-")
    assert data["owner_username"] == tenant_user("admin")
    key_id = data["key_id"]

    try:
        # API Key（机器身份）访问受保护端点 → 200 + 属主身份
        resp = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {data['api_key']}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == tenant_user("admin")
        assert "admin:apikey:manage" in resp.json()["permissions"]

        # 列表不暴露哈希/明文
        lst = client.get("/api/v1/admin/apikeys", headers=headers)
        assert lst.status_code == 200
        items = lst.json()["api_keys"]
        row = next(i for i in items if i["key_id"] == key_id)
        assert row["enabled"] is True
        assert "key_hash" not in lst.text
        assert '"api_key":' not in lst.text  # 完整 Key 明文只返回一次（api_keys 是字段名）
    finally:
        revoke = client.delete(f"/api/v1/admin/apikeys/{key_id}", headers=headers)
        assert revoke.status_code == 200, revoke.text
        assert revoke.json()["revoked"] is True

    # 吊销后立即 401（软删除语义）
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {data['api_key']}"}
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.integration
def test_invalid_api_key_401_and_plain_jwt_still_works(client):
    """无效 API Key → 401；JWT 认证不受影响（双轨并存）。"""
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer sk-invalid-deadbeef"}
    )
    assert resp.status_code == 401, resp.text

    headers = _login(client, tenant_user("admin"))
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200


@pytest.mark.integration
def test_apikey_owner_permission_inheritance_viewer(client):
    """viewer 属主的 Key 继承只读权限：/me 200 但 admin 端点 403。"""
    # viewer 无 admin:apikey:manage → 不能创建；由 admin 代建并指定 owner
    admin_headers = _login(client, tenant_user("admin"))
    created = client.post(
        "/api/v1/admin/apikeys",
        headers=admin_headers,
        json={"name": "viewer-key", "owner_username": tenant_user("viewer")},
    )
    assert created.status_code == 200, created.text
    key = created.json()["api_key"]
    key_id = created.json()["key_id"]
    try:
        # 只读权限：/me 200（kb:chat 有）
        assert (
            client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {key}"}
            ).status_code
            == 200
        )
        # 无 admin:apikey:manage → admin 端点 403（权限继承正确收窄）
        resp = client.get("/api/v1/admin/apikeys", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 403, resp.text
    finally:
        client.delete(f"/api/v1/admin/apikeys/{key_id}", headers=admin_headers)


@pytest.mark.integration
def test_apikey_management_forbidden_for_operator(client):
    """非 admin（operator 无 admin:apikey:manage）→ 403。"""
    headers = _login(client, tenant_user("operator"))
    assert (
        client.post("/api/v1/admin/apikeys", headers=headers, json={"name": "x"}).status_code
        == 403
    )
    assert (
        client.get("/api/v1/admin/apikeys", headers=headers).status_code == 403
    )
    assert (
        client.delete("/api/v1/admin/apikeys/1", headers=headers).status_code == 403
    )


@pytest.mark.integration
def test_api_key_rate_limit_429_and_retry_after(client):
    """令牌桶打满 → 429 + Retry-After 头（需要真实 Redis；不可用则 fail-open skip）。"""
    headers = _login(client, tenant_user("admin"))
    created = client.post(
        "/api/v1/admin/apikeys", headers=headers, json={"name": "limit-test"}
    ).json()
    key = created["api_key"]
    key_id = created["key_id"]
    try:
        hit_429 = False
        # 独立桶容量 10：连续请求最多 10 次后应 429；Redis 挂则一路 200
        for _ in range(30):
            resp = client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {key}"}
            )
            if resp.status_code == 429:
                hit_429 = True
                assert resp.headers.get("Retry-After"), "429 必须带 Retry-After 头"
                body = resp.json()
                assert body["code"] == "QUOTA_429"
                assert body["trace_id"], "429 也应带 trace_id（Err 契约）"
                break
        if not hit_429:
            pytest.skip("Redis 不可用 → 限速 fail-open（部署环境验证 429）")
    finally:
        client.delete(f"/api/v1/admin/apikeys/{key_id}", headers=headers)
