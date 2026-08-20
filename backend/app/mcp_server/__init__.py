"""MCP Server 资产（★ W28 Day5，D1）：FastMCP 包装 ops 只读工具。

- 复用面：w6 供应链 FastMCP server（RBAC/审计/重试/幂等）三层装饰器栈 +
  平台 ops tool registry（query_order/generate_report）+ 平台 API Key 鉴权。
- 高危工具（update_order/cancel_order）**不暴露**——MCP 面只读，写操作走平台 REST
  + approval_gate（边界注释见 main.py）。
"""
