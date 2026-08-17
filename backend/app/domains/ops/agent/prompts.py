"""项目 B 提示词（W19 Day5）：意图识别 + 报表生成。

LLM 只做"理解"，参数校验交给 pydantic/ToolSpec（错就回问用户）。
"""

INTENT_SYSTEM_PROMPT = """你是供应链业务操作助手。根据用户的一句话，判断意图并提取参数。

可用意图：
- query_order      查订单（参数：order_id）
- update_order     改订单金额/交期（参数：order_id, 可选 amount / delivery_date）——高危操作
- cancel_order     取消订单（参数：order_id, reason）——高危操作
- generate_report  生成报表（参数：report_type=inventory|reconciliation, 可选 from/to 日期）
- unclear          无法判断

规则：
1. 订单号形如 PO-XXXX（如 PO-0001）
2. 涉及"改金额/改交期/取消"必须是 update_order/cancel_order
3. 涉及"库存/对账/报表/汇总"是 generate_report
4. 查状态/查订单/查详情是 query_order
5. 参数不全时 intent 仍返回该意图，params 给已提取的部分，缺失字段标 null
6. 完全无法判断返回 unclear
以 JSON 对象格式输出：{"intent": "...", "params": {...}, "reason": "..."}
"""

REPORT_SYSTEM_PROMPT = """你是供应链报表解读助手。把结构化报表数据转成给业务人员看的中文总结。

要求：
1. 先说结论（库存：低库存预警；对账：金额最大的供应商/总金额）
2. 低库存项要逐个列出（SKU/名称/当前量/安全库存）
3. 对账要列出 top3 供应商及金额
4. 语言简洁，用中文，不要编造数据（只依据给出的 rows/summary）
"""


def build_intent_messages(user_message: str) -> list[dict]:
    return [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"用户输入：{user_message}"},
    ]


def build_report_messages(report: dict) -> list[dict]:
    import json
    payload = json.dumps({
        "report_type": report.get("report_type"),
        "summary": report.get("summary"),
        "rows": report.get("rows", [])[:10],
    }, ensure_ascii=False)
    return [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {"role": "user", "content": f"报表数据：\n{payload}"},
    ]


def build_reply_for_tool_result(result_text: str) -> list[dict]:
    return [
        {"role": "system", "content": "你是供应链业务助手，把工具执行结果转成简洁的中文回答给用户。"},
        {"role": "user", "content": f"工具结果：{result_text}"},
    ]


# ---- RAG 兜底拼装（★ W21 Day6：real_provider._build_messages 的 fallback 分支
#      引用此函数——此前缺失会在兜底路径 NameError，属存量 bug，mypy 揪出后补齐）----

def build_rag_context(ctx: list[dict]) -> str:
    """检索上下文 → 文本（real_provider 无 system_prompt_override 时的兜底）。"""
    parts = []
    for h in ctx:
        doc = h.get("doc_id", "")
        section = h.get("section_path", "")
        text = (h.get("text") or "")[:800]
        parts.append(f"[文档 {doc} | 章节 {section}]\n{text}")
    return "\n\n".join(parts)


def build_system_prompt(ctx_text: str) -> str:
    """系统提示：只依据上下文 + 引用 doc_id + 不知道就明说（与项目 A 一致）。"""
    return (
        "你是供应链制度问答助手。只依据提供的文档回答，不要编造；"
        "回答必须引用相关文档（doc_id）；检索不到就明确说不知道。\n\n"
        f"可用文档：\n{ctx_text}"
    )
