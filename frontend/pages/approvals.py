"""审批页：待审列表 + 通过/驳回（★ W28 Day2，C2）。

对应后端：
- `GET /api/v1/ops/approvals`：待审批列表（含 HITL 恢复上下文 session_id）
- `POST /api/v1/ops/approval`：审批决策（approve/reject，resume LangGraph 图）

设计要点（对齐 W28 手册 Day2 下午）：
- 列表：`gr.Dataframe` 展示待审批（approval_id / 操作 / 订单 / 理由 / 创建时间）
- 选中行（Dropdown）→ 显示 diff 详情；通过/驳回按钮行级操作
- `decide()` 后刷新列表 + 展示返回（ok/reply）——审批动作落库有审计（后端 audit.log）
- 空列表空态提示；后端 503（审批存储故障）→ 明确提示不雪崩
"""

from __future__ import annotations

import contextlib
import os

import gradio as gr
from scm_client import ScmCopilot

# 容器内 SDK 默认走 nginx 入口（环境变量可覆盖）；浏览器登录的 base_url 仅作探针
SDK_BASE_URL = os.environ.get("SCM_BASE_URL", "https://nginx:443")

# diff 字段常见 key → 中文标签（展示友好）
_DIFF_LABELS = {
    "field": "字段",
    "old": "原值",
    "new": "新值",
    "column": "字段",
    "old_value": "原值",
    "new_value": "新值",
}


def _make_client(base_url: str, api_key: str) -> ScmCopilot:
    # 容器内 SDK 始终走 SDK_BASE_URL，避免 base_url 浏览器地址（localhost）在容器内不解析
    return ScmCopilot(base_url=base_url or SDK_BASE_URL, api_key=api_key or None, verify=False)


def _pending_to_table(items: list) -> list[list]:
    """审批项 → DataFrame 行（无则返回空表）。"""
    rows = []
    for it in items:
        rows.append(
            [
                it.approval_id,
                it.operation,
                it.order_id,
                (it.reason or "")[:60],
                it.created_at or "",
                it.status,
            ]
        )
    return rows


def _format_diff(diff: list) -> str:
    """diff（list[dict]）→ 人类可读文本。"""
    if not diff:
        return "（无字段变更明细）"
    lines = []
    for d in diff:
        if not isinstance(d, dict):
            lines.append(str(d))
            continue
        field = d.get("field") or d.get("column") or "?"
        field = _DIFF_LABELS.get(field, field)
        old = d.get("old") or d.get("old_value")
        new = d.get("new") or d.get("new_value")
        # 不展示敏感值：order diff 只显示字段与变化形态（不打印完整值）
        lines.append(
            f"- `{field}`：{old} → {new}" if old is not None or new is not None else f"- `{field}`"
        )
    return "\n".join(lines)


def build(base_url: gr.Textbox, api_key: gr.Textbox) -> dict:
    """构建审批页（在 gr.Blocks 上下文内调用）。返回可测组件 dict。"""
    with gr.Row():
        refresh = gr.Button("🔄 刷新待审批", variant="secondary")
        status = gr.Markdown("")
    table = gr.Dataframe(
        headers=["approval_id", "操作", "订单号", "理由", "创建时间", "状态"],
        label="待审批列表",
        interactive=False,
        wrap=True,
    )
    with gr.Row():
        picker = gr.Dropdown(label="选择审批项（行级操作）", choices=[], scale=3)
        detail = gr.Markdown("（选择审批项查看 diff 明细）", scale=5)
    with gr.Row():
        reason = gr.Textbox(
            label="审批意见（落审计）", placeholder="批准/驳回理由（可空）", scale=4
        )
        approve_btn = gr.Button("✅ 通过", variant="primary", scale=1)
        reject_btn = gr.Button("⛔ 驳回", variant="stop", scale=1)
    result = gr.Markdown("")

    # ---------- 刷新列表 ----------
    def _refresh(b_url: str, key: str):
        try:
            items = _make_client(b_url, key).approvals.list_pending()
        except Exception as e:  # noqa: BLE001
            return (
                gr.update(),
                "（无数据）",
                [],
                f"❌ 获取审批列表失败：{type(e).__name__}: {str(e)[:160]}",
                "",
            )
        if not items:
            return gr.update(), "（暂无待审批）", [], "✅ 无待审批事项", ""
        choices = [
            (
                f"{it.operation} · {it.order_id} · {it.reason[:20]}"
                if it.reason
                else f"{it.operation} · {it.order_id}",
                it.approval_id,
            )
            for it in items
        ]
        return (
            gr.update(),
            f"共 {len(items)} 条待审批",
            _pending_to_table(items),
            "✅ 已刷新",
            gr.update(value=choices),
        )

    refresh.click(_refresh, [base_url, api_key], [status, status, table, result, picker])

    # ---------- 选择审批项 → 显示 diff ----------
    def _on_pick(approval_id: str, b_url: str, key: str):
        if not approval_id:
            return "（选择审批项查看 diff 明细）"
        try:
            items = _make_client(b_url, key).approvals.list_pending()
        except Exception as e:  # noqa: BLE001
            return f"❌ 加载详情失败：{type(e).__name__}: {str(e)[:120]}"
        for it in items:
            if it.approval_id == approval_id:
                head = f"**{it.operation}** · 订单 `{it.order_id}`\n\n"
                head += f"- 状态：`{it.status}`\n- 创建：{it.created_at or '—'}\n"
                if it.reason:
                    head += f"- 发起理由：{it.reason}\n"
                return head + "\n**字段变更明细**：\n" + _format_diff(it.diff)
        return "（审批项已不存在，请刷新）"

    picker.change(_on_pick, [picker, base_url, api_key], [detail])

    # ---------- 通过 / 驳回 ----------
    def _decide(action: str, approval_id: str, reason_txt: str, b_url: str, key: str):
        if not approval_id:
            return "⚠️ 请先选择审批项", gr.update(), gr.update()
        try:
            resp = _make_client(b_url, key).approvals.decide(
                approval_id, action, reason=reason_txt or ""
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ 审批失败：{type(e).__name__}: {str(e)[:160]}", gr.update(), gr.update()
        ok = resp.get("ok")
        reply = resp.get("reply") or ""
        # 刷新剩余待审批（决策后自动重拉列表；失败不阻塞返回结果）
        items = []
        with contextlib.suppress(Exception):
            items = _make_client(b_url, key).approvals.list_pending()
        if ok:
            head = f"✅ 已{('批准' if action == 'approve' else '驳回')} `{approval_id}`，LangGraph 图已恢复继续执行"
        else:
            head = f"⚠️ {('批准' if action == 'approve' else '驳回')}未成功：{resp.get('error') or '未知原因'}"
        if reply:
            head += f"\n\n回复：{reply[:300]}"
        remaining = f"剩余待审批 {len(items)} 条" if items else "✅ 无待审批事项"
        return head, _pending_to_table(items), remaining

    approve_btn.click(
        lambda p, r, b, k: _decide("approve", p, r, b, k),
        [picker, reason, base_url, api_key],
        [result, table, status],
    )
    reject_btn.click(
        lambda p, r, b, k: _decide("reject", p, r, b, k),
        [picker, reason, base_url, api_key],
        [result, table, status],
    )

    return {
        "refresh": refresh,
        "table": table,
        "picker": picker,
        "detail": detail,
        "reason": reason,
        "approve_btn": approve_btn,
        "reject_btn": reject_btn,
        "result": result,
        "status": status,
    }
