# SCM Copilot 前端（★ W28 Day2：Gradio 三页）

浏览器演示载体：**对话 / 审批 / 日报**，直接走 SCM SDK 0.2.0（dogfooding）。

## 快速开始（本机开发）

```bash
pip install -r frontend/requirements.txt
python frontend/app.py          # 默认 https://localhost:18443，端口 7860
# 打开 http://localhost:7860 → 顶部输入 API Key（sk-）→ 连接
```

环境变量（可选）：
- `SCM_BASE_URL` 平台地址（默认 `https://localhost:18443`，nginx https 入口）
- `SCM_API_KEY` 默认 API Key（可空，登录区手填）
- `SCM_FRONTEND_PORT` 监听端口（默认 7860）
- `SCM_ROOT_PATH` nginx 反代到 `/ui` 时设 `/ui`（容器内由 compose 注入）

## 三页功能

| 页 | 功能 | 对应后端 |
|---|---|---|
| 💬 对话 | SSE 流式打字机；`data_table` → 表格；SQL 折叠可回溯；引用溯源列表；kb/ops 双域；ops 高危触发 `approval_request` 提示 | `/api/v1/{kb,ops}/chat` |
| 🛂 审批 | 待审批列表（Dataframe）；选中行查看 diff 明细；通过/驳回（理由落审计）；决策后自动重拉列表 | `/api/v1/ops/approvals` + `/approval` |
| 📊 日报 | Day2 占位 + 手动触发 daily_brief（admin 权限）；Day3 接 BI 图表 | `/api/v1/admin/scheduler/jobs/daily_brief/trigger` |

## 部署（compose + nginx）

```bash
docker compose build gradio
docker compose up -d gradio
# 浏览器 https://localhost:18443/ui（nginx 反代；proxy_buffering off 保 SSE/websocket）
```

- `deploy/frontend/Dockerfile`：前端镜像（SDK 本地安装 + gradio/plotly/pandas）
- `deploy/nginx/nginx.conf`：`location /ui/` 反代 `gradio:7860`（Host/Upgrade 头 + 去前缀）

## 验收清单（W28 Day2）

- [ ] 浏览器打开 https 下三页可用
- [ ] 对话页含表格（data_table）+ SQL 折叠 + 引用溯源
- [ ] 审批页操作落库有审计（后端 audit.log / approvals 表状态变化）
- [ ] nginx `/ui` 反代 SSE/websocket 正常（打字机流式不卡）

> 技术选型注记（面试）：Gradio 非 React——演示 ROI（一天 vs 一周），面试考察点
> 在 SSE/协议设计不在 CSS；"前端正式化（React）"列入三期 backlog。
