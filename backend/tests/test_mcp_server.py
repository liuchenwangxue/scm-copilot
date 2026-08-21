"""★ MCP Server 单元测试（W28 Day5 D1 补齐：mcp_server 模块原 0% 覆盖）。

纯逻辑测试（CI 可跑，无需 MySQL/MCP 服务在线）：
- apikey_db：权限矩阵四分支 / hash 算法与平台一致 / 缓存 TTL 与上限淘汰
- auth：ScmApiKeyAuthProvider.verify_token 双路径
- main：require_permission 拒绝消息（回归：调用者名不得重复拼接）/
  audit_call 审计落盘 / _current_user 回退 MCP_RUN_AS

DB 相关（_query_user_by_key / _read_daily_brief）经 monkeypatch mock，
真实链路由容器内 mcp-server + 集成验收覆盖。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from app.mcp_server import apikey_db
from app.mcp_server.apikey_db import (
    _CACHE,
    TOOL_PERMISSIONS,
    check_tool_permission,
    hash_api_key,
    resolve_api_key,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


# ==================== check_tool_permission：权限矩阵四分支 ====================


def test_check_permission_whitelist_miss():
    """不在 TOOL_PERMISSIONS 白名单的工具（update_order 等）一律拒绝。"""
    ok, reason = check_tool_permission({"permissions": {"ops:order:read"}}, "update_order")
    assert ok is False
    assert "白名单" in reason


def test_check_permission_unauthenticated():
    """未认证（user=None）拒绝，消息不含调用者名（避免误导）。"""
    ok, reason = check_tool_permission(None, "query_order")
    assert ok is False
    assert "未认证" in reason


def test_check_permission_missing_code():
    """已认证但缺权限码：拒绝。★ 回归：reason 本身不再携带调用者名（由装饰器统一拼）。"""
    ok, reason = check_tool_permission(
        {"username": "viewer_t1", "permissions": {"kb:doc:read"}}, "query_order"
    )
    assert ok is False
    assert "ops:order:read" in reason
    assert "调用者" not in reason


def test_check_permission_ok():
    user = {"username": "admin_t1", "permissions": {"ops:order:read", "admin:brief:read"}}
    for tool in TOOL_PERMISSIONS:
        ok, _ = check_tool_permission(user, tool)
        assert ok is True


def test_tool_permissions_readonly_whitelist():
    """安全边界：白名单只含只读工具，高危写工具不得出现。"""
    assert set(TOOL_PERMISSIONS) == {"query_order", "query_inventory", "daily_report"}


# ==================== hash_api_key：与平台算法一致 ====================


def test_hash_api_key_matches_platform():
    from app.platform.apikeys import hash_api_key as platform_hash

    key = "sk-test-abc123"
    assert hash_api_key(key) == platform_hash(key)
    assert hash_api_key(key) == hashlib.sha256(key.encode()).hexdigest()


# ==================== resolve_api_key：前缀 + 缓存行为 ====================


def test_resolve_api_key_rejects_non_sk_prefix(monkeypatch):
    calls = []
    monkeypatch.setattr(apikey_db, "_query_user_by_key", lambda k: calls.append(k) or {"u": 1})
    assert resolve_api_key("jwt-like-token") is None
    assert calls == []


def test_resolve_api_key_cache_hit(monkeypatch):
    """第二次解析命中缓存，不再查库。"""
    calls = []
    monkeypatch.setattr(
        apikey_db, "_query_user_by_key", lambda k: calls.append(k) or {"user_id": 1}
    )
    first = resolve_api_key("sk-cache-hit")
    second = resolve_api_key("sk-cache-hit")
    assert first == second == {"user_id": 1}
    assert len(calls) == 1


def test_resolve_api_key_cache_expiry(monkeypatch):
    """过期后重新查库（权限变更近实时生效的前提）。"""
    calls: list[str] = []
    monkeypatch.setattr(
        apikey_db, "_query_user_by_key",
        lambda k: calls.append(k) or {"user_id": len(calls)},
    )
    resolve_api_key("sk-expire-me")
    # 手动把缓存时间拨回 TTL 之前
    ts, val = _CACHE["sk-expire-me"]
    _CACHE["sk-expire-me"] = (ts - apikey_db._CACHE_TTL - 1, val)
    resolve_api_key("sk-expire-me")
    assert len(calls) == 2


def test_resolve_api_key_cache_eviction(monkeypatch):
    """条目数超上限：过期项被淘汰；全部新鲜时整体清空（防无限增长）。"""
    monkeypatch.setattr(apikey_db, "_query_user_by_key", lambda k: None)
    for i in range(apikey_db._CACHE_MAX):
        resolve_api_key(f"sk-bulk-{i}")
    assert len(_CACHE) == apikey_db._CACHE_MAX
    resolve_api_key("sk-bulk-overflow")
    assert len(_CACHE) <= apikey_db._CACHE_MAX


def test_resolve_api_key_fail_closed_on_db_error(monkeypatch):
    """平台库故障 → None（fail-closed：无身份不放行；异常在 _query_user_by_key 内消化）。"""
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(apikey_db, "_connect", boom)
    assert resolve_api_key("sk-db-down") is None


# ==================== auth：verify_token 双路径 ====================


async def test_auth_provider_valid_token(monkeypatch):
    from app.mcp_server.auth import ScmApiKeyAuthProvider

    monkeypatch.setattr(
        "app.mcp_server.auth.resolve_api_key",
        lambda k: {"user_id": 7, "username": "admin_t1",
                   "tenant_id": "t1", "permissions": {"ops:order:read"}},
    )
    token = await ScmApiKeyAuthProvider().verify_token("sk-valid")
    assert token is not None
    assert token.subject == "admin_t1"
    assert token.scopes == ["ops:order:read"]
    assert token.claims["tenant_id"] == "t1"


async def test_auth_provider_invalid_token(monkeypatch):
    from app.mcp_server.auth import ScmApiKeyAuthProvider

    monkeypatch.setattr("app.mcp_server.auth.resolve_api_key", lambda k: None)
    assert await ScmApiKeyAuthProvider().verify_token("sk-bad") is None


# ==================== main：装饰器与身份回退 ====================


def test_require_permission_denial_message(monkeypatch):
    """★ 回归（消息重复 bug）：403 文本中调用者名只出现一次。"""
    from app.mcp_server import main as m

    monkeypatch.setattr(m, "_current_user", lambda ctx: {"username": "viewer_t1",
                                                          "permissions": set()})
    with pytest.raises(PermissionError) as ei:
        m.require_permission("query_order")(lambda **kw: None)(ctx=None)
    msg = str(ei.value)
    assert "[403]" in msg and "ops:order:read" in msg
    assert msg.count("viewer_t1") == 1


def test_require_permission_passes(monkeypatch):
    from app.mcp_server import main as m

    monkeypatch.setattr(m, "_current_user", lambda ctx: {"username": "admin_t1",
                                                          "permissions": {"ops:order:read"}})
    called = {}
    m.require_permission("query_order")(lambda **kw: called.update(ok=True))(ctx=None)
    assert called == {"ok": True}


def test_current_user_falls_back_to_env(monkeypatch):
    """stdio/本地：无 HTTP 请求上下文 → MCP_RUN_AS 模拟身份（默认 viewer，fail-closed）。"""
    from app.mcp_server import main as m

    monkeypatch.delenv("MCP_PERMISSIONS", raising=False)
    monkeypatch.setenv("MCP_RUN_AS", "operator_t9")
    user = m._current_user(None)
    assert user["username"] == "operator_t9"
    assert user["permissions"] == set()


def test_audit_call_writes_log(monkeypatch):
    """审计装饰器：正常路径落 ok，异常路径落 error 且异常向上抛。"""
    from app.mcp_server import main as m

    class FakeAudit:
        def __init__(self):
            self.entries: list[str] = []

        def log(self, event, **kw):
            self.entries.append(json.dumps({"event": event, **kw}, ensure_ascii=False))

    fake = FakeAudit()
    monkeypatch.setattr(m, "audit", fake)

    @m.audit_call("query_order")
    def ok_tool(**kw):
        return {"success": True, "error": None}

    @m.audit_call("query_order")
    def biz_error_tool(**kw):
        return {"success": False, "error": "order not found"}

    @m.audit_call("query_order")
    def boom_tool(**kw):
        raise ValueError("boom")

    assert ok_tool(ctx=None) == {"success": True, "error": None}
    biz_error_tool(ctx=None)
    with pytest.raises(ValueError):
        boom_tool(ctx=None)

    assert len(fake.entries) == 3
    statuses = [json.loads(e)["status"] for e in fake.entries]
    assert statuses == ["ok", "business_error", "error"]
    assert all(e.startswith("{\"event\": \"mcp_query_order\"") for e in fake.entries)


def test_read_daily_brief_no_row(monkeypatch):
    """无日报记录 → 明确 error（不假成功）。"""
    from app.mcp_server import main as m

    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **kw):
            pass

        def fetchone(self):
            return None

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return FakeCur()

    import pymysql  # noqa: F401  # 证明依赖可用

    monkeypatch.setattr(
        "pymysql.connect", lambda **kw: FakeConn()
    )
    res = m._read_daily_brief("2026-08-22")
    assert res["error"] == "无日报记录"


def test_read_daily_brief_db_error(monkeypatch):
    """平台库不可用 → 明确错误信息（不假成功）。"""
    from app.mcp_server import main as m

    def boom(**kw):
        raise RuntimeError("connect refused")

    monkeypatch.setattr("pymysql.connect", boom)
    res = m._read_daily_brief("")
    assert "日报读取失败" in res["error"]
