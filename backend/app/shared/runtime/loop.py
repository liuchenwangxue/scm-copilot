"""★ 自研 Runtime PoC 最小内核（W28-D6，B5 项）——图节点循环 + 原生 tool-calling 循环。

设计目标（对应《W28学习执行手册》Day6）：
1. **内核双形态**：
   - `run_graph`：图节点循环（data 域同构——把 LangGraph 版 data 图的 7 节点 + 3 路由
     函数搬到自研引擎上，同输入同输出对照测试）；
   - `run_tool_loop`：**原生 tool-calling 循环**（w11/w12 路径回归）——`tools schema →
     tool_calls → registry 执行 → 回填 → 终答`，这正是 w11 day2_tool_demo / w12 的
     SDK 标准循环；与平台现有"结构化 JSON 选工具"形成双路径对照。
2. **同构对照**：data 图以 `(nodes, router_map)` 描述图拓扑，自研引擎只做三件事——
   按拓扑走到节点、执行节点函数、按路由函数决定下一步；与 LangGraph 的 ainvoke
   同输入同输出（对照测试见 `test_runtime_loop.py::test_data_graph_isomorphic_*`）。
3. **熔断底线**：`max_steps` 防环（repair→validate→repair 死循环必须有熔断）；
   缺节点/路由键 → `RuntimeNodeError`（显式失败，不静默吞）。

接口（面试讲清楚"框架税"的对照物）：
    ToolSchema            # 工具 schema：name / description / input_schema(JSON Schema)
    ToolCall              # 原生协议形态的调用：id / name / args
    tool_result(tc_id, text) -> dict   # 回填消息（role="tool"，对照 OpenAI/Claude 协议）
    run_tool_loop(llm, tools, registry, state, max_steps) -> str   # 原生 tool-calling 循环
    run_graph(start, nodes, edges, router_map, state, max_steps) -> dict  # 图节点循环
    ToolLoopError / RuntimeNodeError   # max_steps 熔断 / 拓扑错误

与 LangGraph 的取舍（ADR-011）：
- 自研：100 行可控内核、零依赖、mock 友好（w11/w12 SDK 形态）；但无 checkpointer/
  interrupt/持久化——只适合单轮无状态子图（data 域）；
- LangGraph：checkpointer/interrupt 生态（ops HITL 主场）保留；kb/ops 不迁；
- 混合运行：data 域可随时切自研内核，对外接口不变（service.py 只需改一行）。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

# ---- 类型 ----

NodeFn = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
RouterFn = Callable[[dict[str, Any]], str]


class ToolLoopError(RuntimeError):
    """tool-calling 循环超过 max_steps（熔断）。"""


class RuntimeNodeError(RuntimeError):
    """图拓扑错误：缺节点 / 路由函数返回未知键。"""


# ================= 原生 tool-calling 循环（w11/w12 路径回归） =================


class ToolSchema:
    """工具 schema（对照 OpenAI tools / Claude tool_use 的输入描述）。"""

    def __init__(self, name: str, description: str, input_schema: dict | None = None):
        self.name = name
        self.description = description
        self.input_schema = input_schema or {
            "type": "object",
            "properties": {},
        }

    def as_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolCall:
    """一次工具调用（原生协议形态：由 llm 的 tool_calls 给出）。"""

    def __init__(self, name: str, args: dict, call_id: str | None = None):
        self.id = call_id or f"call_{uuid.uuid4().hex[:8]}"
        self.name = name
        self.args = args


def tool_result(tool_call_id: str, text: str) -> dict:
    """工具执行结果回填消息（对照 OpenAI/Claude 的 tool/tool_result 协议）。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": text}


def run_tool_loop(
    llm: Callable[[list[dict], list[dict]], Awaitable[Any]],
    tools: list[ToolSchema],
    registry: dict[str, Callable[[dict], Awaitable[str] | str]],
    messages: list[dict],
    max_steps: int = 10,
) -> Awaitable[str]:
    """原生 tool-calling 循环内核：tools schema → tool_calls → registry 执行 → 回填 → 终答。

    - llm(messages, tool_schemas) 返回含 `text`（终答）与 `tool_calls`（列表）的对象；
    - 有 tool_calls → 逐个执行 registry[name](args) → tool_result 回填 → 下一轮；
    - 无 tool_calls（终答）→ 返回 text；超过 max_steps → ToolLoopError 熔断。
    """

    async def _loop() -> str:
        schemas = [t.as_dict() for t in tools]
        for _step in range(max_steps):
            resp = await llm(messages, schemas)
            calls = getattr(resp, "tool_calls", None) or []
            if not calls:
                return getattr(resp, "text", "") or ""
            for tc in calls:
                fn = registry[tc.name]
                result = fn(tc.args)
                if hasattr(result, "__await__"):  # async 工具
                    result = await result
                # 回填进调用方传入的 messages（与手册 `state["messages"].append` 同语义，
                # 调用方可观察工具执行轨迹）
                messages.append(tool_result(tc.id, str(result)))
        raise ToolLoopError(f"tool loop exceeded max_steps={max_steps}")

    return _loop()


# ================= 图节点循环（data 域同构） =================


def run_graph(
    start: str,
    nodes: dict[str, NodeFn],
    edges: dict[str, str],
    router_map: dict[str, tuple[RouterFn, dict[str, str]]],
    state: dict[str, Any],
    max_steps: int = 32,
) -> Awaitable[dict[str, Any]]:
    """最小图引擎：按拓扑执行节点 → 路由函数决定下一步 → 汇合到终态。

    - nodes: {name: async/sync 节点函数}，节点函数返回 partial state（与 LangGraph 同语义）；
    - edges: {node: next_node} 静态边；router_map: {node: (router_fn, {key: next})} 条件边；
    - 路由键未命中 → RuntimeNodeError（显式失败）；超过 max_steps → ToolLoopError 熔断；
    - END 用空串表示（终点）。
    """

    async def _run() -> dict[str, Any]:
        current = start
        merged: dict[str, Any] = dict(state)
        for _step in range(max_steps):
            if current == "":
                return merged
            if current not in nodes:
                raise RuntimeNodeError(f"unknown node: {current}")
            out = nodes[current](merged)
            if hasattr(out, "__await__"):
                out = await out  # type: ignore[assignment]
            merged.update(out or {})
            if current in router_map:
                router, mapping = router_map[current]
                key = router(merged)
                if key not in mapping:
                    raise RuntimeNodeError(f"router '{current}' returned unknown key: {key}")
                current = mapping[key]
            elif current in edges:
                current = edges[current]
            else:
                raise RuntimeNodeError(f"node '{current}' has no outgoing edge")
        raise ToolLoopError(f"graph exceeded max_steps={max_steps}")

    return _run()
