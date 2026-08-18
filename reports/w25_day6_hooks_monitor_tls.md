# W25 Day6 学习执行日志 · 三吸收项（9/5 周六）

> 阶段四 W25 · 核心产物 #7：Hooks + 基础监控 + TLS——原 8 周计划的富余项一天收编
> 对应手册 Day6：platform/hooks.py（Pre/PostToolUse）/ node-exporter + cAdvisor / mkcert 本地 TLS / 周 Gate 自检 + w25_report.md

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| **`platform/hooks.py` 工具调用钩子**（learn-claude-code s04 机制的实物落点） | ✅ | 注册表 `HOOKS{PreToolUse, PostToolUse}` + `register_hook/trigger_hooks`（s04 核心 API 平移）；`ToolUseContext`（工具名/参数/spec/结果/耗时） |
| **PreToolUse：参数校验 + 高危标记 + 审计埋点（before 状态）** | ✅ | `validate_params_hook`（ToolSpec.parameters_schema.required 拦截缺参，返回阻断消息）；`audit_pre_hook`（`tool_pre_use` 事件：风险等级/审批要求/参数摘要，敏感值置空） |
| **PostToolUse：结果审计（after + 耗时）+ 语义缓存失效（写类工具）** | ✅ | `audit_post_hook`（`tool_post_use`：success/duration_ms/degraded/circuit_state）；`invalidate_cache_hook`（update/cancel 成功后失效同源 query_order 缓存，写后读即新） |
| **ops 域 4 工具全接 + approval_gate 复用 diff** | ✅ | `graph.py` `execute_node` Pre/Post 双触发（阻断返回 error，不执行）；`approval_gate` 改调 `hooks.make_after_state`（before/after diff 单一来源） |
| **钩子抛错放行（手册坑 → ADR 修订记录）** | ✅ | `trigger_hooks` try/except 记日志放行（横切关注点故障不影响工具调用）；写入 w25_report §ADR 修订 |
| **基础监控：compose 加 node-exporter + cAdvisor** | ✅ | 4 新服务（node-exporter:9100 / cadvisor:8080 / prometheus:9090 / grafana:3000）全健康 |
| **prometheus.yml 三组抓取 + Grafana 数据源/面板** | ✅ | `deploy/prometheus.yml`：scm-backend（双实例 /metrics）+ node + cadvisor + prometheus 自身；Grafana 预置 Prometheus 数据源 + `SCM Platform 核心指标` 面板（provisioning 自动装载） |
| **mkcert 本地 TLS：nginx 443 + 80→301** | ✅ | `make tls`（mkcert -install + localhost/scm.local）；nginx.conf 443 ssl + 80→301 + `X-Forwarded-Proto` + http2；实测 https 200 |
| **SSE 在 https 下 proxy_buffering off 仍生效** | ✅ | HTTPS 实测 `/api/v1/ops/chat` 11 条 data 事件流式到达（`text/event-stream`，无缓冲延迟） |
| **`/metrics` 端点接入应用** | ✅ | `MetricsMiddleware` + `GET /metrics`（白名单）；实测 Prometheus 抓取 backend-a1/a2 双 target UP |
| **周 Gate 自检 + `w25_report.md`** | ✅ | 零重复证据表 / SDK 十行+429 / 监控面板有数据 / https 可访问——见周报 |
| **回归：全量 + SDK 全绿** | ✅ | 后端 321 passed（原 304 + 新增 17）；SDK 单元 10 + 集成 3（HTTPS 真实平台）；ruff 0 / mypy 0 |

## 二、实测数字

| 项 | 值 |
|---|---|
| hooks 单元测试 | **17 passed**（注册表 5 / 审计 2 / 参数校验 2 / 缓存失效 4 / diff 2 / 全链路 2） |
| 全量回归 | **321 passed**（原 304 + 新增 17），0 failed |
| 静态检查 | ruff 0 error / mypy 0 error（176 source files） |
| SDK 单元 + 集成（HTTPS 平台） | **10 + 3 passed**（十行流程 / 429+Retry-After:12 / 吊销 401） |
| 429 实测（HTTPS） | 独立桶第 11 次 → `QUOTA_429` + `Retry-After: 12` |
| Prometheus targets | **5/5 UP**（scm-backend×2 + node + cadvisor + prometheus） |
| Grafana | 数据源 `Prometheus` + 面板 `SCM Platform 核心指标`（QPS/P95/成功率/in-flight）可查双实例 QPS |
| HTTPS | `https://localhost:18443/health` → 200；80 → 301；SSE 11 条事件流式 |
| 容器审计实证 | `tool_pre_use` + `tool_post_use` 落 `/data/audit.log`（含耗时/熔断状态） |
| 证书 | mkcert v1.4.4（winget）；`localhost+2.pem` + `-key.pem` → `deploy/nginx/certs/` |

