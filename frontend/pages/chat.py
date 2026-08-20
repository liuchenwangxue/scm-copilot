"""对话页：SSE 流式 + data_table 表格 + SQL 折叠 + 引用溯源（★ W28 Day2，C2）。

事件协议（对齐后端）：
- kb：progress / message / citations / data_table / done / error
- ops：progress / approval_request / message / done / error

设计要点（对齐 W28 手册 Day2）：
- generator 回调逐事件 `yield`，message 事件按 delta 增量追加（打字机效果，不整段替换）
- data_table 事件 → `gr.Dataframe` 表格；SQL 放 `gr.Accordion("查看 SQL", open=False)` 折叠
- citations 事件 → 引用溯源 `gr.Markdown` 列表（doc_id 溯源）
- approval_request 事件（ops HITL）→ 状态区提示"请前往审批页处理"
- API Key 从登录区传入（sk-），每次回调新建 ScmCopilot（SDK 0.2.0 自带 429 退避）
- 证书：本地 mkcert 自签 HTTPS 平台，SDK verify=False（生产走 CA/verify=True）
"""

from __future__ import annotations

import os

import gradio as gr
from scm_client import ScmCopilot

# 平台默认地址（nginx https 入口；本地直连可改为 http://localhost:8000）
DEFAULT_BASE_URL = "https://localhost:18443"
# 容器内 SDK 默认走 nginx 入口（环境变量可覆盖，演示场景固定即可）
SDK_BASE_URL = os.environ.get("SCM_BASE_URL", "https://nginx:443")


def _make_client(base_url: str, api_key: str) -> ScmCopilot:
    """构建 SDK 客户端——始终走容器内可达地址（环境变量 SCM_BASE_URL）。

    浏览器登录的 base_url 输入框（`https://localhost:18443`）仅用于用户视角
    的"连接测试"探针，**不**作为 SDK 调后端的入参——容器内 SDK 需走
    容器间地址（`https://nginx:443`），避免 localhost 在容器内指自身。
    本地自签证书 → verify=False；生产应传 CA 或 verify=True。
    """
    return ScmCopilot(base_url=base_url or SDK_BASE_URL, api_key=api_key or None, verify=False)


def _fmt_metrics(ev_data: dict) -> str:
    """data_table 事件的指标区文本（insights/elapsed/truncated）。"""
    parts = []
    elapsed = ev_data.get("elapsed")
    if elapsed is not None:
        parts.append(f"耗时 {float(elapsed) * 1000:.0f} ms")
    insights = ev_data.get("insights") or []
    if insights:
        parts.append("洞察：" + "；".join(str(i) for i in insights[:3]))
    if ev_data.get("truncated"):
        parts.append("（结果已截断）")
    return " ｜ ".join(parts)


