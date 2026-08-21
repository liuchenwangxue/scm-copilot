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


# ================= data 图同构对照（确定性 execute，无需真实 DB） =================
#
# ★ W28-D6 CI 修复：同构对照不应依赖真实 DB 的冷启动时序——LangGraph 先跑时
#   execute 首连可能 3s 超时 → 触发 repair#2；自研后跑时连接已热 → 1 次成功，
#   导致 repair_attempts 不一致（CI flaky，本机不复现）。
#   修复：把 execute_sql patch 成**可编程确定性 fake**（成功/首次失败脚本），
#   两引擎在完全相同的 execute 行为下对比——真正验证"图引擎调度/状态合并同构"，
#   SQL 执行本身由 test_executor/test_nl2sql_e2e 覆盖。

from app.domains.data.executor import dispose_engine  # noqa: E402
from app.domains.data.graph import data_graph  # noqa: E402
from app.domains.data.prompts import DATA_BASE_DATE  # noqa: E402
from app.domains.data.runtime_graph import run_data_runtime  # noqa: E402


class _ScriptedExecute:
    """确定性 execute fake：按脚本返回成功或首次失败（模拟冷启动）。

    关键：每次 `reset()` 后两引擎各自经历**相同**的调用序列——
    若先跑 LangGraph 吃了"首次失败"，后跑自研 reset 后同样吃一次，
    repair 路径在两引擎上确定性对齐（消除真实 DB 时序差异）。
    """

    def __init__(self):
        self.calls: list[str] = []
        self.fail_first = False  # 首次调用抛 ExecutionError（模拟冷连接超时）

    def reset(self, fail_first: bool = False) -> None:
        self.calls.clear()
        self.fail_first = fail_first

    async def __call__(self, sql: str, audit=None, **kw) -> dict:
        self.calls.append(sql)
        if self.fail_first and len(self.calls) == 1:
            from app.domains.data.executor import ExecutionError

            raise ExecutionError("query timeout after 3.0s")
        return {
            "sql": sql,
            "columns": ["cnt"],
            "rows": [[200]],
            "truncated": False,
            "elapsed_ms": 1.0,
            "error": None,
        }


async def _run_both(state: dict, fake: _ScriptedExecute) -> tuple[dict, dict]:
    """同一 state 在两引擎各跑一遍，**各自 reset** 保证确定性对齐。"""
    from typing import Any

    st: dict[str, Any] = dict(state)  # DataState 兼容输入（TypedDict total=False）
    fake.reset()
    lg = await data_graph.ainvoke(st)  # type: ignore[call-overload]  # 测试态宽松输入
    fake.reset()
    rt = await run_data_runtime(dict(st))
    return lg, rt


@pytest.mark.asyncio
async def test_data_graph_isomorphic_legal_question(monkeypatch):
    """同构对照：合法问题——两引擎同输入同输出（核心验收）。"""
    fake = _ScriptedExecute()
    # execute_node 内调用的是 graph 模块级绑定的 execute_sql（from ... import）——
    # patch graph.execute_sql 即对两引擎都生效（runtime_graph 复用同一节点函数）
    monkeypatch.setattr("app.domains.data.graph.execute_sql", fake)

    state = {"question": "华东区域有多少订单？", "today": DATA_BASE_DATE.isoformat()}
    lg, rt = await _run_both(state, fake)

    # 两引擎关键字段一致（sql/result/reply/repair_attempts）
    assert lg["sql"] == rt["sql"]
    assert lg.get("rejected_reason") == rt.get("rejected_reason")
    assert lg["reply"] == rt["reply"]
    assert lg.get("repair_attempts", 0) == rt.get("repair_attempts", 0)
    assert lg["result"]["columns"] == rt["result"]["columns"]
    assert lg["result"]["rows"] == rt["result"]["rows"]


@pytest.mark.asyncio
async def test_data_graph_isomorphic_reject_path(monkeypatch):
    """同构对照：安全类拒绝路径（写 SQL 直送 → 两引擎都拒答）。"""
    fake = _ScriptedExecute()
    monkeypatch.setattr("app.domains.data.graph.execute_sql", fake)

    state = {
        "question": "删掉所有订单",
        "today": "2026-08-18",
        "initial_sql": "DELETE FROM orders WHERE 1=1",
    }
    lg, rt = await _run_both(state, fake)
    assert lg["rejected_reason"] == rt.get("rejected_reason")
    assert "无法执行" in lg["reply"] and lg["reply"] == rt.get("reply", "")


@pytest.mark.asyncio
async def test_data_graph_isomorphic_unknown_question(monkeypatch):
    """同构对照：评测集外问题（mock 默认安全 SQL → 两引擎一致）。"""
    fake = _ScriptedExecute()
    monkeypatch.setattr("app.domains.data.graph.execute_sql", fake)

    state = {"question": "随便问一个奇怪的问题？", "today": DATA_BASE_DATE.isoformat()}
    lg, rt = await _run_both(state, fake)
    assert lg["sql"] == rt["sql"]
    assert lg["result"]["columns"] == rt["result"]["columns"]
    assert lg["reply"] == rt["reply"]


@pytest.mark.asyncio
async def test_data_graph_isomorphic_repair_path(monkeypatch):
    """同构对照：可修复闸拒路径（parse-error 语法坏 SQL → 修复循环）。"""
    fake = _ScriptedExecute()
    monkeypatch.setattr("app.domains.data.graph.execute_sql", fake)

    state = {
        "question": "华东区域有多少订单？",
        "today": DATA_BASE_DATE.isoformat(),
        "initial_sql": "SELECT * FORM orders",
    }  # FORM 拼错 → parse-error
    lg, rt = await _run_both(state, fake)
    # 修复循环在两引擎都触发（repair_attempts 相同 / repair_log 对齐）
    assert lg["repair_attempts"] == rt.get("repair_attempts", 0)
    assert lg["repair_log"] == rt.get("repair_log", [])


@pytest.mark.asyncio
async def test_data_graph_isomorphic_execute_fail_path(monkeypatch):
    """同构对照：execute 首次失败（冷连接超时）→ repair → 再次 execute 成功。

    ★ CI flaky 根因场景：LangGraph 先跑吃冷启动失败，自研后跑连接已热——
    本测试用 fail_first fake 让**两引擎各自**经历首次失败，确定性对齐。
    """
    fake = _ScriptedExecute()
    monkeypatch.setattr("app.domains.data.graph.execute_sql", fake)

    state = {"question": "华东区域有多少订单？", "today": DATA_BASE_DATE.isoformat()}
    fake.reset(fail_first=True)
    lg = await data_graph.ainvoke(dict(state))
    fake.reset(fail_first=True)
    rt = await run_data_runtime(dict(state))

    # 两引擎都走了"execute 失败 → repair → 成功"，次数与日志对齐
    assert lg["repair_attempts"] == rt.get("repair_attempts", 0) == 1
    assert lg["repair_log"] == rt.get("repair_log", [])
    assert lg["error"] is None and rt.get("error") is None  # 最终成功
    assert lg["reply"] == rt["reply"]


@pytest.mark.asyncio
async def test_data_graph_isomorphic_loop_dispose():
    """同构对照收尾：释放 executor 引擎（与 e2e 同纪律）。"""
    await dispose_engine()
