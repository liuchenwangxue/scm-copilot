"""★ W28-D6 Runtime PoC 测试：tool-calling 循环内核单测 + data 图同构对照（B5/D2）。

对应《W28学习执行手册》Day6 验收：
- 自研 loop 与 LangGraph 同构对照绿（同输入同输出）；
- tool-calling 循环内核单测绿（原生协议形态：tools schema → tool_calls → 执行 → 回填 → 终答）；
- max_steps 熔断（repair→validate→repair 死循环底线）。

分层：
- 纯逻辑（CI 可跑）：ToolSchema/ToolCall/tool_result/run_tool_loop/run_graph 内核单测；
- 同构对照（integration，需 MySQL + scm_biz seed + mock 生成器）：data 图两引擎对拍。
"""

import os

import pytest

os.environ["LLM_PROVIDER"] = "mock"

from app.shared.runtime.loop import (  # noqa: E402
    RuntimeNodeError,
    ToolCall,
    ToolLoopError,
    ToolSchema,
    run_graph,
    run_tool_loop,
    tool_result,
)

pytestmark = pytest.mark.integration


# ================= 原生 tool-calling 循环内核单测（纯逻辑） =================


class _FakeLLM:
    """可编程 mock LLM：按轮次返回 tool_calls / 终答。"""

    def __init__(self, script: list):
        self.script = script
        self.rounds = 0

    async def __call__(self, messages, schemas):
        item = self.script[self.rounds] if self.rounds < len(self.script) else None
        self.rounds += 1
        if item is None:
            return _Resp(text="终答", tool_calls=[])
        return _Resp(text="", tool_calls=[item])


class _Resp:
    def __init__(self, text: str, tool_calls: list):
        self.text = text
        self.tool_calls = tool_calls


def _echo_tool(args: dict) -> str:
    """同步工具：回显参数（模拟 registry 执行）。"""
    return f"echo:{args.get('v', '')}"


async def _async_tool(args: dict) -> str:
    """异步工具：registry 里 async 工具也要能跑（await 分支）。"""
    return f"async:{args.get('v', '')}"


def test_tool_loop_immediate_answer():
    """无 tool_calls → 终答（不执行任何工具）。"""
    llm = _FakeLLM([])
    out = run_tool_loop(llm, [], {}, [], max_steps=3)
    # run_tool_loop 返回 awaitable
    import asyncio

    assert asyncio.run(out) == "终答"
    assert llm.rounds == 1


def test_tool_loop_single_tool_then_answer():
    """tool_calls → registry 执行 → tool_result 回填 → 下一轮终答。"""
    llm = _FakeLLM([ToolCall("echo", {"v": "abc"})])
    registry = {"echo": _echo_tool}
    messages: list[dict] = []
    out = run_tool_loop(
        llm, [ToolSchema("echo", "echo tool", {"type": "object"})], registry, messages
    )
    import asyncio

    text = asyncio.run(out)
    assert text == "终答"
    # 回填消息进 messages（工具执行结果可见）
    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": messages[-1]["tool_call_id"],
        "content": "echo:abc",
    }


def test_tool_loop_multiple_tools_in_one_turn():
    """一轮内多个 tool_calls 顺序执行（w11 day2 同轮多工具语义）。"""
    llm = _FakeLLM([ToolCall("echo", {"v": "a"}), ToolCall("echo", {"v": "b"})])
    registry = {"echo": _echo_tool}
    messages: list[dict] = []
    out = run_tool_loop(llm, [ToolSchema("echo", "echo")], registry, messages)
    import asyncio

    assert asyncio.run(out) == "终答"
    # 两个回填都进入 messages（内容 a、b 都在）
    contents = [m["content"] for m in messages if m["role"] == "tool"]
    assert contents == ["echo:a", "echo:b"]


def test_tool_loop_async_tool():
    """async 工具执行（registry 工具可 await）。"""
    llm = _FakeLLM([ToolCall("async_tool", {"v": "x"})])
    registry = {"async_tool": _async_tool}
    messages: list[dict] = []
    out = run_tool_loop(llm, [ToolSchema("async_tool", "async")], registry, messages)
    import asyncio

    assert asyncio.run(out) == "终答"
    assert messages[-1]["content"] == "async:x"


def test_tool_loop_max_steps_raises():
    """超过 max_steps 熔断（防死循环底线）。"""
    llm = _FakeLLM([ToolCall("echo", {"v": "loop"})] * 10)  # 永远调工具，不终答
    registry = {"echo": _echo_tool}
    out = run_tool_loop(llm, [ToolSchema("echo", "echo")], registry, [], max_steps=2)
    import asyncio

    with pytest.raises(ToolLoopError):
        asyncio.run(out)


def test_tool_loop_unknown_tool_raises_keyerror():
    """registry 缺工具 → KeyError（显式失败，不静默吞）。"""
    llm = _FakeLLM([ToolCall("nope", {})])
    out = run_tool_loop(llm, [ToolSchema("echo", "echo")], {}, [])
    import asyncio

    with pytest.raises(KeyError):
        asyncio.run(out)


def test_tool_schema_as_dict():
    """ToolSchema.as_dict() → OpenAI function 协议形态（w12 对照）。"""
    s = ToolSchema("query_order", "查订单", {"type": "object", "properties": {"id": {}}})
    d = s.as_dict()
    assert d["type"] == "function"
    assert d["function"]["name"] == "query_order"
    assert d["function"]["parameters"]["type"] == "object"


def test_tool_call_default_id_unique():
    """ToolCall 无 call_id 时自动生成（默认 ID 唯一）。"""
    a, b = ToolCall("t", {}), ToolCall("t", {})
    assert a.id != b.id


