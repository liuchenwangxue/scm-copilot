"""★ 业务 Agent 编排图（W19 Day5）：intent → approval_gate → execute → respond

流程：
    intent（LLM 识别，超预算时规则兜底）
      → approval_gate（高危 → interrupt 挂起等人工确认；低危直通）
      → execute（可靠层包装的工具调用 + 审计）
      → respond（结果转回答；报表走 LLM 生成，超预算降级模板）

Key 设计：
1. ★ HITL：高危工具在 approval_gate 用 interrupt 挂起，resume 值 = {decision: approve|reject}
   （checkpointer=SqliteSaver 保证断点恢复，Day4 已建 approval 表双保险）
2. ★ 幂等键：审批发起时生成（Day4 语义），execute 带同一个 key 调用 mock（幂等头双保险）
3. ★ 超预算降级（A3）：intent 改规则匹配、报表改模板生成——不拒绝
4. 审计：approval_requested/approved/rejected + execution_succeeded/failed（Day4 全链）
5. 条件路由：unclear → 直接 respond 回问，不进工具链
"""
import asyncio
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.domains.ops import config
from app.domains.ops.agent.intent import IntentRouter
from app.domains.ops.agent.tools.order_tools import OrderTools
from app.domains.ops.agent.tools.registry import registry
from app.domains.ops.agent.tools.report_tools import ReportTools
from app.domains.ops.persistence import get_async_checkpointer
from app.domains.ops.security.approval import ApprovalService
from app.domains.ops.security.audit import AuditLogger
from app.platform.hooks import ToolUseContext, run_post_hooks, run_pre_hooks
from app.shared.reliability.cost_budget import get_session_budget
from app.shared.reliability.idempotency import IdempotencyStore, IdemUnavailableError
from app.shared.reliability.redis_client import get_redis_client

# ---- 服务单例（进程内；Docker 化时可用依赖注入替换） ----
audit = AuditLogger(config.AUDIT_LOG)
approval_svc = ApprovalService(dsn=config.APPROVAL_DSN, audit=audit)  # ★ Day5：MySQL 平台库
# ★ W27 D3 A5：熔断状态 Redis 共享（双实例各熔各的 → 秒级收敛；Redis 挂 fail-open 不误熔断）
order_tools = OrderTools(config.BIZ_BASE_URL, redis_client=get_redis_client())
report_tools = ReportTools(config.BIZ_BASE_URL, redis_client=get_redis_client())
idem_store = IdempotencyStore(config.IDEMPOTENCY_DB)

# ★ W27 D3 A7：写类请求集合——Redis 挂时幂等保护 fail-closed 拒绝（读类降级不受影响）
_WRITE_TOOLS = frozenset({"update_order", "cancel_order"})


def _make_intent_router():
    from app.shared.llm import get_provider
    return IntentRouter(get_provider())


intent_router = _make_intent_router()


class BizState(TypedDict, total=False):
    message: str
    session_id: str
    intent: dict                 # {intent, params, source}
    tool_name: str
    tool_params: dict
    approval: dict               # {status, approval_id, idem_key}
    tool_result: dict            # 执行结果（dict 化 ToolResult）
    reply: str
    degraded: bool


# ================= 节点 =================

async def intent_node(state: BizState) -> dict:
    """意图识别：LLM 优先；超预算 → 规则兜底（A3 降级不拒绝）。"""
    message = state["message"]
    session_id = state["session_id"]
    budget = get_session_budget(session_id)
    use_llm = not budget.is_over_budget()
    if not use_llm:
        print("  [INTENT] 会话已超预算 → 规则兜底识别（A3 降级）")
    result = await intent_router.route(
        message, use_llm=use_llm,
        token_sink=lambda p, c: budget.add_usage(p, c))
    intent = result["intent"]
    if intent == "unclear":
        return {"intent": result, "reply": "没太明白您的意思。可以试试：查一下 PO-0001 的状态 / 把 PO-0002 的金额改成 9000 / 生成库存报表。"}
    return {"intent": result, "tool_name": intent, "tool_params": result["params"]}


