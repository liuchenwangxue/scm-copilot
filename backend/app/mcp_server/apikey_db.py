"""MCP Server 鉴权辅助（★ W28 Day5 D1）：平台 API Key → 用户/权限（同步 MySQL）。

为什么独立模块（面试可讲）：
- MCP server 是**独立进程**（stdio/HTTP 双 transport），没有 FastAPI app.state.session_factory
  （async SQLAlchemy 绑定事件循环），故用 pymysql 同步直连平台库——与 ApprovalService
  同款模式（parse_mysql_dsn 复用），双实例无状态、MySQL 是唯一权威。
- 校验语义与 `apikeys.authenticate_api_key` 完全一致（★ W25 Day5）：
  sha256 哈希匹配 → enabled=1 → owner 用户存活 → 动态加载 owner 当前权限码
  （MCP 调用方 = 机器身份，权限继承 owner 用户，变更实时生效）。
- 只读工具权限码映射（与平台 seed_platform.py 权限码一致）：
    query_order    -> ops:order:read     （admin/operator）
    query_inventory -> ops:order:read     （admin/operator）
    daily_report   -> admin:brief:read    （admin，日报含经营数字）
- stdio 模式（本地调试/dogfooding）：进程即身份，回退环境变量 MCP_RUN_AS
  （默认最弱 viewer，fail-closed）——与 w6 server.py 同款设计。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.domains.ops.security.approval import parse_mysql_dsn
from app.platform.settings import settings

# 工具 -> 所需权限码（只读工具白名单；高危工具 update_order/cancel_order 不在此表 =
# 不暴露，见 main.py 边界注释）
TOOL_PERMISSIONS: dict[str, str] = {
    "query_order": "ops:order:read",
    "query_inventory": "ops:order:read",
    "daily_report": "admin:brief:read",
}


def hash_api_key(key: str) -> str:
    """与平台 apikeys.hash_api_key 同算法（sha256）。"""
    return hashlib.sha256(key.encode()).hexdigest()


def _connect():
    """pymysql 同步连接平台库（从 settings.platform_dsn 派生）。"""
    import pymysql
    from pymysql.cursors import DictCursor

    return pymysql.connect(cursorclass=DictCursor, **parse_mysql_dsn(settings.platform_dsn))


# 进程内短缓存：MCP 每请求都查库成本高（HTTP 模式每工具调用一次），
# 30s 过期（与 JWT access 15min 相比更紧，权限变更近实时生效）
_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = 30.0


def resolve_api_key(key: str) -> dict | None:
    """API Key -> {user_id, username, permissions:set}；无效/吊销/owner 禁用 -> None。

    与平台 authenticate_api_key 语义一致（动态加载 owner 权限，零查库差异）。
    """
    if not key.startswith("sk-"):
        return None
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    user = _query_user_by_key(key)
    _CACHE[key] = (now, user)
    return user


def _query_user_by_key(key: str) -> dict | None:
    key_hash = hash_api_key(key)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, owner_user_id, enabled FROM api_keys WHERE key_hash=%s", (key_hash,)
            )
            row = cur.fetchone()
            if row is None or row["enabled"] != 1 or not row["owner_user_id"]:
                return None
            uid = row["owner_user_id"]
            cur.execute(
                "SELECT id, username, tenant_id, status FROM users WHERE id=%s", (uid,)
            )
            u = cur.fetchone()
            if u is None or u["status"] != 1:
                return None
            # 动态加载 owner 用户当前权限（与 apikeys.authenticate_api_key 同 SQL）
            cur.execute(
                "SELECT DISTINCT p.code FROM permissions p "
                "JOIN role_permissions rp ON rp.permission_id = p.id "
                "JOIN user_roles ur ON ur.role_id = rp.role_id "
                "JOIN users u2 ON u2.id = ur.user_id "
                "WHERE u2.id=%s AND u2.status=1",
                (uid,),
            )
            perms = {r["code"] for r in cur.fetchall()}
        return {
            "user_id": u["id"],
            "username": u["username"],
            "tenant_id": u["tenant_id"],
            "permissions": perms,
        }
    except Exception:  # noqa: BLE001  # 平台库故障：fail-closed 拒绝（无身份不放行）
        return None


def check_tool_permission(user: dict | None, tool_name: str) -> tuple[bool, str]:
    """工具级权限：调用者权限集是否含该工具所需权限码。"""
    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        return False, f"工具 '{tool_name}' 未在 MCP 白名单（不对外暴露）"
    if user is None:
        return False, "未认证（API Key 无效/已吊销/owner 禁用）"
    if required not in user["permissions"]:
        return False, f"缺少权限码 '{required}'（调用者: {user.get('username', '?')}）"
    return True, ""