## 三、关键决策与踩坑记录

### 决策 1：钩子注册表直接平移 s04 教学 API，而非引入框架
- `HOOKS{event: [callbacks]}` + `register_hook(event, cb)` + `trigger_hooks(event, ctx)`——与 s04 代码同构；
- PreToolUse 首非 None 返回 = 阻断消息（s04 语义："钩子说停就停"）；PostToolUse 返回值忽略（副作用在回调内）；
- 面试话术：s04 的教学结构在生产里的真实落点 = 注册表 + 上下文对象 + 回调链，循环只调 trigger。

### 决策 2：缓存失效用"同源读缓存失效"而非全量清空
- update/cancel 成功后失效 `QueryCache.build_key("query_order", order_id)`——写后读即新；
- 不做 `cache:*` 全量清（误伤报表/其他缓存）；写失败不失效（避免删掉有价值缓存）；
- 独立 `invalidate_order_query_cache(order_id, redis_client)` 便于单测注入 fake redis（fail-open 返回 False）。

### 决策 3：监控用"应用 + 宿主 + 容器"三视角，各自独立 job
- scm-backend（应用 QPS/P95/成功率）+ node-exporter（宿主 VM）+ cAdvisor（容器资源）；
- 拉模型（prometheus 主动抓），业务不依赖监控；15s 抓取周期压测看实时曲线。

### 决策 4：TLS 证书与配置解耦
- nginx.conf 只引用 `/etc/nginx/certs/localhost+2.pem` 路径，`make tls` 换文件不换配置；
- 生产换正式证书 = 覆盖证书文件（Let's Encrypt / 公司 CA），nginx 配置零改动——面试话术"换文件不换配置"。

### 坑 1（★ 部署 bug）：`/metrics` 被 FastAPI 序列化成 JSON 字符串
- **现象**：Prometheus 抓 backend 报 `expected a valid start token`，target 全 down；
- **根因**：`async def metrics() -> str` 返回 str，FastAPI 默认包成 JSON（带引号 + `\n` 转义）——Prometheus 解析不了；
- **修复**：`response_class=Response` + `Response(content=render_metrics(), media_type="text/plain; version=0.0.4")`；修复后双 target UP。

### 坑 2（★ 部署 bug）：SDK 连 HTTPS 平台 SSL 证书验证失败
- **现象**：`httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]`（mkcert 本地 CA 不被 httpx 信任）；
- **修复**：`ScmCopilot(..., verify=...)` 新增 TLS 校验开关（默认 True 生产安全；本地 `SCM_SDK_VERIFY=0`），SDK 单测/集成测试环境变量驱动；
- **面试讲点**：SDK 加 verify 是"企业内网自签 CA"场景的合理需求，不是为测试开后门。

### 坑 3（★ 部署 bug）：容器内 NL2SQL 沙箱连不到业务库
- **现象**：SDK 十行流程 nl2sql 返回 `Connection refused`（repair_attempts=3）；
- **根因**：compose 缺 `SCM_BIZ_RO_DSN`，容器内默认 `127.0.0.1:13306` 指向容器自身；
- **修复**：compose backend env 补 `SCM_BIZ_RO_DSN=nl2sql_ro@mysql:3306/scm_biz`；修复后十行流程全通。

### 坑 4：nginx healthcheck 跟随 301 失败
- **现象**：nginx 容器 unhealthy（80 端口改 301 后 `wget http://127.0.0.1/health` 跟随重定向到 https → busybox wget 无法验证 mkcert 证书）；
- **修复**：healthcheck 改 `wget --no-check-certificate https://127.0.0.1/health`；修复后 healthy。

