"""MCP 工具注册表（★ W21 Day4）：把动态发现的 MCP 工具映射为 Agent 可调用工具。

设计（对齐项目 B 的 ToolRegistry 思路，轻量版）：
- 不写死工具：从 MCPClient.list_tools() 动态发现 → 注册为 MCPTool
- MCPTool 契约：{name, description, input_schema, call}——
  与 LLM function calling schema 对齐（describe_for_llm 供意图路由/工具选择）
- 用途：项目 A（RAG 问答）可组合"知识库检索 + MCP 外部工具"回答——
  MCP 工具提供知识库没有的事实数据（订单/物流实时状态），知识库提供制度条文

接口：
    MCPTool(name, description, input_schema, call)
    MCPToolRegistry
    .register_tool / .register_mcp_tools(mcp_client) / .names() / .get() / .all()
    .describe_for_llm() -> list[dict]   # function calling schema
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict
    call: Callable[..., Any]      # async 或 sync：调用 MCP 工具

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.input_schema}


class MCPToolRegistry:
    def __init__(self):
        self._tools: dict[str, MCPTool] = {}

    def register_tool(self, tool: MCPTool) -> None:
        self._tools[tool.name] = tool

    async def register_mcp_tools(self, mcp_client) -> int:
        """从 MCPClient 动态发现并注册全部工具。返回注册数。

        list_tools 是 async → 本方法 async，调用方 await（demo 里已 await）。"""
        tools = await mcp_client.list_tools()
        n = 0
        for t in tools:
            name = t["name"]
            self._tools[name] = MCPTool(
                name=name,
                description=t["description"],
                input_schema=t["input_schema"],
                call=lambda args, _n=name, _c=mcp_client: _c.call_tool(_n, args),
            )
            n += 1
        return n

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> MCPTool | None:
        return self._tools.get(name)

    def all(self) -> list[MCPTool]:
        return list(self._tools.values())

    def describe_for_llm(self) -> list[dict]:
        """转 LLM function calling schema（供意图路由/工具选择）。"""
        return [t.describe() for t in self._tools.values()]
