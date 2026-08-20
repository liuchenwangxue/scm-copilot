"""★ MCP Server HTTP 层认证（W28 Day5 D1）：平台 API Key AuthProvider（w6 模式）。

对应 w6 `auth.py`：`AuthProvider.verify_token(token)` 校验 `Authorization: Bearer sk-...`
→ 查平台 api_keys 表（sha256 哈希 + enabled + owner 存活 + 动态加载权限）→ 返回
AccessToken；无效/吊销/owner 禁用 → None → fastmcp HTTP 层 401 拒绝。

安全分层（w6 三层模型落地）：
- 认证层（本文件）：HTTP transport 下无 Key/错 Key 到不了工具函数（401）
- 工具白名单（apikey_db.TOOL_PERMISSIONS + main.require_permission）：按权限码拦
- 数据审计（main.audit_call）：谁/何时/结果，脱敏

stdio 模式：进程边界即安全边界，AuthProvider 不参与（fastmcp 已实测无副作用，
与 w6 notes.md 记录一致）——工具内回退 MCP_RUN_AS 环境变量模拟。
"""
from __future__ import annotations

from fastmcp.server.auth.auth import AccessToken, AuthProvider

from app.mcp_server.apikey_db import resolve_api_key


class ScmApiKeyAuthProvider(AuthProvider):
    """平台 API Key 认证：Bearer sk-... → 查平台库，无效返回 None（401）。"""

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url=base_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        user = resolve_api_key(token)
        if user is None:
            return None
        perms = sorted(user.get("permissions") or [])
        return AccessToken(
            token=token,
            client_id="scm-mcp-client",
            scopes=perms,                    # 权限码即 scope（工具级校验据此）
            subject=str(user.get("username", "unknown")),
            claims={"user_id": user.get("user_id"), "tenant_id": user.get("tenant_id"),
                    "permissions": perms},
        )


def get_auth_provider():
    """供 main.py 引用（懒加载，避免循环导入）。"""
    return ScmApiKeyAuthProvider()
