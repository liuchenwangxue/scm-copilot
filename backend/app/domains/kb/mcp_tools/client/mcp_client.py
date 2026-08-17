"""MCP Client（★ W21 Day4）：用官方 mcp SDK 消费第三方 MCP Server（stdio）。

为什么（面试 9 题素材）：
- W6 做了 MCP Server（工具供给侧），Day4 做 Client（消费侧）——生态闭环
- 动态发现工具列表：session.list_tools() 拿 {name, description, inputSchema}
  → 不写死任何工具，server 新增工具客户端自动可用（"工具即插即用"）
- stdio 子进程：MCP server 作为子进程启动（官方 StdioServerParameters），
  与 server 解耦（任何 stdio MCP server 都能接，GitHub MCP / Playwright MCP 同理）

关键点（手册坑提示）：
- 本地自建 MCP server（W6 server.py）当 client 目标——公开 server 需要网络/认证
- Windows 上子进程必须用完整 python 路径（sys.executable）
- server 的日志必须走 stderr（stdout 是 JSON-RPC 协议通道，W6 audit 已修复）

接口：
    MCPClient(server_script, python=None, cwd=None, env=None)
    async .connect()                      # 建立 stdio 连接 + initialize
    async .list_tools() -> list[dict]     # 动态发现工具 [{name, description, input_schema}]
    async .call_tool(name, args) -> str   # 调用工具，返回文本结果
    async .close()
    支持 async with（自动 connect/close）
"""
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 默认连接本地自建 MCP server（W6 供应链域，learning-outputs/w6-mcp-server），环境变量可覆盖
# mcp_client.py 迁移后位于 .../learning-outputs/scm-copilot/backend/app/domains/kb/mcp_tools/client/
DEFAULT_SERVER = (Path(__file__).resolve().parents[7] / "w6-mcp-server" / "server.py")
# 默认 python：当前进程的 python（共享 .venv）
DEFAULT_PYTHON = sys.executable


class MCPClient:
    def __init__(self, server_script=None, python=None, cwd=None, env=None):
        self.server_script = str(server_script or DEFAULT_SERVER)
        self.python = python or DEFAULT_PYTHON
        self.cwd = str(cwd or Path(self.server_script).parent)
        self.env = env or dict(os.environ)
        # stdio 协议日志必须走 stderr（W6 audit 修复），fastmcp 自身日志降级
        self.env.setdefault("FASTMCP_LOG_LEVEL", "ERROR")
        self.env.setdefault("PYTHONIOENCODING", "utf-8")
        self._session = None
        self._read = None
        self._write = None
        self._client_ctx = None
        self.server_info = None

    async def connect(self):
        """启动子进程 + 建立 session + initialize。"""
        params = StdioServerParameters(command=self.python, args=[self.server_script],
                                       env=self.env, cwd=self.cwd)
        from contextlib import AsyncExitStack
        self._stack = AsyncExitStack()
        self._client_ctx = await self._stack.enter_async_context(stdio_client(params))
        self._read, self._write = self._client_ctx
        self._session = await self._stack.enter_async_context(ClientSession(self._read, self._write))
        init = await self._session.initialize()
        self.server_info = {
            "name": init.serverInfo.name,
            "version": init.serverInfo.version,
            "protocolVersion": init.protocolVersion,
        }
        return self

    async def list_tools(self) -> list[dict]:
        """动态发现工具列表（MCP 核心能力：不写死，server 加了工具自动可见）。"""
        if self._session is None:
            raise RuntimeError("MCPClient 未 connect")
        res = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description,
             "input_schema": t.inputSchema}
            for t in res.tools
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """调用工具。返回工具文本输出（MCP CallToolResult content 拼接）。"""
        if self._session is None:
            raise RuntimeError("MCPClient 未 connect")
        res = await self._session.call_tool(name, args)
        texts = []
        for c in (res.content or []):
            if getattr(c, "type", "") == "text":
                texts.append(c.text)
        return "\n".join(texts) if texts else str(res)

    async def close(self):
        await self._stack.aclose()

    # ---- async with ----
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