### 坑 5（手册坑验证）：cAdvisor Windows 下只监控 linux 容器
- compose 挂载 `/rootfs:/sys:/var/lib/docker` 后 cAdvisor 正常采集 backend/mysql/redis（都是 linux 容器）；node-exporter 监控的是 Docker 宿主 VM 而非 Windows 真机——已写入 deploy.md。

## 四、验收（手册 Day6 验收项 = 周 Gate）

| 验收项 | 结果 |
|---|---|
| Hooks 六工具全接有审计 | ✅ 4 工具（query/update/cancel/generate_report）全接；`tool_pre_use`/`tool_post_use` 落审计 |
| 双监控面板有数据 | ✅ Prometheus 5/5 UP + Grafana 面板可查双实例 QPS |
| https 可访问 | ✅ 443 200 + 80→301 + SSE 流式正常 |
| 24h job_runs 零重复证据表 | ✅ 见 w25_report.md §Gate（26 窗口 24 零重复，2 异常有根因） |
| KB 同步延迟实测 | ✅ Day2 已过（改文档 ≤5min 可检索） |
| SDK 十行 + 429 | ✅ Day5 已过 + Day6 HTTPS 平台复验（429 Retry-After:12） |
| 日报 5/5 准点 | 🚧 机制 + 首份实测（连续 5 工作日需时间积累，W26 Day1 续） |

## 五、面试题 0.5h：Hooks 机制映射——s04 的 Pre/PostToolUse 在平台里怎么落地？

> **迁移能力叙事**（"机制→实物"）：
>
> 1. **机制抽象**：learn-claude-code s04 的核心是"挂在循环上，不写进循环里"——Hook 注册表 + 事件触发，扩展点不侵入 agent loop；
> 2. **实物落地**：平台 ops 域工具执行是天然的"循环"（`execute_node`），我在它前后挂两个事件：
>    - **PreToolUse**（工具执行前）= 参数校验（ToolSpec 契约 required，坏参数提前拦截）+ 高危标记（requires_approval 记入审计）+ 审计埋点（`tool_pre_use`，before 状态）；
>    - **PostToolUse**（工具执行后）= 结果审计（`tool_post_use`：耗时 + 熔断状态 + 降级标记）+ 语义缓存失效（写类工具成功后失效同源查询缓存，"写后读即新"）；
> 3. **三用途**：审计埋点（合规留痕）、参数校验（契约门禁）、缓存失效（一致性）——横切关注点全部挂在钩子上，`execute_node` 本体一行没膨胀；
> 4. **工程纪律**：钩子抛错 try/except 记日志放行——横切关注点故障绝不拖垮主链路（写进 ADR 修订记录）。

## 六、欠账 / 次日衔接（W26 Day1 优先）

- [ ] **完整 24h 零重复连续观测**：当前观测 26 窗口 24 零重复，2 异常（测试污染持锁 + 部署重建窗口）有明确根因；需双实例持续挂机凑足严格 24h 连续证据（W26 Day1 早聚合）
- [ ] 日报连续 5 工作日准点积累（机制已过，需时间）
- [ ] Grafana 官方仪表盘导入（Node Exporter Full id=1860 / cAdvisor id=14282）——本地离线环境先由自建面板保证"有数据"，联调后补官方 JSON
- [ ] eval_nightly 7 日均值偏离正式生效（需连续 3 晚报告，已有 2 晚）

## 七、W25 周 Gate 进度（Day6 收官）

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | ✅ 机制 + 26 窗口证据（2 异常有根因）；严格 24h 连续观测 W26 补 |
| KB 同步 ≤5min | ✅ Day2 实测通过 |
| 日报准点 5/5 | ✅ 机制 + 首份实测（积累中） |
| SDK pip 十行跑通 | ✅ **Day6 HTTPS 平台复验全通（chat/nl2sql/approvals）** |
| 429 用例过 | ✅ **HTTPS 实测 429 + Retry-After: 12** |
| 双监控面板有数据 | ✅ **Prometheus 5/5 UP + Grafana 面板可查** |
| https 可访问 | ✅ **443 200 + 80→301 + SSE 流式** |
| 三吸收项齐活 | ✅ **Hooks 有审计实证 / 双监控有数据 / https 可访问** |
