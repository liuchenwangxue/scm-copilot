"""★ 平台 MCP Server（W28 Day5，D1 资产回归）：FastMCP 包装 ops 只读工具。

对照 w6 供应链 FastMCP server（7 工具 + RBAC/审计/重试/幂等，双传输）——本文件是
w6 装饰器栈（@mcp.tool() → @audit_call → @require_permission）在平台的落地，
包装 **ops tool registry**（query_order / generate_report），平台正式具备 MCP server 侧。

边界（面试安全叙事一致）：
- **只暴露只读工具**：query_order / query_inventory / daily_report。
- **高危工具 update_order / cancel_order 不暴露**——MCP 调用方无法绕过审批流；
  若未来需要写操作，必须映射到平台 approval_gate（HITL），禁止图省事直接放行。
- 鉴权复用平台 API Key（`Authorization: Bearer sk-...` → apikey_db.resolve_api_key
  查平台库，权限继承 owner 用户）；stdio 模式进程即身份（MCP_RUN_AS 模拟）。
- 审计复用平台 AuditLogger（写 ops 审计文件），脱敏不落敏感值。

运行：
    python -m app.mcp_server.main                   # stdio（本地调试）
    python -m app.mcp_server.main --transport http  # HTTP（容器间，w6 client_http_test 模式）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from functools import wraps
from pathlib import Path

# ★ stdio 子进程 sys.path 修正：MCPClient 以脚本所在目录（backend/app/mcp_server）为
#   sys.path[0]，`import app` 需要 backend 在解析路径——加到 parents[2]（=backend）。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from fastmcp import FastMCP
from fastmcp.server.context import Context

from app.domains.ops import config as ops_config
from app.domains.ops.agent.tools.order_tools import OrderTools
from app.domains.ops.agent.tools.report_tools import ReportTools
from app.domains.ops.security.audit import AuditLogger
from app.mcp_server.apikey_db import check_tool_permission, resolve_api_key
from app.shared.reliability.redis_client import get_redis_client

SERVER_NAME = "scm-ops"
SERVER_VERSION = "1.0.0"
DESCRIPTION = (
    "SCM Copilot ops 只读工具 MCP Server（平台 API Key 鉴权 + 审计）。"
    "工具：query_order / query_inventory / daily_report。"
    "高危写操作（update_order/cancel_order）不暴露——走平台 REST + 审批流。"
)

# ★ HTTP 层认证挂载（w6 模式）：Bearer sk-... → 平台 API Key 校验（无效 401）；
#   stdio 模式认证不参与（进程边界即安全边界），工具内回退 MCP_RUN_AS。
from app.mcp_server.auth import get_auth_provider

mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION, instructions=DESCRIPTION,
              auth=get_auth_provider())

# 服务单例（与 ops graph.py 同构：熔断 Redis 共享、降级链内置）
# ★ echo=False：stdout 是 MCP JSON-RPC 协议通道，审计可见性由 audit_call 写 stderr
audit = AuditLogger(ops_config.AUDIT_LOG, echo=False)
order_tools = OrderTools(ops_config.BIZ_BASE_URL, redis_client=get_redis_client())
report_tools = ReportTools(ops_config.BIZ_BASE_URL, redis_client=get_redis_client())


# ==================== 身份解析（w6 get_identity 模式） ====================

def _current_user(ctx: Context | None) -> dict | None:
    """从请求上下文解析调用者。

    - HTTP 模式：Authorization Bearer sk- → 平台 API Key 校验（真实流程，查平台库）
    - stdio 模式：进程即身份，用环境变量 MCP_RUN_AS 模拟（本地开发）
    取不到真实身份时回退环境变量（默认最弱 'viewer'，fail-closed）。
    """
    user = None
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            user = resolve_api_key(auth[len("Bearer "):])
    except Exception:  # noqa: BLE001  # 合成 request/无 header → 回退
        pass
    if user is None:
        # stdio/本地：MCP_RUN_AS 模拟身份（默认 viewer 最弱权限，fail-closed）
        user = {
            "user_id": 0,
            "username": os.getenv("MCP_RUN_AS", "viewer"),
            "tenant_id": "local",
            "permissions": set(os.getenv("MCP_PERMISSIONS", "").split(",")) - {""},
        }
    return user


def get_identity(ctx: Context | None) -> dict:
    """审计用身份摘要（脱敏：只落用户名，Key 本身不进审计）。"""
    user = _current_user(ctx)
    return {"actor": (user or {}).get("username", "unknown"),
            "tenant": (user or {}).get("tenant_id", "unknown")}


def require_permission(tool_name: str):
    """工具级权限装饰器：只读白名单 + 平台权限码检查，未通过抛异常。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx")
            user = _current_user(ctx)
            ok, reason = check_tool_permission(user, tool_name)
            if not ok:
                raise PermissionError(f"[403] {reason}（调用者: {(user or {}).get('username')}）")
            return func(*args, **kwargs)
        return wrapper
    return decorator


