"""★ data 域图以自研 Runtime 形态迁移（W28-D6，B5 PoC）——与 LangGraph 版同构对照。

定位（对应《W28学习执行手册》Day6 上午第 1 条）：
- 节点函数 / 路由函数 与 `app.domains.data.graph` **完全复用**（同一个函数对象）；
  区别只在"引擎"：LangGraph StateGraph vs 自研 `run_graph`（shared/runtime/loop.py）。
- 拓扑：generate → validate →（reject | execute | repair | degrade）→ … 与 LangGraph 版
  逐边一致（见 `DATA_GRAPH_ROUTER_MAP`）。
- 同输入同输出对照测试见 `test_runtime_loop.py::test_data_graph_isomorphic_*`——
  mock 全链路（generate 用评测集 gold SQL）跑通，结果与 `data_graph.ainvoke` 一致。

为什么 data 域适合自研内核（ADR-011）：
- data 图单轮无状态、无 checkpointer/interrupt（多轮上下文在图上之外的 session_ctx）；
- 7 节点 + 3 路由 = 简单 DAG，LangGraph 只提供"图结构 + 执行器"，这部分 100 行自研
  内核完全覆盖；
- ops 域有 interrupt/checkpointer（HITL 审批）——那是 LangGraph 主场，**不迁**。

对外接口：`run_data_runtime(state) -> dict`（async），与 `data_graph.ainvoke` 同签名；
service.py 只需把 `data_graph.ainvoke(...)` 换成 `run_data_runtime(...)` 即可切换
（本日仅对照测试，不切换线上路径）。
"""

from __future__ import annotations

from typing import Any

from app.domains.data.graph import (
    degrade_node,
    execute_node,
    format_node,
    generate_node,
    reject_node,
    repair_node,
    route_after_execute,
    route_after_repair,
    route_after_validate,
    validate_node,
)
from app.shared.runtime.loop import run_graph

# 图拓扑（与 graph.py 中 builder 的边一一对应）：
#   START → generate → validate
#   validate: route_after_validate → reject | execute | repair | degrade
#   reject → END
#   execute: route_after_execute → format | repair
#   repair: route_after_repair → validate | degrade
#   degrade → END ; format → END
DATA_GRAPH_NODES: dict[str, Any] = {
    "generate": generate_node,
    "validate": validate_node,
    "reject": reject_node,
    "execute": execute_node,
    "repair": repair_node,
    "degrade": degrade_node,
    "format": format_node,
}

DATA_GRAPH_EDGES: dict[str, str] = {
    "generate": "validate",
    "reject": "",  # END
    "degrade": "",
    "format": "",
}

DATA_GRAPH_ROUTER_MAP: dict[str, tuple[Any, dict[str, str]]] = {
    "validate": (
        route_after_validate,
        {"reject": "reject", "execute": "execute", "repair": "repair", "degrade": "degrade"},
    ),
    "execute": (route_after_execute, {"format": "format", "repair": "repair"}),
    "repair": (route_after_repair, {"validate": "validate", "degrade": "degrade"}),
}


def run_data_runtime(state: dict[str, Any]) -> Any:
    """以自研内核执行 data 图（async），签名与 `data_graph.ainvoke` 一致。"""
    return run_graph(
        start="generate",
        nodes=DATA_GRAPH_NODES,
        edges=DATA_GRAPH_EDGES,
        router_map=DATA_GRAPH_ROUTER_MAP,
        state=state,
    )
