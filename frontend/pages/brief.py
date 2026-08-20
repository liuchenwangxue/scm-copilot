"""日报页：经营日报图表（★ W28 Day3 接 BI 图层，C3/B4 项）。

数据源：`GET /api/v1/admin/brief/charts`（挂 `admin:brief:read`，W28 Day3 新增权限码）——
返回近 7 日 daily_briefs 表的 metrics 快照（已固化口径，**图表 = 快照的可视化，非现算**），
SQL 原文一并回放 → "数字可回溯"卖点延续到图表层。

三图齐备（对齐手册 Day3）：
1. **7 日 GMV 趋势折线**（含昨日标注点，单位万元轴 + hover 原值）
2. **TOP5 供应商横向柱状**（最近一日，按订单金额降序，rank 1 在顶）
3. **延迟发货率趋势**（含 W25 首份实测 9.91% 基准虚线，低于基线的可视化判读）

设计要点：
- 中文字体：Microsoft YaHei（plotly 全局 template，手册坑）
- 空态：无日报记录（夜间回归重建期）→ 空态提示，图整张不挂（后端 COALESCE 兜底）
- SQL 折叠：`gr.Accordion("查看 SQL（数字可回溯）", open=False)` + 三条模板 SQL
- 手动触发：`POST /api/v1/admin/scheduler/jobs/daily_brief/trigger`（admin 权限），
  演示路径：改业务数据 → 手动触发 → 次日/当日 brief 图表变化
"""

from __future__ import annotations

import os

import gradio as gr
import httpx
import plotly.graph_objects as go

# 容器内 SDK 默认走 nginx 入口（环境变量可覆盖）；浏览器登录的 base_url 优先于它
SDK_BASE_URL = os.environ.get("SCM_BASE_URL", "https://nginx:443")

# 中文字体（手册坑：plotly 中文乱码 → 全局字体 Microsoft YaHei）
_FONT_FAMILY = "Microsoft YaHei, PingFang SC, SimHei, sans-serif"
# 延迟率基准（W25 首份日报实测 9.91%，`reports/w25_day3_brief_eval.md`；作对比虚线）
DEFAULT_BASELINE_DELAY_RATE = 9.91


def _make_headers(api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return headers


def _resolve_base(b_url: str) -> str:
    """浏览器输入的 base_url 优先（本地开发可达）；为空时回退容器内 SDK 地址。"""
    return b_url.strip() or SDK_BASE_URL


# ==================== 数据获取 ====================


def fetch_charts(b_url: str, api_key: str) -> dict | None:
    """GET /api/v1/admin/brief/charts。失败返回 None（错误信息由调用方展示）。"""
    try:
        resp = httpx.get(
            f"{_resolve_base(b_url).rstrip('/')}/api/v1/admin/brief/charts",
            headers=_make_headers(api_key),
            verify=False,  # 本地 mkcert 自签证书
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


# ==================== 图表构建（纯函数，可单测） ====================


def _fig() -> go.Figure:
    """统一字体 + 轻量模板（中文字体全局生效，手册坑）。"""
    fig = go.Figure()
    fig.update_layout(
        font=dict(family=_FONT_FAMILY),
        template="plotly_white",
        margin=dict(l=60, r=30, t=40, b=40),
    )
    return fig


def _to_wanyuan(value: float | None) -> float | None:
    """元 → 万元（轴单位可读；None 保留缺值）。"""
    if value is None:
        return None
    return round(value / 10000.0, 2)


def _hover_money(value: float | None) -> str:
    """hover 显示原值（元，千分位）——数字可回溯，不因单位换算失真。"""
    if value is None:
        return "—"
    return f"{value:,.2f}"


def build_gmv_figure(data: dict) -> go.Figure:
    """7 日 GMV 趋势折线：万元轴 + 昨日标注点。"""
    fig = _fig()
    points = data.get("points") or []
    dates = [p["date"] for p in points]
    gmvs = [p["gmv"] for p in points]
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=[_to_wanyuan(v) for v in gmvs],
            mode="lines+markers",
            name="GMV",
            hovertemplate="%{x}<br>GMV %{customdata} 元<extra></extra>",
            customdata=[_hover_money(v) for v in gmvs],
            line=dict(color="#2563eb", width=2),
            marker=dict(size=7),
        )
    )
    # 昨日标注点（最近一日非空才标）
    last = points[-1] if points else None
    if last and last.get("gmv") is not None:
        fig.add_annotation(
            x=last["date"],
            y=_to_wanyuan(last["gmv"]),
            text="昨日",
            showarrow=True,
            arrowhead=2,
            ax=24,
            ay=-32,
            font=dict(size=12, color="#1e40af"),
        )
    fig.update_layout(
        title="7 日 GMV 趋势（万元）",
        xaxis_title="日期",
        yaxis_title="GMV（万元）",
        xaxis=dict(tickangle=-30),
    )
    return fig


