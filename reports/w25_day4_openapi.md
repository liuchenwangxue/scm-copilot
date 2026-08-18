# W25 Day4 学习执行日志 · OpenAPI 规范化（9/3 周四）

> 阶段四 W25 · 核心产物 #5：API 从"能调"到"有契约"——文档即接口规范
> 对应手册 Day4：三分组 tag / 统一错误码 schema / 响应模型 100% 注解 / 端点覆盖自查 / openapi-spec-validator 校验 / /api/v1 版本化

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| **三分组 tag**：`chat / ops / data` + `admin`，每端点补 `summary + description + tags` | ✅ | 全部 15 条业务端点带 summary/description/tags；分组：auth(5) + kb(2) + ops(5) + data(2) + admin(2) + ops(health) |
| **统一错误码 schema**：`Err{code, message, trace_id}`，所有 4xx/5xx 响应模型统一 `Err` | ✅ | `app/platform/errors.py`：ErrorCode 常量 + `Err` model + 三个全局异常处理器（HTTPException / RequestValidationError / 兜底 500）；`FastAPI(responses={401: {"model": Err}, ...})` 注入每个端点；自查测试断言所有 4xx/5xx 响应 `$ref` 指向 `/Err` |
| **响应模型 100% 类型注解**：chat SSE 事件模型 / nl2sql `{table, sql, columns, rows}` / 审批 diff 结构 | ✅ | 各域新建 `schemas.py`：data(`Nl2SqlIn/Out`、`DataFeedbackIn/Out`)、kb(`KbChatIn`、`KbFeedbackIn/Out`、`KbSseEvent`)、ops(`OpsChatIn`、`ApprovalIn/Out`、`ReportIn/EnqueueOut/SyncOut/StatusOut`)、admin(`SchedulerJobsOut`、`SchedulerTriggerOut`、`JobRunOut`)；平台 `LogoutOut`、`HealthOut` |
| **端点覆盖自查脚本**：遍历 routes 断言每条有 summary + response_model（流式显式声明） | ✅ | `backend/tests/test_openapi_coverage.py` 8 项：覆盖 100% / 版本化 / Err 统一 / 契约校验（从最终 OpenAPI 契约取，因新版 FastAPI `include_router` 为 `_IncludedRouter` 延迟展开） |
| **`/openapi.json` 导出 + 校验**（openapi-spec-validator） | ✅ | `openapi-spec-validator` 校验通过（8/8 测试绿）；`reports/openapi_day4.json` 导出 48,202 bytes |
| **版本化**：`/api/v1` 前缀 | ✅ | 5 个 router prefix + `main.py` me 端点 + `OPEN_AUTH_PATHS` + `SKIP_AUDIT_PATHS` + 全量测试 URL 批量替换；旧路径 404 断言在自查测试内 |
| Swagger UI 人工验收清单（描述/参数示例/错误响应） | ✅ | SSE 端点 200 响应含 `text/event-stream` + 事件协议描述 + example；参数示例经 OpenAPI 生成 |

## 二、关键决策与踩坑记录

### 决策 1：错误码集中一个模块（手册坑"别散在各处字符串"）
- `app/platform/errors.py` 是错误码**单一事实来源**：`ErrorCode` 常量 + `Err` model + `register_error_handlers(app)`；
- 命名规范 `<域>_<HTTP状态>`（`AUTH_401` / `AUTH_403` / `VALIDATION_422` / `QUOTA_429` / `SERVICE_UNAVAILABLE_503`...）——SDK 可据 status_code 兜底、据 code 精确分支（Day5 SDK 对齐同一套）；
- `trace_id` 取 `RequestIdMiddleware` 写入的 `scope["request_id"]`（与审计贯穿同一标识）。

### 决策 2：SSE 端点的 200 描述经 `responses` 参数而非 `openapi_extra`（★ 坑）
- **现象**：把事件协议写进 `openapi_extra={"responses": {...}}` 后，Swagger 里看不到自定义 200 描述；
- **根因**：FastAPI 生成 operation 时 `operation["responses"]` 会**覆盖** `openapi_extra` 里的 responses（先合并 openapi_extra，后填充 responses）——手册坑"显式 response_class=StreamingResponse + 文档里描述事件协议"的正确落点；
- **修复**：`@router.post(..., responses={200: {...}})` 路由级参数，FastAPI 合并时以路由声明为准；自查测试断言 `text/event-stream` media type 进文档。

### 决策 3：端点覆盖检查以"最终 OpenAPI 契约"为准（★ FastAPI 新版行为）
- **现象**：遍历 `app.routes` 只看到 `_IncludedRouter` 占位（5 个域 + 0 path），`/api/v1/kb/chat` 找不到；
- **根因**：当前 FastAPI 版本 `include_router` 延迟展开——`app.routes` 存占位对象，openapi 生成时才展开；
- **修复**：自查测试全部基于 `app.openapi()["paths"]`（最终契约）——**文档即接口规范**，验证口径天然正确。