def route_after_intent(state: BizState) -> str:
    """unclear 直接 respond（回问），否则进审批门。"""
    if state.get("intent", {}).get("intent") == "unclear":
        return "respond"
    return "approval_gate"


def approval_gate(state: BizState) -> dict:
    """审批门：高危工具 100% 走 interrupt 人工确认；低危直通。

    interrupt 挂起后：
    - 调用方（main.py/测试）收到 __interrupt__，展示审批表单，等用户 approve/reject
    - Command(resume={"decision": "approve"|"reject", "reason": ...}) 恢复
    """
    spec = registry.get(state["tool_name"])
    if spec is None or not spec.requires_approval:
        return {"approval": {"status": "not_required"}}

    tool_name = state["tool_name"]
    params = state["tool_params"]
    order_id = params.get("order_id", "")

    # 构造 before（查当前订单）与 after（目标状态）→ diff（审批表单核心）
    current = order_tools.query_order(order_id)
    if not current.success or not current.data:
        return {"tool_result": {"success": False, "error": f"订单 {order_id} 不存在或查询失败：{current.error}",
                                "circuit_state": current.circuit_state},
                "approval": {"status": "not_required"}}
    before = current.data
    # ★ W25 Day6：after 目标状态由 hooks 能力统一计算（approval_gate 复用钩子的
    #   before/after diff——s04"扩展点不侵入循环"的实物落点，单一来源防漂移）
    from app.platform.hooks import make_after_state

    after = make_after_state(tool_name, params, before)

    if tool_name == "update_order":
        parts = []
        if params.get("amount") is not None:
            parts.append(f"金额 → {params['amount']}")
        if params.get("delivery_date"):
            parts.append(f"交期 → {params['delivery_date']}")
        reason = params.get("reason") or ("；".join(parts) or "无")
    else:
        reason = params.get("reason") or "无"

    req = approval_svc.create(
        tool_name=tool_name, operation=("修改订单" if tool_name == "update_order" else "取消订单"),
        order_id=order_id, before=before, after=after,
        reason=reason, session_id=state["session_id"])
    print(f"  [APPROVAL] 高危操作 {tool_name} 待审批: {req.approval_id}")

    # ★ HITL：挂起，等人工确认
    decision = interrupt({
        "approval_request": True,
        "approval_id": req.approval_id,
        "form": req.to_form(),
    })

    if decision.get("decision") == "approve":
        approval_svc.approve(req.approval_id)
        return {"approval": {"status": "approved", "approval_id": req.approval_id,
                             "idem_key": req.idem_key}}
    approval_svc.reject(req.approval_id, decision.get("reason", "用户拒绝"))
    return {"approval": {"status": "rejected", "approval_id": req.approval_id}}


