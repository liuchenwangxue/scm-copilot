"""★ W28 Day5（D1 MCP Server 资产回归）：dogfooding 闭环测试。

kb 域自己的 MCPClient（W21 资产）连平台自己的 MCP server（FastMCP 包装 ops registry）
——"自产自销"：跨域调一次 query_order/query_inventory/daily_report，验证：
- 三只读工具可被 kb client 调通（工具发现动态性：list_tools 不写死）
- 权限拦截：无权限调用者（viewer）被 403 拒绝
- 高危写工具（update_order/cancel_order）**不暴露**（安全边界）
- 审计落盘（mcp_* 事件写入 ops audit.log）

覆盖手册验收："MCP server 三只读工具可被 kb client 调通（dogfooding 证据）"。
依赖探测（★ CI 兼容，对齐 test_session_ctx_redis.py 的 skip 惯例）：
- query_order/query_inventory 依赖 **mock-biz**（CI 无此服务）→ 不可达则 skip
- daily_report 依赖 **daily_briefs 表记录**（调度产物，CI 不自动生成）→ 无记录则 skip
- list_tools/viewer 权限/审计 只依赖 MCP server 本身 → 恒跑（CI 也在跑）
"""
import os
import sys
from pathlib import Path

import pytest

from app.domains.kb.mcp_tools.client.mcp_client import MCPClient
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[1]
SERVER = BACKEND / "app" / "mcp_server" / "main.py"
PY = sys.executable


def _biz_ready() -> bool:
    """mock-biz 可达性探测（health 端点 200 + status=ok）。

    CI 无 mock-biz → 连接失败返回 False → 相关 dogfooding 用例 skip
    （本地 compose 全栈在跑 → 正常执行，验收证据仍覆盖）。
    """
    import httpx

    from app.domains.ops import config as ops_config

    try:
        r = httpx.get(f"{ops_config.BIZ_BASE_URL}/health", timeout=2)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:  # noqa: BLE001  # 连接拒绝/超时/非 JSON 一律视为不在线
        return False


async def _brief_ready() -> bool:
    """daily_briefs 表是否有记录（平台库 reachable 且 ≥1 行）。

    CI 只跑 migrate+seed（seed 不产日报）→ 无记录返回 False → 日报用例 skip；
    daily_briefs 由调度任务 daily_brief 产出（W25 Day3），本地全栈已产。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    try:
        engine = create_async_engine(TEST_DSN)
        try:
            async with engine.connect() as conn:
                row = await conn.execute(text("SELECT COUNT(*) FROM daily_briefs"))
                return int(row.scalar_one()) > 0
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001  # 平台库不可达按"无数据"处理 → skip
        return False


def _env(run_as: str, perms: str) -> dict:
    env = dict(os.environ)
    # stdio 模式：进程即身份，MCP_RUN_AS + MCP_PERMISSIONS 模拟（默认 viewer 最弱，fail-closed）
    env["MCP_RUN_AS"] = run_as
    env["MCP_PERMISSIONS"] = perms
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


async def _make_client(run_as: str, perms: str) -> MCPClient:
    """连接 MCP server 并返回 client（手动 connect——避免 async with 的 anyio
    cancel-scope 在 pytest-asyncio loop 下的 teardown 异常）。"""
    client = MCPClient(server_script=str(SERVER), python=PY, cwd=str(BACKEND),
                       env=_env(run_as, perms))
    await client.connect()
    return client


@pytest.fixture
async def client_admin():
    """admin 身份（有 ops:order:read + admin:brief:read）的 MCP client。"""
    from contextlib import suppress

    c = await _make_client("admin_t_huadong", "ops:order:read,admin:brief:read")
    yield c
    with suppress(Exception):  # noqa: BLE001  # stdio teardown 的 cancel-scope 噪声
        await c.close()


@pytest.fixture
async def client_viewer():
    """viewer 身份（无任何 MCP 权限，fail-closed）的 MCP client。"""
    from contextlib import suppress

    c = await _make_client("viewer", "")
    yield c
    with suppress(Exception):  # noqa: BLE001
        await c.close()


async def test_list_tools_dynamic_discovery(client_admin):
    """动态工具发现：三只读工具在列，高危写工具不暴露（安全边界）。"""
    tools = await client_admin.list_tools()
    names = {t["name"] for t in tools}
    assert {"query_order", "query_inventory", "daily_report"} <= names
    # 高危写工具绝对不在白名单（update_order/cancel_order 不暴露）
    assert "update_order" not in names
    assert "cancel_order" not in names


async def test_query_order_dogfood(client_admin):
    """kb client 跨域调平台 query_order（订单真实数据，PO-0001 存在）。"""
    if not _biz_ready():
        pytest.skip("mock-biz 不在线，跳过跨服务 dogfood（本地 compose 全栈可跑）")
    raw = await client_admin.call_tool("query_order", {"order_id": "PO-0001"})
    assert "PO-0001" in raw
    assert "success" in raw


async def test_query_inventory_dogfood(client_admin):
    """query_inventory 走 ReportTools.generate_report(inventory)，真实库存报表。"""
    if not _biz_ready():
        pytest.skip("mock-biz 不在线，跳过跨服务 dogfood（本地 compose 全栈可跑）")
    raw = await client_admin.call_tool("query_inventory", {})
    assert "inventory" in raw
    assert "low_stock" in raw


async def test_daily_report_dogfood(client_admin):
    """daily_report 从平台库 daily_briefs 表取最近一份（可能无指标但结构完整）。"""
    if not await _brief_ready():
        pytest.skip("daily_briefs 无记录（调度产物未生成），跳过日报 dogfood")
    raw = await client_admin.call_tool("daily_report", {})
    assert "brief_date" in raw  # 结构存在即可（metrics 可为 null，COALESCE 兜底）
    assert "title" in raw


async def test_viewer_permission_denied(client_viewer):
    """无权限调用者：query_order 被 403 拒绝（MCP_PERMISSIONS 空 = viewer fail-closed）。

    fastmcp 把工具异常转成 isError 的 CallToolResult——客户端 call_tool 返回
    "Error calling tool ..." 文本而非抛异常（安全边界 = 服务器拒绝，不外泄）。
    """
    raw = await client_viewer.call_tool("query_order", {"order_id": "PO-0001"})
    assert "403" in raw or "缺少权限码" in raw


async def test_audit_logged(client_admin):
    """MCP 工具调用落审计（mcp_* 事件写入 ops audit.log）。"""
    from app.domains.ops import config as ops_config
    from app.domains.ops.security.audit import AuditLogger

    logger = AuditLogger(ops_config.AUDIT_LOG)
    before = len(logger.filter("mcp_query_order"))
    await client_admin.call_tool("query_order", {"order_id": "PO-0002"})
    after = len(logger.filter("mcp_query_order"))
    assert after > before