def build_delay_figure(data: dict) -> go.Figure:
    """延迟发货率趋势：折线 + 9.91% 基准虚线。"""
    fig = _fig()
    points = data.get("points") or []
    baseline = float(data.get("baseline_delay_rate", DEFAULT_BASELINE_DELAY_RATE))
    fig.add_trace(
        go.Scatter(
            x=[p["date"] for p in points],
            y=[p["delay_rate"] for p in points],
            mode="lines+markers",
            name="延迟率",
            hovertemplate="%{x}<br>延迟率 %{y}%<extra></extra>",
            line=dict(color="#dc2626", width=2),
            marker=dict(size=7),
        )
    )
    fig.add_hline(
        y=baseline,
        line_dash="dash",
        line_color="#9ca3af",
        annotation_text=f"基准 {baseline}%",
        annotation_position="bottom right",
        annotation_font=dict(size=11, color="#6b7280"),
    )
    fig.update_layout(
        title="延迟发货率趋势",
        xaxis_title="日期",
        yaxis_title="延迟率（%）",
        xaxis=dict(tickangle=-30),
        yaxis=dict(ticksuffix="%"),
    )
    return fig


def build_top5_figure(data: dict) -> go.Figure:
    """最近一日 TOP5 供应商横向柱状（rank 1 在顶）。"""
    fig = _fig()
    tops = data.get("top_suppliers") or []
    # 横向柱状自下而上 → 反转让 rank1 在顶部
    suppliers = [t["supplier"] for t in tops][::-1]
    values = [t["gmv"] for t in tops][::-1]
    fig.add_trace(
        go.Bar(
            x=[_to_wanyuan(v) for v in values],
            y=suppliers,
            orientation="h",
            name="订单金额",
            hovertemplate="%{y}<br>%{customdata} 元<extra></extra>",
            customdata=[_hover_money(v) for v in values],
            marker=dict(color="#f59e0b"),
        )
    )
    fig.update_layout(
        title=f"TOP5 供应商（最近一日 {data.get('latest_date') or '—'}，万元）",
        xaxis_title="订单金额（万元）",
        yaxis_title="",
        xaxis=dict(tickformat=".1f"),
    )
    return fig


def render_sqls(data: dict) -> str:
    """SQL 回溯（数字可验证）：三条模板 SQL 原文；被拒/无 SQL 显示占位。"""
    sqls = data.get("sqls") or []
    if not sqls:
        return "（无 SQL 记录——该日日报未生成或模板问题为空）"
    lines: list[str] = []
    for s in sqls:
        lines.append(f"### `{s.get('key', '')}`：{s.get('question', '')}")
        sql = s.get("sql") or ""
        if sql:
            lines.append(f"```sql\n{sql}\n```")
        else:
            lines.append("> （被安全闸拒绝 / 无 SQL）")
    return "\n\n".join(lines)


def render_empty_message(data: dict) -> str:
    """空态提示：近 7 日无日报记录（夜间回归重建期）。"""
    if data.get("points"):
        return ""
    return (
        "📭 近 7 日无日报数据（夜间回归重建期）——请先手动触发 `daily_brief`，"
        "或等待次日 08:00 自动生成后刷新。"
    )