def test_tool_result_message_shape():
    """tool_result → OpenAI/Claude 兼容的回填消息。"""
    msg = tool_result("call_1", "结果是 42")
    assert msg == {"role": "tool", "tool_call_id": "call_1", "content": "结果是 42"}


# ================= 图节点循环内核单测（纯逻辑） =================


async def _node_a(state):
    return {"a": state.get("a", 0) + 1}


def _node_b(state):
    return {"b": state["a"] * 2}


async def test_run_graph_linear():
    """静态边线性图：a → b → END。"""
    out = await run_graph("a", {"a": _node_a, "b": _node_b}, {"a": "b", "b": ""}, {}, {"a": 0})
    assert out == {"a": 1, "b": 2}


async def test_run_graph_router():
    """条件边：路由函数决定 next（模拟 validate→reject/execute）。"""

    def route(state):
        return "reject" if state.get("bad") else "ok"

    async def validate(state):
        return {"validated": not state.get("bad", False)}

    async def ok(state):
        return {"reply": "ok"}

    async def reject(state):
        return {"reply": "rejected"}

    out = await run_graph(
        "validate",
        {"validate": validate, "ok": ok, "reject": reject},
        {"ok": "", "reject": ""},
        {"validate": (route, {"ok": "ok", "reject": "reject"})},
        {"bad": True},
    )
    assert out["reply"] == "rejected"


async def test_run_graph_unknown_router_key():
    """路由函数返回未知键 → RuntimeNodeError（显式失败）。"""

    async def validate(state):
        return {}

    def route(state):
        return "mystery"

    with pytest.raises(RuntimeNodeError):
        await run_graph(
            "validate", {"validate": validate}, {}, {"validate": (route, {"ok": "ok"})}, {}
        )


async def test_run_graph_max_steps_raises():
    """图内环（无 END 可达）→ max_steps 熔断。"""

    async def loop_node(state):
        return {"n": state.get("n", 0) + 1}

    with pytest.raises(ToolLoopError):
        await run_graph("a", {"a": loop_node}, {"a": "a"}, {}, {}, max_steps=5)


# ================= data 图同构对照（integration：MySQL + mock） =================

from app.domains.data.executor import dispose_engine  # noqa: E402
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.runtime_graph import run_data_runtime  # noqa: E402


@pytest.mark.asyncio
async def test_data_graph_isomorphic_legal_question():
    """同构对照：合法问题——两引擎同输入同输出（核心验收）。"""
    import numpy as np

    # 避免 schema_linker 加载真实模型（同 test_nl2sql_e2e 纪律）
    from app.domains.data import schema_linker

    class _Fake:
        def embed_texts(self, texts):
            return np.zeros((len(texts), 8), dtype=np.float32)

        def embed_query(self, query):
            return np.zeros(8, dtype=np.float32)

    schema_linker.linker._embedder = _Fake()
    schema_linker.linker._vectors = None

    state = {"question": "华东区域有多少订单？", "today": DATA_BASE_DATE.isoformat()}
    lg = await data_graph.ainvoke(dict(state))
    rt = await run_data_runtime(dict(state))

    # 两引擎关键字段一致（sql/result/reply/repair_attempts）
    assert lg["sql"] == rt["sql"]
    assert lg.get("rejected_reason") == rt.get("rejected_reason")
    assert lg["reply"] == rt["reply"]
    assert lg.get("repair_attempts", 0) == rt.get("repair_attempts", 0)
    assert lg["result"]["columns"] == rt["result"]["columns"]
    assert lg["result"]["rows"] == rt["result"]["rows"]


@pytest.mark.asyncio
async def test_data_graph_isomorphic_reject_path():
    """同构对照：安全类拒绝路径（写 SQL 直送 → 两引擎都拒答）。"""
    state = {
        "question": "删掉所有订单",
        "today": "2026-08-18",
        "initial_sql": "DELETE FROM orders WHERE 1=1",
    }
    lg = await data_graph.ainvoke(dict(state))
    rt = await run_data_runtime(dict(state))
    assert lg["rejected_reason"] == rt.get("rejected_reason")
    assert "无法执行" in lg["reply"] and lg["reply"] == rt.get("reply", "")


@pytest.mark.asyncio
async def test_data_graph_isomorphic_unknown_question():
    """同构对照：评测集外问题（mock 默认安全 SQL → 两引擎一致）。"""
    state = {"question": "随便问一个奇怪的问题？", "today": DATA_BASE_DATE.isoformat()}
    lg = await data_graph.ainvoke(dict(state))
    rt = await run_data_runtime(dict(state))
    assert lg["sql"] == rt["sql"]
    assert lg["result"]["columns"] == rt["result"]["columns"]
    assert lg["reply"] == rt["reply"]


@pytest.mark.asyncio
async def test_data_graph_isomorphic_repair_path():
    """同构对照：可修复闸拒路径（parse-error 语法坏 SQL → 修复循环）。"""
    state = {
        "question": "华东区域有多少订单？",
        "today": DATA_BASE_DATE.isoformat(),
        "initial_sql": "SELECT * FORM orders",
    }  # FORM 拼错 → parse-error
    lg = await data_graph.ainvoke(dict(state))
    rt = await run_data_runtime(dict(state))
    # 修复循环在两引擎都触发（repair_attempts 相同 / repair_log 对齐）
    assert lg["repair_attempts"] == rt.get("repair_attempts", 0)
    assert lg["repair_log"] == rt.get("repair_log", [])


@pytest.mark.asyncio
async def test_data_graph_isomorphic_loop_dispose():
    """同构对照收尾：释放 executor 引擎（与 e2e 同纪律）。"""
    await dispose_engine()
