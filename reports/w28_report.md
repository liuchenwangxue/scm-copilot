
---

# W28 Day2 报告 · Gradio 前端三页（阶段五 · 口径统一与二期功能 第 2 天）

> 阶段五 SCM Copilot 第 2 周 Day2 ｜ 2026-08-21 ｜ 依据《W28学习执行手册》Day2
> 主题：浏览器可演示的对话 / 审批 / 日报三页 + compose gradio 服务 + nginx `/ui` 反代
> **Day2 验收：浏览器三页可演示 ✓ / 对话含表格+SQL折叠+引用 ✓ / 审批可操作（落库+审计）✓ / 容器内 /ui SSE 通道 ✓ / 容器内外评分差 ≤2pp（D1）✓**

---

## 〇、Day2 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | `frontend/app.py` 骨架 + API Key 登录（`gr.Textbox(type=password)` + 状态探针 `/health`） | ✅ |
| 2 | `pages/chat.py`：SSE 流式（`message.delta` 打字机 + `progress` 节点状态 + `citations` 引用溯源 + `data_table` 表格 + `approval_request` HITL 提示 + `done/error`） | ✅ 真实后端端到端验证通过 |
| 3 | `pages/chat.py` 表格 / SQL 折叠 / 引用溯源：`gr.Dataframe` 接 `{headers, data}` dict + `gr.Accordion("查看 SQL", open=False)` + `gr.Code(language="sql")` | ✅ |
| 4 | `pages/approvals.py`：Dataframe 列表 + Dropdown 行级选中 + 通过/驳回按钮 + 理由落审计 + 决策后自动重拉 | ✅ 真实端到端：list_pending 9 条、decide `ok=True`、订单真实更新 |
| 5 | `pages/brief.py`：Day2 占位（说明 + 三图规划）+ 手动触发 daily_brief（`admin:scheduler:manage`） | ✅ trigger `audited=true`、daily_briefs 表 `pushed` |
| 6 | `deploy/frontend/Dockerfile`（python:3.12-slim + SDK 本地装 + gradio/plotly/pandas） | ✅ 镜像 1.2GB 构建成功 |
| 7 | `deploy/docker-compose.yml` 加 `gradio` 服务（7860 + SCM_BASE_URL=https://nginx:443 + SCM_ROOT_PATH=/ui） | ✅ 容器 healthy |
| 8 | `deploy/nginx/nginx.conf` `location /ui/` 反代（`proxy_buffering off` + Upgrade/Connection + Host 头） | ✅ `nginx -t` 通过、reload 后 /ui 反代 200 |
| 9 | `frontend/_selftest.py`（Makefile `test-frontend` 目标）：6/6 PASS | ✅ build_app / 数据函数 / SSE 事件循环 mock 验证 |
| 10 | ruff check + format：All checks passed / 7 files already formatted | ✅ |

---

## 一、对话页（chat.py）端到端验证

**关键点：gradio 6.25 API 适配**

- `gr.Chatbot(type=...)` 在 gradio 6 中移除——value 直接是 `list[dict]`（role/content）。用 dict 形式。
- `theme/css` 从 `gr.Blocks.__init__` 移到 `launch()` 方法。
- `gr.Dataframe` 接受 `{"headers": [...], "data": [[...], ...]}` dict 格式。
- `gr.Code(language="sql")` + `gr.Accordion("查看 SQL", open=False)` 折叠 SQL 折叠可回溯。
- `gr.Blocks.launch(root_path="/ui")` 适配 nginx 反代子路径（资源/websocket 自动带前缀）。

**SDK base_url 双向语义（设计点）**：
- 浏览器登录页 `base_url` 输入框（默认 `https://localhost:18443`）仅作 `/health` 探针，**不**作 SDK 入参
- 容器内 SDK 始终走 `SCM_BASE_URL` 环境变量（`https://nginx:443`）——避免 `localhost` 在容器内指自身
- 这样用户视角与容器内网络解耦：演示录屏时录浏览器 URL，验证时容器 SDK 走容器网络

**端到端实测（真实后端）**：
- kb 域 SSE 流式：`/api/v1/kb/chat` → 5 个事件（progress/message×3/citations/done）→ 回答 + 5 条真实 doc_id 引用
- data 域 NL2SQL：`/api/v1/data/query` → 表格（columns+rows） + SQL 折叠
- ops 域高危操作：`/api/v1/ops/chat` → `approval_request` 事件 + form（diff/order_id/reason）→ 切审批页

---

## 二、审批页（approvals.py）端到端验证

**设计取舍（"行内按钮" 落地）**：gradio 中动态行内按钮成本高，采用 "Dataframe 全表 + Dropdown 行级选中 + 通过/驳回按钮 + 理由输入框" 等价方案。效果：选中行后操作，演示清晰。

