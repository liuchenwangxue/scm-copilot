"""SCM Copilot Gradio 前端主应用（★ W28 Day2，C2：前端三页可演示）。

结构：
- 顶部登录区：API Key（sk-）+ 平台地址，存 `gr.State`（session state）
- 三个 Tab：对话 / 审批 / 日报
  · 对话页：SSE 流式 + data_table 表格 + SQL 折叠 + 引用溯源（kb/ops 双域）
  · 审批页：待审列表 + 通过/驳回（HITL，落库审计）
  · 日报页：Day2 占位（Day3 接 BI 图表）
- 直接走 SCM SDK 0.2.0（dogfooding：前端即 SDK 的消费者，429 自动退避内置）

运行：
    python frontend/app.py            # 本机开发（默认 https://localhost:18443）
    # 或容器内（compose gradio 服务）——见 deploy/docker-compose.yml

环境变量（可选覆盖）：
    SCM_BASE_URL    平台地址，默认 https://localhost:18443（nginx https 入口）
    SCM_API_KEY     默认 API Key（可空，登录区手填）
    SCM_FRONTEND_PORT  监听端口，默认 7860
"""

from __future__ import annotations

import os

import gradio as gr
from pages.approvals import build as build_approvals
from pages.brief import build as build_brief
from pages.chat import DEFAULT_BASE_URL
from pages.chat import build as build_chat

# ---------------- 页面内共享常量（放在这里便于 app 级引用） ----------------
BASE_URL_DEFAULT = os.environ.get("SCM_BASE_URL", DEFAULT_BASE_URL)
API_KEY_DEFAULT = os.environ.get("SCM_API_KEY", "")

_CSS = """
:root {
  --scm-primary: #2563eb;
  --scm-bg: #f8fafc;
}
.gradio-container { background: var(--scm-bg); max-width: 1200px !important; margin: 0 auto; }
#login-bar { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white;
  border-radius: 12px; padding: 12px 16px; }
footer { display: none; }
"""


def _on_login(key: str, b_url: str) -> str:
    """登录按钮：校验输入非空，返回登录状态提示（API Key 有效性由真实调用校验）。"""
    if not key or not key.strip():
        return "⚠️ 请先输入 API Key（sk- 开头）"
    if not b_url or not b_url.strip():
        return "⚠️ 请填写平台地址"
    # 轻量校验：调用 /health（白名单）确认地址可达；API Key 有效性留到实际操作校验
    import httpx

    try:
        resp = httpx.get(f"{b_url.rstrip('/')}/health", verify=False, timeout=5)
        if resp.status_code == 200:
            body = resp.json()
            model_note = f"embedder={body.get('embedder')} reranker={body.get('reranker')} cache={body.get('semantic_cache')}"
            return f"✅ 平台可达（{resp.status_code}），模型：{model_note}。API Key 已保存。"
        return f"⚠️ 平台返回 HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"❌ 无法连接平台（{type(e).__name__}: {str(e)[:80]}）——请确认地址与网络"


def build_app() -> gr.Blocks:
    """组装三页 Blocks。"""
    with gr.Blocks(title="SCM Copilot 供应链智能运营平台") as demo:
        gr.HTML(
            '<div id="login-bar"><span style="font-size:20px;font-weight:700;">🛠 SCM Copilot '
            "供应链智能运营平台</span>&nbsp;&nbsp;·&nbsp;&nbsp;"
            "<span style='opacity:.85'>对话 / 审批 / 日报（W28 Day2 Gradio 前端）</span></div>"
        )
        with gr.Row():
            api_key = gr.Textbox(
                label="API Key（sk-）",
                placeholder="粘贴平台 API Key（sk-...）",
                value=API_KEY_DEFAULT,
                type="password",
                scale=3,
            )
            base_url = gr.Textbox(
                label="平台地址",
                placeholder="https://localhost:18443",
                value=BASE_URL_DEFAULT,
                scale=2,
            )
            login_btn = gr.Button("连接", variant="primary", scale=1)
        login_status = gr.Markdown("")

        # 登录：保存到 State（session 级）+ 状态提示
        login_btn.click(_on_login, [api_key, base_url], [login_status])

        with gr.Tabs():
            with gr.Tab("💬 对话"):
                build_chat(base_url, api_key)
            with gr.Tab("🛂 审批"):
                build_approvals(base_url, api_key)
            with gr.Tab("📊 日报"):
                build_brief(base_url, api_key)

        gr.Markdown(
            "---\n"
            "**演示路径**：对话查数（表格 + SQL 折叠 + 引用）→ 高危操作在对话页触发审批"
            "（`approval_request` 提示）→ 切审批页批准/驳回（恢复 LangGraph 图）→ 日报图表（Day3 接入）。"
            "\n\n> 数据闭环：夜间回归 13 晚证据见 `reports/w28_report.md`；SDK 0.2.0 自带 429 自动退避。"
        )
    return demo


if __name__ == "__main__":
    app = build_app()
    port = int(os.environ.get("SCM_FRONTEND_PORT", "7860"))
    # 本地开发本机访问；容器/nginx 反代时需 host="0.0.0.0"
    # SCM_ROOT_PATH：nginx 反代到 /ui 子路径时设置（gradio 生成资源/websocket 带前缀）
    app.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=port,
        root_path=os.environ.get("SCM_ROOT_PATH") or None,
        show_error=True,
        theme=gr.themes.Soft(),
        css=_CSS,
    )