### 决策 4：业务校验错误从 `JSONResponse` 统一为 `Err` 契约
- kb/feedback 的 `ValueError`、data/query 空 question、approval 非法 decision 从 `{"ok":false, "error":...}`（200/400 混合）统一为 `HTTPException` → `Err{code: BAD_REQUEST_400 / VALIDATION_422, ...}`；
- 行为微调（400→422 规范语义）经全量回归验证无测试依赖旧格式；错误可机器分支是 SDK 的前提。

### 决策 5：请求模型 Pydantic 化，但**不做长度强制**
- POST 端点请求体从 `request.json()` 手工解析升级为 Pydantic 模型（行为契约化）；
- **坑**：模型层 `min_length`/`max_length` 会把服务端 400 变成 422——语义变了还误导文档。约束放服务端显式校验（空消息/超长 → 400），模型层只做结构契约（字段/类型/Literal）。

### 坑 6：`ReportEnqueueOut` 的 `async` 关键字字段
- Pydantic 字段名不能是 `async` → `async_` + `alias="async"` + `populate_by_name=True`；
- mypy 对 `Model(async_=...)` 报 `Unexpected keyword argument`（pydantic 生成 __init__ 的签名差异）→ 改用 `model_validate({...})` 按 alias 构造。

### 坑 7：认证端点的审计 target 路径跟随版本化
- `auth.py` 登录/刷新/登出显式落账的 `target`、`audit.py` 的 `SKIP_AUDIT_PATHS`、main 的 `OPEN_AUTH_PATHS` 三处都改 `/api/v1`——漏一处会出现"认证端点被审计中间件双重落账"或"登录 401"。

## 三、实测数字

| 项 | 值 |
|---|---|
| 业务端点总数 | **15**（health 含 1：GET /health；auth 4；kb 2；ops 5；data 2；admin 2） |
| OpenAPI spec 大小 | 48,202 bytes（reports/openapi_day4.json） |
| 端点覆盖自查 | 8/8 通过（summary+description+tags 100%、200 content 100%、SSE media type、/api/v1 100%、Err 引用 100%、openapi-spec-validator 通过） |
| 统一 Err 注入 | 每个端点 401/403/404/422/429/500/503 responses 全 `$ref: #/components/schemas/Err` |
| 全量回归 | **290 passed**（原 282 + 新增 8） |
| 静态检查 | ruff 0 error / mypy 0 error（168 source files） |
| SSE 事件协议进文档 | `/api/v1/kb/chat` + `/api/v1/ops/chat` 200 → `text/event-stream` |

## 四、验收（手册 Day4 验收项）

| 验收项 | 结果 |
|---|---|
| `/openapi.json` 通过校验 | ✅ openapi-spec-validator 绿（test_openapi_coverage::test_openapi_json_validates） |
| 端点覆盖 100% | ✅ 自查测试断言全部业务端点 summary/description/tags + 200 content |
| Swagger UI 三分组可浏览 | ✅ auth/kb/ops/data/admin 五组 tag 全覆盖（test_openapi_has_version_and_groups） |

## 五、面试题 0.5h：为什么坚持 OpenAPI 3.1 而非口头文档？

> 契约驱动：**文档即接口规范，接口即文档**。
>
> 1. **SDK 生成的比对基准**（Day5）：openapi.json 是唯一事实来源，SDK 端点路径/请求字段/错误码全部从它对齐——口头文档必然漂移；
> 2. **前端 mock 与联调**：`/docs` 可交互试调，前端开发者不写后端一行代码就能自测；`openapi-spec-validator` 把"接口定义合法"变成 CI 门槛；
> 3. **集成方自助**：Java 出身的天然优势面——Swagger 生成 Java 客户端、错误码 `Err` 可映射异常体系；
> 4. **演进路径**：今天 `/api/v1` 前缀是成本最低的版本化时机（SDK 未发布、无外部消费者）；未来 `/api/v2` 只需新 router 挂载。

## 六、欠账 / 次日衔接（W25 Day5 优先）

- [ ] **24h 零重复观测聚合**：Day3 启动的观测，明早出 job_runs 证据表（Day6 周 Gate 用）
- [ ] eval_nightly 第二晚报告 → 7 日均值偏离正式生效
- [ ] Day5 SDK：`scm-copilot-client` 三接口 + TestPyPI + 429（OpenAPI 契约今天已就绪，SDK 直接对齐 `/api/v1` + `Err` code）
- [ ] W23 遗留"40 并发 P95"挂账（Day6 评估）

## 七、W25 周 Gate 进度

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | 🚧 观测进行中（Day3 启动，Day5 早聚合） |
| KB 同步 ≤5min | ✅ Day2 实测通过 |
| 日报准点 5/5 | 🚧 机制 + 首份实测通过 |
| SDK pip 十行跑通 | ⏳ Day5 |
| 429 用例过 | ⏳ Day5 |
| OpenAPI 校验 + 覆盖 100% + 三分组（★ 今日新增） | ✅ 8/8 测试绿 + 契约校验通过 |