def audit_call(tool_name: str):
    """审计装饰器（w6 audit.py 模式）：调用前后落审计（含身份/耗时/结果摘要）。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx")
            identity = get_identity(ctx)
            t0 = time.perf_counter()
            status, result = "ok", None
            try:
                result = func(*args, **kwargs)
                # 业务错误判定：error 键**非空**才算（ToolResult.to_dict 恒含 error=None 键）
                if isinstance(result, dict) and result.get("error"):
                    status = "business_error"
                return result
            except Exception as e:  # noqa: BLE001
                status = "error"
                result = {"error": str(e)}
                raise
            finally:
                audit.log(
                    f"mcp_{tool_name}",
                    actor=identity.get("actor", ""),
                    tenant=identity.get("tenant", ""),
                    status=status,
                    result_summary=str(result)[:200],
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                )
                # ★ MCP stdio 协议：审计日志必须走 stderr（stdout 是 JSON-RPC 通道）
                import sys as _sys
                print(f"[MCP-AUDIT] {tool_name} {identity.get('actor')} -> {status} "
                      f"({round((time.perf_counter() - t0) * 1000, 2)}ms)",
                      file=_sys.stderr, flush=True)
        return wrapper
    return decorator


# ==================== 工具区（只读白名单） ====================
# 装饰顺序（w6 务必记住）：@mcp.tool() 最外层（注册）-> @audit_call 中间（审计，包住权限）
# -> @require_permission 内层（权限）-> 函数体——权限拒绝也被审计（谁试图越权是安全事件）。

@mcp.tool()
@audit_call("query_order")
@require_permission("query_order")
def query_order(order_id: str, ctx: Context) -> dict:
    """查询采购订单状态与明细（只读）。需要 ops:order:read。"""
    result = order_tools.query_order(order_id)
    return result.to_dict()


@mcp.tool()
@audit_call("query_inventory")
@require_permission("query_inventory")
def query_inventory(ctx: Context) -> dict:
    """生成库存报表（只读，含低库存预警汇总）。需要 ops:order:read。"""
    result = report_tools.generate_report("inventory")
    return result.to_dict()


@mcp.tool()
@audit_call("daily_report")
@require_permission("daily_report")
def daily_report(ctx: Context, brief_date: str = "") -> dict:
    """查询经营日报（只读，默认最近一份）。需要 admin:brief:read。"""
    return _read_daily_brief(brief_date)


# ==================== 数据读取（daily_briefs 平台库） ====================

def _read_daily_brief(brief_date: str = "") -> dict:
    """从平台库 daily_briefs 表读日报（指定日期或最近一份）。

    复用 ApprovalService 的 parse_mysql_dsn（pymysql 同步，MCP server 独立进程
    无 FastAPI app.state.session_factory）。
    """
    import json as _json

    import pymysql
    from pymysql.cursors import DictCursor

    from app.domains.ops.security.approval import parse_mysql_dsn
    from app.platform.settings import settings

    try:
        conn = pymysql.connect(cursorclass=DictCursor, **parse_mysql_dsn(settings.platform_dsn))
        with conn, conn.cursor() as cur:
            if brief_date:
                cur.execute(
                    "SELECT brief_date, title, metrics, sqls FROM daily_briefs "
                    "WHERE brief_date=%s LIMIT 1",
                    (brief_date,),
                )
            else:
                cur.execute(
                    "SELECT brief_date, title, metrics, sqls FROM daily_briefs "
                    "ORDER BY brief_date DESC LIMIT 1"
                )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001  # 平台库不可用 → 明确错误（不假成功）
        return {"error": f"日报读取失败: {type(e).__name__}: {str(e)[:120]}"}

    if row is None:
        return {"error": "无日报记录", "brief_date": brief_date or "latest"}
    return {
        "brief_date": row["brief_date"],
        "title": row["title"],
        "metrics": _json.loads(row["metrics"]) if row.get("metrics") else {},
        "sqls": _json.loads(row["sqls"]) if row.get("sqls") else [],
    }


# ==================== 入口 ====================

def main():
    parser = argparse.ArgumentParser(description="SCM Copilot MCP Server（scm-ops）")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8765")))
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