def execute_node(state: BizState) -> dict:
    """执行工具（可靠层包装）。rejected 不执行。写操作带幂等键。"""
    tool_name = state["tool_name"]
    params = state["tool_params"]
    approval = state.get("approval", {})

    if approval.get("status") == "rejected":
        r = ToolResultDummy(success=False, error="操作已被审批人拒绝")
        audit.log("execution_failed", approval_id=approval.get("approval_id"),
                  target=params.get("order_id", ""), error="rejected")
        return {"tool_result": r.to_dict(), "degraded": False}

    # ★ W25 Day6：PreToolUse 钩子——参数校验 + 高危标记 + 审计埋点（before 状态）。
    #   返回非 None = 阻断消息 → 本次工具调用不执行（s04 "钩子说停就停"语义）
    hook_ctx = ToolUseContext(
        tool_name=tool_name,
        params=params,
        session_id=state.get("session_id", ""),
        trace_id=state.get("session_id", ""),
    )
    blocked = run_pre_hooks(hook_ctx)
    if blocked:
        r = ToolResultDummy(success=False, error=blocked)
        audit.log("execution_failed", approval_id=approval.get("approval_id"),
                  target=params.get("order_id", ""), tool=tool_name, error=f"hook:{blocked}")
        return {"tool_result": r.to_dict(), "degraded": False}

    # ---- ★ W27 D3 A7：幂等写路径 fail-closed 前置检查 ----
    # Redis 挂 + 写类请求（高危工具执行）→ 直接拒绝（错误码 IDEM_UNAVAILABLE），
    # 避免跨实例重复副作用；读类请求（查询/生成）走 sqlite 降级不受影响。
    if tool_name in _WRITE_TOOLS:
        try:
            idem_store.resolve_backend(risk="write")
        except IdemUnavailableError as e:
            r = ToolResultDummy(success=False, error=str(e))
            audit.log("execution_failed", approval_id=approval.get("approval_id"),
                      target=params.get("order_id", ""), tool=tool_name,
                      error="idem_unavailable")
            return {"tool_result": r.to_dict(), "degraded": False}

    # ---- 可靠层工具调用（Day3 熔断+降级链已内置） ----
    # ★ W27-D6 (B8)：if/elif 硬编码改 registry.dispatch 统一分发——开闭原则实例，
    #   新增工具只需在 tools 层注册 handler，本图代码零改动；未注册名 → 明确错误。
    if tool_name in _WRITE_TOOLS:
        # 写操作幂等键：审批发起时的 idem_key 优先，否则自动生成（防重）
        params = dict(params)
        params.setdefault("idempotency_key",
                          approval.get("idem_key") or order_tools.new_idempotency_key())
    _t0 = time.time()
    try:
        result = registry.dispatch(tool_name, params)
    except KeyError:
        return {"tool_result": {"success": False, "error": f"未知工具: {tool_name}"},
                "degraded": False}

    # ★ W25 Day6：PostToolUse 钩子——结果审计（after 状态 + 耗时）+ 语义缓存失效
    hook_ctx.result = result
    hook_ctx.duration_ms = (time.time() - _t0) * 1000.0
    run_post_hooks(hook_ctx)

    # ---- 审计执行事件 ----
    if result.success:
        audit.log("execution_succeeded", approval_id=approval.get("approval_id"),
                  target=params.get("order_id", ""), tool=tool_name)
    else:
        audit.log("execution_failed", approval_id=approval.get("approval_id"),
                  target=params.get("order_id", ""), tool=tool_name,
                  error=result.error)

    return {"tool_result": result.to_dict() if hasattr(result, "to_dict") else _result_to_dict(result),
            "degraded": result.degraded}


async def respond_node(state: BizState) -> dict:
    """结果转回答。报表走 LLM 生成（超预算 → 模板降级）。"""
    session_id = state["session_id"]
    budget = get_session_budget(session_id)
    tool_result = state.get("tool_result")
    tool_name: str = state.get("tool_name") or ""

    # unclear 回问路径
    if tool_result is None and state.get("reply"):
        return {"reply": state["reply"], "degraded": False}

    if not tool_result or not tool_result.get("success"):
        err = (tool_result or {}).get("error", "未知错误")
        return {"reply": f"操作未完成：{err}", "degraded": bool(state.get("degraded"))}

    data = tool_result.get("data") or {}

    if tool_name == "generate_report" and not budget.is_over_budget():
        try:
            from app.domains.ops.agent.prompts import build_report_messages
            from app.shared.llm import get_provider
            provider = get_provider()
            if provider.name == "mock":
                # mock 只返回检索式回答，无法解读报表 → 直接用模板（避免无意义输出）
                reply = _template_report(data)
            else:
                reply = await provider.generate(build_report_messages(data), max_tokens=512)
                # ★ W20 Day2 修复：real 失败会【内部降级】mock 而不抛异常（返回带
                #   [WARNING] 前缀的检索式回答），except 分支抓不到 → 报表输出无意义。
                #   检测降级标记 → 改用模板兜底（A3 设计：降级不是拒绝）
                if isinstance(reply, str) and reply.startswith("[WARNING]"):
                    print("  [RESPOND] real 报表生成失败（降级标记）→ 模板兜底")
                    reply = _template_report(data)
            budget.add_usage(500, 150)   # 报表生成的估算 usage
        except Exception as e:
            print(f"  [RESPOND] 报表 LLM 生成失败，模板降级: {e}")
            reply = _template_report(data)
    elif tool_name == "generate_report":
        print("  [RESPOND] 会话已超预算 → 报表模板生成（A3 降级）")
        reply = _template_report(data)
    else:
        reply = _textify_result(tool_name, data)

    return {"reply": reply, "degraded": budget.is_over_budget()}