def build(base_url: gr.Textbox, api_key: gr.Textbox) -> dict:
    """构建对话页（在 gr.Blocks 上下文内调用）。返回可测组件 dict。"""
    with gr.Row():
        domain = gr.Radio(
            choices=["kb", "ops"],
            value="kb",
            label="对话域",
            info="kb=知识问答（制度/引用）；ops=业务操作（意图识别/审批/HITL）",
        )
        status = gr.Markdown("", label="链路状态")
    chatbot = gr.Chatbot(label="对话", height=420)

    with gr.Row():
        msg = gr.Textbox(
            label="输入问题",
            placeholder="例如：供应商准入需要哪些资质？ 或 ops 域：查询订单 SO-2026-00001",
            scale=8,
        )
        send = gr.Button("发送", variant="primary", scale=1)

    # data_table → 表格；SQL 折叠可回溯；引用溯源列表
    with gr.Accordion("查看 SQL", open=False):
        sql_code = gr.Code(label="SQL", language="sql", interactive=False)
    with gr.Accordion("引用溯源", open=False):
        citations_md = gr.Markdown("（暂无引用）")
    table = gr.Dataframe(label="查询结果", interactive=False, wrap=True)

    def _respond(message: str, history: list, domain_sel: str, b_url: str, key: str):
        """SSE 流式回调：逐事件 yield（generator → gradio 打字机效果）。

        输出顺序 = [chatbot, msg, status, table, sql_code, citations_md]。
        """
        if not message or not message.strip():
            yield history or [], gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
            return
        history = list(history or [])
        history.append({"role": "user", "content": message})
        yield history, "", "⏳ 正在处理…", gr.update(), gr.update(), gr.update()

        client = _make_client(b_url, key)
        answer = ""
        citations: list = []
        try:
            for ev in client.chat_stream(message, domain=domain_sel):
                if ev.type == "progress":
                    node = ev.data.get("node", "")
                    result = (ev.data.get("data") or {}).get("result", "")
                    yield history, "", f"⚙️ {node}：{result}", gr.update(), gr.update(), gr.update()
                elif ev.type == "message":
                    answer += ev.delta
                    # 已追加过 assistant 消息则更新，否则新增
                    if history and history[-1].get("role") == "assistant":
                        history[-1]["content"] = answer
                    else:
                        history.append({"role": "assistant", "content": answer})
                    yield history, "", "⏳ 生成中…", gr.update(), gr.update(), gr.update()
                elif ev.type == "data_table":
                    df = _to_dataframe(ev.data)
                    sql = ev.data.get("sql") or ""
                    meta = _fmt_metrics(ev.data)
                    yield history, "", f"📊 查询完成（{meta or '—'}）", df, sql, gr.update()
                elif ev.type == "citations":
                    citations = ev.data.get("citations") or []
                    src = ev.data.get("source") or "rag"
                    val = ev.data.get("validation") or {}
                    md = _render_citations(citations, ev.data.get("retrieved_docs") or [], src, val)
                    yield history, "", gr.update(), gr.update(), gr.update(), md
                elif ev.type == "approval_request":
                    appr_id = ev.data.get("approval_id") or ""
                    form = ev.data.get("form") or {}
                    ops = "；".join(f"{k}={v}" for k, v in (form or {}).items())
                    note = f"🔔 高危操作需要审批（approval_id={appr_id}）：{ops} ——请前往【审批】页处理"
                    yield history, "", note, gr.update(), gr.update(), gr.update()
                elif ev.type == "error":
                    yield (
                        history,
                        "",
                        f"❌ 错误：{ev.data.get('error', '')}",
                        gr.update(),
                        gr.update(),
                        gr.update(),
                    )
            # done：若消息已完整则状态标记完成
            yield history, "", "✅ 完成", gr.update(), gr.update(), gr.update()
        except Exception as e:  # noqa: BLE001  # 网络/认证错误统一提示
            yield (
                history,
                "",
                f"❌ 调用失败：{type(e).__name__}: {str(e)[:200]}",
                gr.update(),
                gr.update(),
                gr.update(),
            )

    inputs = [msg, chatbot, domain, base_url, api_key]
    outputs = [chatbot, msg, status, table, sql_code, citations_md]
    send.click(_respond, inputs, outputs)
    msg.submit(_respond, inputs, outputs)

    return {
        "chatbot": chatbot,
        "msg": msg,
        "send": send,
        "domain": domain,
        "status": status,
        "table": table,
        "sql_code": sql_code,
        "citations_md": citations_md,
    }


def _to_dataframe(ev_data: dict):
    """data_table 事件 → gradio DataFrame 可接受的值（list[list] + headers）。"""
    columns = list(ev_data.get("columns") or [])
    rows = list(ev_data.get("rows") or [])
    if not columns:
        return gr.update()
    return {"headers": columns, "data": rows}


def _render_citations(citations: list, retrieved: list, source: str, validation: dict) -> str:
    """渲染引用溯源（doc_id 列表 + 校验状态）。citations 是 doc_id 字符串列表。"""
    lines = []
    passed = validation.get("passed", True)
    warn = validation.get("warning") or ""
    if not citations:
        return "（本次回答无引用）"
    lines.append(f"**引用来源**（`{source}`）：")
    for i, cid in enumerate(citations, 1):
        lines.append(f"{i}. `{cid}`")
    if warn:
        lines.append("")
        lines.append(f"⚠️ {warn}")
    elif passed:
        lines.append("")
        lines.append("✅ 引用校验通过")
    else:
        lines.append("")
        lines.append("⚠️ 引用可信度低（校验未完全通过）")
    return "\n".join(lines)