# ==================== 页面构建 ====================


def build(base_url: gr.Textbox, api_key: gr.Textbox) -> dict:
    """构建日报页（在 gr.Blocks 上下文内调用）。返回可测组件 dict。"""
    gr.Markdown(
        "## 供应链经营日报（BI 图表）\n\n"
        "数据来自 `daily_briefs` 表 metrics 快照（已固化口径，可回溯，非现算）：\n"
        "1. **7 日 GMV 趋势折线**（含昨日标注点，万元轴）\n"
        "2. **延迟发货率趋势**（含 W25 实测 9.91% 基准虚线）\n"
        "3. **TOP5 供应商横向柱状**（最近一日，按订单金额）\n\n"
        "每张图 SQL 折叠可回溯（`admin:brief:read` 权限）。"
    )

    with gr.Row():
        load_btn = gr.Button("📊 加载图表（近 7 日）", variant="primary")
        trigger = gr.Button("🔄 手动触发今日日报（admin 权限）", variant="secondary")
    status = gr.Markdown("")
    gmv_plot = gr.Plot(label="7 日 GMV 趋势")
    delay_plot = gr.Plot(label="延迟发货率趋势（9.91% 基准虚线）")
    top5_plot = gr.Plot(label="TOP5 供应商（最近一日）")
    with gr.Accordion("查看 SQL（数字可回溯）", open=False):
        sql_md = gr.Markdown("（加载后显示三条模板 SQL）")

    def _load(b_url: str, key: str):
        """加载图表 + SQL 回溯；失败/空态给出可读提示（图整张不挂）。"""
        data = fetch_charts(b_url, key)
        if data is None:
            return (
                gr.update(),
                gr.update(),
                gr.update(),
                "❌ 加载图表失败（连接/权限错误）——请确认平台地址与 API Key（需 admin 权限）",
                "（加载失败）",
            )
        empty = render_empty_message(data)
        if empty:
            return (
                gr.update(),
                gr.update(),
                gr.update(),
                empty,
                "（暂无 SQL）",
            )
        latest = data.get("latest_date") or "—"
        return (
            build_gmv_figure(data),
            build_delay_figure(data),
            build_top5_figure(data),
            f"✅ 已加载 {len(data.get('points') or [])} 日数据，最近 {latest}；"
            f"基准延迟率 {data.get('baseline_delay_rate', DEFAULT_BASELINE_DELAY_RATE)}%",
            render_sqls(data),
        )

    load_btn.click(_load, [base_url, api_key], [gmv_plot, delay_plot, top5_plot, status, sql_md])

    def _trigger(b_url: str, key: str):
        """手动触发 daily_brief 调度任务（演示：改数据→触发→看图表变化）。"""
        try:
            resp = httpx.post(
                f"{_resolve_base(b_url).rstrip('/')}/api/v1/admin/scheduler/jobs/daily_brief/trigger",
                headers=_make_headers(key),
                verify=False,  # 本地 mkcert 自签证书
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ 触发失败（连接错误）：{type(e).__name__}: {str(e)[:160]}"
        if resp.status_code == 200:
            body = resp.json()
            return f"✅ 已触发 `daily_brief`（audited={body.get('audited')}），点击『加载图表』查看变化。"
        try:
            body = resp.json()
            return f"⚠️ 触发未成功（HTTP {resp.status_code}）：{body.get('message', body.get('detail', ''))[:200]}"
        except Exception:  # noqa: BLE001
            return f"⚠️ 触发未成功（HTTP {resp.status_code}）：{resp.text[:200]}"

    trigger.click(_trigger, [base_url, api_key], [status])

    return {
        "load_btn": load_btn,
        "trigger": trigger,
        "status": status,
        "gmv_plot": gmv_plot,
        "delay_plot": delay_plot,
        "top5_plot": top5_plot,
        "sql_md": sql_md,
    }