# ================= 工具结果转换 =================

class ToolResultDummy:
    """拒绝路径的最小结果对象（避免依赖 registry 的 ToolResult）。"""
    def __init__(self, success=False, error="", **kw):
        self.success = success
        self.error = error
        self.degraded = False
        self.circuit_state = "N/A"

    def to_dict(self):
        return {"success": self.success, "error": self.error,
                "data": None, "degraded": self.degraded,
                "circuit_state": self.circuit_state}


def _result_to_dict(r) -> dict:
    return {
        "success": r.success, "data": r.data, "error": r.error,
        "degraded": r.degraded, "level": r.level,
        "attempts": r.attempts, "circuit_state": r.circuit_state,
    }


def _textify_result(tool_name: str, data: dict) -> str:
    if tool_name == "query_order":
        d = data
        return (f"订单 {d.get('order_id')}：状态 **{d.get('status_label')}**，"
                f"金额 ¥{d.get('amount')}，交期 {d.get('delivery_date')}，"
                f"供应商 {d.get('supplier_name')}。")
    if tool_name == "update_order":
        d = data
        return (f"订单 {d.get('order_id')} 已更新：金额 ¥{d.get('amount')}，"
                f"交期 {d.get('delivery_date')}，状态 {d.get('status_label')}。")
    if tool_name == "cancel_order":
        d = data
        return f"订单 {d.get('order_id')} 已取消（状态：{d.get('status_label')}）。"
    return str(data)


def _template_report(data: dict) -> str:
    """报表模板降级（A3）：不调 LLM，直接结构化输出。"""
    rt = data.get("report_type")
    summary = data.get("summary") or {}
    if rt == "inventory":
        low = [r for r in data.get("rows", []) if r.get("low_stock")]
        lines = [f"库存报表（共 {summary.get('total_items')} 项，低库存 {summary.get('low_stock')} 项）："]
        for r in low:
            lines.append(f"  - {r.get('sku')} {r.get('name')}：现量 {r.get('qty')} < 安全库存 {r.get('safety_stock')}")
        return "\n".join(lines)
    rows = data.get("rows", [])
    lines = [f"对账报表：共 {summary.get('order_count')} 单，总金额 ¥{summary.get('total_amount')}。"]
    for r in rows[:3]:
        lines.append(f"  - {r.get('supplier_name')}：{r.get('order_count')} 单 ¥{r.get('total_amount')}")
    return "\n".join(lines)


# ================= 图编译 =================

builder = StateGraph(BizState)
builder.add_node("intent", intent_node)
builder.add_node("approval_gate", approval_gate)
builder.add_node("execute", execute_node)
builder.add_node("respond", respond_node)
builder.add_edge(START, "intent")
builder.add_conditional_edges("intent", route_after_intent,
                              {"approval_gate": "approval_gate", "respond": "respond"})
builder.add_edge("approval_gate", "execute")
builder.add_edge("execute", "respond")
builder.add_edge("respond", END)

# ---- 惰性编译：async 图必须用 AsyncSqliteSaver，其 aiosqlite 连接绑定创建它的
#      事件循环（跨 loop 复用会 "threads can only be started once"）。
#      因此不在模块级编译，而是在调用方的运行 loop 内首次编译并缓存。 ----
_biz_graph = None
_biz_graph_lock = asyncio.Lock()


async def get_biz_graph():
    """获取已编译的业务图（进程内单例，首次在调用方 loop 内编译）。"""
    global _biz_graph
    if _biz_graph is None:
        async with _biz_graph_lock:
            if _biz_graph is None:
                checkpointer = await get_async_checkpointer()
                _biz_graph = builder.compile(checkpointer=checkpointer)
    return _biz_graph
