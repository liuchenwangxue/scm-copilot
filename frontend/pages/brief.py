"""日报页：经营日报图表（★ W28 Day2 占位 → Day3 接 BI 图层，C3/B4 项）。

Day2 占位：三图规划（GMV 折线 / TOP5 供应商柱状 / 延迟率趋势）+ 手动触发按钮。
Day3 接入 `domains/admin/brief_charts.py` 图表 API（admin:* 权限）后，用 `gr.Plot`
渲染 Plotly 图表（GMV 折线含昨日标注点、TOP5 横向柱状、延迟率+9.91% 基准虚线），
图表数据来自 daily_briefs 表 metrics JSON（已固化口径，非现算），SQL 折叠可回溯。

手动触发：`POST /api/v1/admin/scheduler/jobs/daily_brief/trigger`（需 admin:scheduler:manage），
演示路径：改业务数据 → 手动触发 → 次日/当日 brief 图表变化。
"""

from __future__ import annotations

import gradio as gr
import httpx


def _make_headers(api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return headers


def build(base_url: gr.Textbox, api_key: gr.Textbox) -> dict:
    """构建日报页（在 gr.Blocks 上下文内调用）。返回可测组件 dict。"""
    gr.Markdown(
        "## 供应链经营日报\n\n"
        "本周（W28）接 BI 图层后，本页将展示三张真数据图表：\n"
        "1. **7 日 GMV 趋势折线**（含昨日标注点）\n"
        "2. **TOP5 供应商横向柱状**（按订单金额）\n"
        "3. **延迟发货率趋势**（含 9.91% 基准虚线）\n\n"
        "图表数据来自 `daily_briefs` 表的 metrics 快照（已固化口径，可回溯），"
        "每张图旁带对应 SQL（`gr.Accordion` 折叠）。"
    )

    chart_slot = gr.Plot(label="BI 图表（Day3 接入）", value=None)
    gr.Markdown(
        "> 📌 图表数据源：`daily_briefs` 表（W25 Day3 起积累 7 日历史）；当前为占位，Day3 接入 `brief_charts` API。"
    )

    with gr.Row():
        trigger = gr.Button("🔄 手动触发今日日报（admin 权限）", variant="secondary")
        status = gr.Markdown("")

    def _trigger(b_url: str, key: str):
        """手动触发 daily_brief 调度任务（演示：改数据→触发→看图表变化）。"""
        try:
            resp = httpx.post(
                f"{b_url.rstrip('/')}/api/v1/admin/scheduler/jobs/daily_brief/trigger",
                headers=_make_headers(key),
                verify=False,  # 本地 mkcert 自签证书
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ 触发失败（连接错误）：{type(e).__name__}: {str(e)[:160]}"
        if resp.status_code == 200:
            body = resp.json()
            return f"✅ 已触发 `daily_brief`（audited={body.get('audited')}），稍后本页图表即更新。"
        try:
            body = resp.json()
            return f"⚠️ 触发未成功（HTTP {resp.status_code}）：{body.get('message', body.get('detail', ''))[:200]}"
        except Exception:  # noqa: BLE001
            return f"⚠️ 触发未成功（HTTP {resp.status_code}）：{resp.text[:200]}"

    trigger.click(_trigger, [base_url, api_key], [status])

    return {
        "chart_slot": chart_slot,
        "trigger": trigger,
        "status": status,
    }