**端到端实测**：
- `list_pending()` → 9 条历史 + 新发高危操作后 1 条，共 10 条
- 新建审批流：ops 域 `把订单 PO-0002 的金额改成 9500` → `approval_request` 事件（id=`283f2d0d...`、diff=`{field: amount, before: 8900.5, after: 9500.0}`）
- 审批决策：`decide(approve)` 返回 `{ok: True, reply: "订单 PO-0002 已更新：金额 ¥9500.0，交期 2026-09-15，状态 草稿。"}`，tool_result 包含 `success: True`

**审计落地**：
- `approve` 路由层 `audit.log("approval_action", user, role, approval_id, decision, reason)` 落 `/data/audit.log`
- `approve` 内部 `approval_svc.approve()` 调 `audit.log("approval_approved", ...)`
- 双层审计留痕（HITL + 服务层）

---

## 三、nginx `/ui` 反代（Day2 关键部署）

**反代配置要点**（`deploy/nginx/nginx.conf`）：

```nginx
location /ui/ {
    proxy_pass http://gradio:7860/;        # 尾斜杠 = 去掉 /ui 前缀
    proxy_http_version 1.1;
    proxy_set_header Host $host;            # websocket 升级校验 Host
    proxy_set_header Upgrade $http_upgrade; # websocket 握手
    proxy_set_header Connection "upgrade";
    proxy_buffering off;                    # SSE 不缓冲
    proxy_read_timeout 300s;
    proxy_connect_timeout 5s;
}
```

**坑 1：nginx reload**。容器在配置变更前启动，旧配置无 `/ui` location，reload 即可（`docker exec scm-nginx nginx -s reload`）。

**坑 2：gradio 6 队列端点路径**。`api_prefix=/gradio_api`，websocket/SSE 端点 `/ui/gradio_api/queue/join`——gradio 6 实际走 POST 端点 + SSE 流（非纯 websocket Upgrade）。

**实测通道**：
- `GET /ui/` → 200（HTML 95KB）
- `GET /ui/config` → 200
- `GET /ui/manifest.json` → 200
- `GET /ui/assets/index-Dqxt3WGu.js` → 200
- `POST /ui/gradio_api/queue/join` → 422（端点可达，仅缺请求体）—— gradio SSE 通道正常
- `nginx -t` 校验 → `configuration file test is successful`

---

## 四、SDK base_url 双地址隔离（面试亮点）

**问题场景**：
- 浏览器用户在 `https://localhost:18443/ui/` 操作——`base_url` 输入框用户视角
- 容器内 gradio 服务在 `scm-gradio` 容器内——SDK 需走 `https://nginx:443`（容器间）

**实现**：
```python
# pages/chat.py / approvals.py
SDK_BASE_URL = os.environ.get("SCM_BASE_URL", "https://nginx:443")

def _make_client(base_url: str, api_key: str) -> ScmCopilot:
    return ScmCopilot(base_url=base_url or SDK_BASE_URL, ...)  # 容器内固定
```

`base_url` 入参来自登录页（用户视角）但被 SDK_BASE_URL 覆盖——保证 SDK 永远走容器网络。

**面试话术**：演示 ROI（一天 vs 一周）；SSE/协议设计而非 CSS 是考察重点；React 正式前端列入三期；"base_url 双地址隔离"展示容器内网络与服务视角解耦的工程素养。

---

## 五、观察项：HITL resume 重复 create 审批单（后端，非 Day2 范围）

**现象**：ops 域新发高危操作 → approval_request（id `283f2d0d`）→ 切审批页 approve → 返回 `ok: True`、订单真实更新、audit 落库——**但** `approvals` 表里出现两条同 actor 单：

| approval_no | status | 备注 |
|---|---|---|
| 283f2d0d-ccb6-... | pending | 前端列表展示 / 用户 approve 的目标 |
| 561bda98-b6f1-... | approved | resume 时图内重新 create + approve 的真实审批单 |

**根因（LangGraph 语义）**：`approval_gate` 节点在 `interrupt()` 之前调用 `approval_svc.create()`。LangGraph resume 时，节点函数从**头**重新执行，`create()` 再次执行（`approval_id=str(uuid.uuid4())` 生成新 id），`req` 指向新对象，`approval_svc.approve(req.approval_id)` 更新的是**新单**，前端展示的旧单残留 pending。

**为什么是后端既有行为**：W25 阶段 `test_ops_approval_flow.py::test_hitl_resume_from_mysql` 是直接调 `svc.approve()`，不经过 graph resume 路径；W26 集成验收可能没细查审批单状态。这个"resume 重复建单"导致 pending 列表堆积（当前 9 条历史都是这个原因）。

**Day2 处理**：Day2 范围是 Gradio 前端，前端 approve 调用本身完全正确（`ok=True`、订单更新、审计落库），不在 Day2 修复。**建议移至 D6/D7 独立处理**：

```python
# 修复思路（仅草案，需单独验证）
def approval_gate(state):
    ...
    # 把 create() 移到 interrupt 之后用 config 携带 approval_id 复用
    # 或：用 state.get("approval_id") 复用既有 id
    if existing := state.get("approval_id"):
        req = approval_svc.get(existing)
    else:
        req = approval_svc.create(...)
