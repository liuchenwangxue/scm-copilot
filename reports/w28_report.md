
---

# W28 Day6 报告 · 终验：覆盖率冲 75% + 混沌复验 + 容器 eval 复跑 + Runtime PoC 验收（阶段五 · 第 2 周 Day 6）

> 阶段五 SCM Copilot 第 2 周 Day6 ｜ 2026-08-21 ｜ 依据《W28学习执行手册》Day6
> 主题：覆盖率收尾（B4 终验）/ 混沌五连复验（快速版）/ 全量回归 + 容器内 eval 复跑 + 压测 30 并发快验 / Runtime PoC 内核与同构对照验收
> **Day6 验收：总覆盖率 ≥75%（76% 实测）✓ / otel 连接失败路径 + parser 洼地补测 ✓ / 混沌复验 Redis-down 行为如预期 ✓ / 全量回归 737 passed ✓ / 容器内 eval 复跑 hit@1=0.8974 ✓ / 压测 30 并发 P95 稳定 830ms ✓ / Runtime PoC 单测 18 用例绿 ✓**

---

## 〇、Day6 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | ★ 覆盖率收尾（B4 终验）：otel.py 连接失败路径 + parser 洼地补测 → 总覆盖 65%→**76%**（≥75% Gate 达成） | ✅ `test_otel_failopen.py` 7 例 + `test_parser_coverage.py` 10 例 |
| 2 | 纯逻辑洼地一次补齐：mock_provider/logger/cache/retry_policy/redis_client/store/retriever/hybrid 融合/feedback_store | ✅ `test_coverage_gaps_d6.py` 53 例 |
| 3 | answer_validator 大洼地（7%→高覆盖）：normalize/规则/LLM 校验/CRAG 反思/全流程/补检索 | ✅ `test_answer_validator_d6.py` 30 例 |
| 4 | prompts/eval_nightly/cache_warmup 终验 | ✅ `test_coverage_final_d6.py` 19 例 |
| 5 | ★ 混沌五连复验（快速版）：Redis 挂 fail-open（幂等写拒/锁本地兜底/缓存内存兜底）+ 杀实例 least_conn 摘除 | ✅ 实测通过 + 恢复 |
| 6 | 全量回归 + ruff + mypy | ✅ 737 passed / ruff 0 / mypy 0 |
| 7 | ★ 容器内 eval 复跑：eval_nightly 触发（RAG 156 真 bge 推理 + NL2SQL 100） | ✅ hit@1=0.8974 / recall@5=0.9936 / cit_acc=0.9722 / nl2sql overall=1.0 |
| 8 | 压测 30 并发快验（W27 基线不劣化） | ✅ P95 稳定 830ms / 成功率 100% / 5xx=0 |
| 9 | Runtime PoC 验收：tool-calling 内核 + data 图同构对照 | ✅ `test_runtime_loop.py` 18 用例绿 |

---

## 一、覆盖率收尾（B4 终验，65% → 76%）

### 1.1 手册要求与落点

《W28学习执行手册》Day6：**补 otel.py（连接失败路径）与 parser 洼地至总覆盖 ≥75%**。
目标锁定两个最大洼地 + 一批纯逻辑模块：

| 模块 | 原覆盖 | 补测后 | 说明 |
|---|---|---|---|
| `obs/otel.py` | ~40% | ~90% | **连接失败路径**：OTLP exporter 构造失败 fail-open / 包缺失 fail-open / 未启用不初始化 / setup 幂等 / trace_id 在 span 内 16 位 hex |
| `rag/parser/*` | ~55% | ~85% | **洼地**：真实 PDF（pdfplumber 字号分层 + 表格 + 扫描页）+ 真实 docx（python-docx Heading/表格）+ registry 批量坏文件 |
| `domains/kb/agent/answer_validator.py` | 7% | ~90% | 最大的纯逻辑洼地（203 行未覆盖）——normalize/规则/LLM 校验/CRAG 反思/补检索全流程 |
| `shared/llm/mock_provider.py` | 33% | ~85% | 冲突检测 / 空上下文诚实拒答 / 多来源综合 / generate/stream/json |
| `shared/obs/logger.py` | 43% | ~90% | JsonFormatter dict/str/exc / setup 幂等 / log_event / 中间件（X-Request-Id 注入 + http_request 落盘 + 非 http 透传 + disabled） |
| `shared/reliability/cache.py` | 35% | ~90% | QueryCache 命中/过期/删除/Redis 故障内存兜底 |
| `shared/reliability/retry_policy.py` | 58% | ~90% | 重试救回 / 非重试直抛 / degrade_chain 各级 / 业务错误透传 |
| `shared/reliability/redis_client.py` | 66% | ~95% | 全操作 fail-open（注入假 client 抛异常） |
| `rag/store.py` + `rag/retriever.py` | 12%/0% | ~85% | 假 Qdrant 客户端：upsert/query 过滤/重试/collection 管理/租户路由 |
| `rag/hybrid_retriever.py` 融合 | 65% | ~85% | RRF/weighted 归一化/租户 BM25 过滤/chunk_meta 容错 |
| `domains/kb/feedback/feedback_store.py` | 22% | ~85% | 提交/审核/回流评测/统计 |

### 1.2 otel 连接失败路径（B4 点名项）

`test_otel_failopen.py` 7 用例覆盖**不依赖真实 OTLP 端点**的失败注入：

- `OTEL_ENABLED=0` → 完全不初始化（get_tracer None / trace_id ""）
- OTLP exporter 构造失败（patch 源模块 `opentelemetry.exporter...trace_exporter.OTLPSpanExporter` 抛错）
  → fail-open：不提供 tracer、不阻塞业务——**注意坑**：`setup()` 内是局部 `from ... import OTLPSpanExporter`，
  必须 patch 源模块属性而非 `otel.OTLPSpanExporter`
- `opentelemetry` 包导入失败（patch `builtins.__import__`）→ fail-open
- FastAPI 埋点失败 → 静默（幂等 setup 不炸）
- 真实 span 内 `trace_id()` 返回 16 位 hex（no-op exporter 避免 pytest stdout 关闭噪音）

### 1.3 parser 洼地（B4 点名项）

`test_parser_coverage.py` 10 用例**生成真实文件**走真实解析库（比 mock 适配层信息量大）：

- 最小合法 PDF（手写 PDF 字节流，2 页：标题 24pt + 正文 12pt + 空页）→ pdfplumber 解析出 `#` 标题 + 正文
- 真实 docx（python-docx 生成：Heading 1/2 + 表格 + 正文）→ `#`/`##` 前缀 + 表格 markdown
- 纯函数：`_heading_prefix` 字号分档 / `_rows_from_chars` top 聚类 + x0 排序 / `_table_to_md` 防御
- registry：parse_markdown / 空内容拒 / 批量坏文件记录 errors.jsonl

> 坑：pdfplumber chars 会过滤空格字符 → 断言以实际解析行为为准（"Chapter One" 拼为 "ChapterOne"）。

### 1.4 覆盖率数字

```text
TOTAL   7639 语句，1616 未覆盖，覆盖率 76%（≥75% Gate 达成，W27 终点 66%）
```

确实难测的纯网络壳（LLM real provider 对真实 OTLP/LLM 端点的调用）已在 W27-D5 报告"文档化接受"清单，
本轮不重复——`test_real_provider_errors.py` 已用注入故障覆盖失败路径。

---

## 二、混沌五连复验（快速版，脚本现成）

《W28学习执行手册》Day6：**重点看 W27-D3 改造后的 redis-down 行为（幂等写拒绝、锁本地兜底）是否在演练中如预期**。

### 2.1 Redis 挂（scm-redis docker stop）实测

| 探针 | 预期（W27-D3） | 实测 |
|---|---|---|
| `GET /health` | 仍 200（Redis 非 health 判定项） | ✅ 200 `{"status":"ok","db":"up",...}` |
| `POST /ops/chat` 查单 | fail-open 可用（cache 走内存） | ✅ 200（SSE 流正常） |
| `POST /ops/chat` 改单（写路径） | 幂等 claim fail-closed → 拒绝写 | ✅ 200 响应带明确降级/拒绝提示（不 500 不雪崩） |
| `GET /api/v1/ops/approvals` | 可用（MySQL 权威） | ✅ 200 |
| `GET /api/v1/kb/chat` | 语义路由正常 | ✅ 200 |
| `GET /api/v1/admin/scheduler/jobs` | 调度 leader 锁 → 本地兜底放行 | ✅ 200（任务幂等兜底，零重复语义不破） |

纯逻辑复验（`redis_idem_failopen_check.py`）：幂等降 SQLite 同 key 只执行一次、查询缓存内存兜底命中、
分布式锁 fail-open 放行——**全部 PASS**。恢复 `docker start scm-redis` → PONG，无需重启 backend
（懒连接 + 冷却探测自动切回）。

### 2.2 杀实例（scm-backend-a1 docker stop）实测

| 探针 | 预期 | 实测 |
|---|---|---|
| `GET /health` | nginx least_conn 自动摘除 a1，流量集中 a2 | ✅ 200 |
| `POST /auth/login` | a2 正常服务 | ✅ 200 |
| `GET /ops/approvals`（无 token） | 401（安全边界不丢） | ✅ 401 |

恢复 `docker start scm-backend-a1` → 15s 后 health: starting → healthy（自动回归，双实例负载均衡）。

> 其余三连（MySQL 挂 / Qdrant 挂 / LLM 全超时）由 737 例全量回归覆盖：`test_chaos_degrades.py`（auth fail-open + login 503）、
> `test_hybrid_retriever_degrade.py`（Qdrant 挂 → BM25-only）、`test_llm_degrades.py`（三级模型池 → mock 兜底）全绿——
> 快速版演练不重复杀真容器（破坏性操作留本地手动），回归测试即证据。

---

## 三、全量回归 + 容器内 eval 复跑 + 压测快验

### 3.1 全量回归

```text
737 passed（W28 Day5 终点 600 → +137：本轮新增测试 130+）
ruff check backend: All checks passed
mypy --explicit-package-bases: Success: no issues found in 223 source files
```

### 3.2 容器内 eval 复跑（C1 口径验证）

通过 admin 调度面板手动触发 `eval_nightly`（容器内真 bge embedding + bge reranker）：

| 指标 | 值 | 说明 |
|---|---|---|
| RAG 156 条 | hit@1=**0.8974** / recall@5=**0.9936** / cit_acc=**0.9722** / error_rate=0.0 / errors=0 | 容器内真模型推理全通 |
| NL2SQL 100 条 | overall=**1.0** / single=join=aggregation=1.0 / error_rate=0.0 / rejected=0 | 容器内全绿 |
| 运行时长 | 17 分钟（RAG 156 真 bge CPU 推理，p95_retrieve_ms=8537ms） | CPU 容器真实耗时，非卡死 |

容器日志证据：`[embedder] 加载模型 BAAI/bge-small-zh-v1.5` → `[reranker] bge-reranker 就绪（device=cpu）` →
jieba 构建 + 权重加载完成 → 推理推进——**C1 容器口径统一（真模型）在 eval 链路实证**。

> 观察：W27-D6 曾因 `/data/chunks_title.json` 缺失导致容器 RAG 报告 error_rate=1.0（B16 快失败机制正确拦截了假成功）；
> 本轮 `/data/chunks_title.json` 已在卷中，复跑完整出分——"数据缺失 → 快失败落 FAILED → 补数据 → 恢复"闭环验证。

### 3.3 压测 30 并发快验（W27 基线不劣化）

三连跑（本轮环境非净环境——30+ 容器共享 12 核 VM，W27 基线 714ms 为净环境口径）：

| 轮次 | 总 P95 | ops P95 | 成功率 | 5xx | 备注 |
|---|---|---|---|---|---|
| 第 1 轮（eval 刚结束） | 2519ms | — | 100% | 0 | a1 还在跑 17 分钟 eval 的 CPU 余温 |
| 第 2 轮 | 1125ms | 1561ms | 100% | 0 | 冷却中 |
| **第 3 轮（稳定）** | **830.6ms** | 1540ms | **100%** | **0** | 稳态 |

结论：稳定态 P95=830ms，与 W27 D1 非净环境基线（794ms）同量级、远低于 1.5s Gate；**成功率 100%、5xx=0 三连全保**。
"容器内真模型"未使压测 P95 劣化超 W27 ×1.3 容忍线。数据落 `deploy/reports/day6_load_final.json`。

---

## 四、Runtime PoC 验收（C8/D2，Day6 上午产物复核）

`test_runtime_loop.py` 18 用例全绿：

- **tool-calling 循环内核单测**（原生协议形态）：立即终答 / 单工具回填 / 同轮多工具 / async 工具 /
  max_steps 熔断（防死循环底线）/ registry 缺工具显式 KeyError / ToolSchema.as_dict() OpenAI function 协议形态
- **图节点循环内核单测**：静态边线性 / 条件边路由 / 路由未知键 RuntimeNodeError / 图内环 max_steps 熔断
- **data 图同构对照**（LangGraph vs 自研 runtime，同输入同输出）：合法问题 / 安全拒绝 / 未知问题 / parse-error 修复循环

ADR-011（自研 Runtime 边界）+ ADR-012（记忆分层）均已入库。

---

## 五、周 Gate 对照（Day6 完成项）

| Gate | 状态 |
|---|---|
| 容器内 eval 与本机分差 ≤2pp | ✅ 容器 hit@1=0.8974（本机基线 0.9038，差 0.6pp） |
| 覆盖率 ≥75% | ✅ 76%（W27 终点 66%） |
| 混沌复验过 | ✅ Redis-down 行为矩阵实测 + 杀实例 failover + 回归全绿 |
| 全量回归绿 | ✅ 737 passed / ruff 0 / mypy 0 |
| Runtime PoC 同构对照绿 + tool-calling 内核单测绿 | ✅ 18 用例 |
| ADR-011/012 入库 | ✅ |

---

# W28 Day5 报告 · MCP Server 资产回归 + IM webhook 最小版 + 读写分离 ADR（阶段五 · 第 2 周 Day 5）

> 阶段五 SCM Copilot 第 2 周 Day5 ｜ 2026-08-21 ｜ 依据《W28学习执行手册》Day5
> 主题：D1 MCP Server 资产回归（w6 server + W21 client 合并进平台）/ C6 IM webhook 最小版 / C7·B8 读写分离 ADR-010
> **Day5 验收：MCP server 三只读工具被 kb client 调通（dogfooding 证据）✓ / HTTP transport 平台 Key 鉴权 401 ✓ / webhook 摘要卡片单测 7 用例 ✓ / RO 路由单测 6 用例绿 ✓ / ADR-010 入库 ✓ / 全量 600 passed + ruff·mypy 0 ✓**

---

## 〇、Day5 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | ★ MCP Server 资产回归（D1）：`backend/app/mcp_server/`——FastMCP 包装 ops registry 只读工具 | ✅ 三工具：query_order / query_inventory / daily_report |
| 2 | 鉴权复用平台 API Key：HTTP transport AuthProvider（Bearer sk- → 平台库 api_keys 表 sha256 + owner 权限动态加载） | ✅ 有效 Key 200 / 错误 Key 401 / 无 Key 401（实测） |
| 3 | 审计装饰器（w6 三层栈 @mcp.tool → @audit_call → @require_permission）+ AuditLogger echo=False（MCP stdio stdout 是协议通道） | ✅ `mcp_*` 审计落盘 |
| 4 | **dogfooding 闭环**：kb 域 MCPClient（W21 资产）连平台 MCP server 调通三只读工具 | ✅ `test_mcp_dogfooding.py` 6 用例全绿 |
| 5 | 高危写工具（update_order/cancel_order）**不暴露**（安全边界注释 + 单测断言） | ✅ |
| 6 | ★ IM webhook 最小版（C6/B6）：`ops/notify/webhook.py` + approval_requested 钩子 + SCM_WEBHOOK_URL 开关 | ✅ 3s 超时 + 1 次重试，摘要不发敏感值 |
| 7 | ★ 读写分离 ADR-010 + `shared/db.py` 路由开关（B8 PoC 口径） | ✅ `test_db_routing.py` 6 用例绿；ADR-010 入库 |
| 8 | compose 加 mcp-server 服务（HTTP transport，18765:8765，复用 scm-backend 镜像） | ✅ `docker compose config` 语法通过 |
| 9 | 全量回归 + ruff + mypy | ✅ 600 passed / ruff 0 / mypy 0 |

---

## 一、MCP Server 资产回归（D1，核心）

### 1.1 为什么是"资产回归"不是"新增功能"（面试叙事）

w6 已做过供应链 FastMCP server（7 工具 + RBAC + 审计 + 重试 + 幂等，双传输），且工具清单
与 ops registry 同域同名（query_order 同名同域）；kb 域有 MCP client（W21，stdio 消费第三方
server）。Day5 把**两半资产合并**：FastMCP 包装 ops tool registry，平台正式具备 MCP server 侧。

- 复用面：w6 三层装饰器栈（`@mcp.tool() → @audit_call → @require_permission`）+ W21
  `MCPClient`（`mcp_client.py` 直接作为 dogfooding 客户端，零改造）。
- 落点：`backend/app/mcp_server/`——`main.py`（FastMCP + 工具）、`auth.py`（AuthProvider）、
  `apikey_db.py`（平台 API Key → 用户/权限，同步查平台库）。

### 1.2 三只读工具 + 安全边界

| 工具 | 实现 | 权限码 | 数据源 |
|---|---|---|---|
| `query_order(order_id)` | `OrderTools.query_order`（registry 分发） | `ops:order:read` | mock-biz 实时订单 |
| `query_inventory()` | `ReportTools.generate_report("inventory")` | `ops:order:read` | mock-biz 库存报表 |
| `daily_report(brief_date?)` | `_read_daily_brief`（平台库 daily_briefs 表） | `admin:brief:read` | MySQL |

**高危写工具（update_order/cancel_order）不暴露**——MCP 面只读；写操作必须走平台 REST +
approval_gate（HITL），MCP 调用方无法绕过审批流。边界注释写进 `main.py` docstring，单测断言
`update_order`/`cancel_order` 不在工具列表。

### 1.3 鉴权（w6 AuthProvider 模式 → 平台 API Key）

- HTTP transport：`ScmApiKeyAuthProvider.verify_token(token)` 查平台库 `api_keys` 表
  （sha256 哈希匹配 + enabled + owner 存活 + **动态加载 owner 当前权限码**）——与
  `apikeys.authenticate_api_key` 同语义；无效/吊销 → None → fastmcp HTTP 层 401。
- stdio 模式：进程即身份（`MCP_RUN_AS` + `MCP_PERMISSIONS` 环境变量模拟，默认 viewer
  fail-closed）——与 w6 同款设计，dogfooding 测试用此路径。
- 实测：有效 Key → 三工具调通；错误 Key / 无 Key → HTTP 401。

### 1.4 MCP stdio 协议坑（W21 踩坑在本日复发）

`AuditLogger.log()` 默认 `print()` 到 stdout——MCP stdio 的 **stdout 是 JSON-RPC 协议通道**，
任何非 JSON 打印都会导致 client 报 `Failed to parse JSONRPC message`（实测踩中）。
修复：`AuditLogger(path, echo=False)` 加开关，MCP server 用 `echo=False` 静默落盘，
可见审计由 `audit_call` 装饰器显式写 stderr。

### 1.5 dogfooding 闭环证据

`test_mcp_dogfooding.py`（6 用例）：
- `list_tools` 动态发现（三工具在列、高危写工具不暴露）
- `query_order` / `query_inventory` / `daily_report` 三工具被 **kb 域自己的 MCPClient** 调通
- viewer 无权限 → 403（fail-closed）
- `mcp_*` 审计事件落盘（`AuditLogger.filter("mcp_query_order")` 递增）

> 面试叙事就绪："MCP 双侧都做过——W6 生产级 server（RBAC/审计/重试/幂等）、W21 client
> 消费第三方工具，Day5 合并进平台，接入方式全家桶（SDK/OpenAPI/webhook/MCP）"。

## 二、IM webhook 最小版（C6/B6）

- `ops/notify/webhook.py`：`send_approval_webhook()` POST 群机器人摘要卡片（企微/钉钉
  msgtype=text 兼容），**只发审批 id 前缀 + 工具名 + 变更字段名**（金额/日期/原因值不进群）。
- 3s 超时 + 1 次重试后放弃；`SCM_WEBHOOK_URL` 空 = 关闭（默认）；`notify_approval_requested_async`
  线程 fire-and-forget——通知尽力而为，审批状态机不受影响。
- 挂钩点：`ApprovalService.create()` 审计 `approval_requested` 之后（与审计同源触发）。
- 测试 7 用例：卡片脱敏 / URL 空关闭 / 2xx 一次成功 / 5xx 重试后放弃 / 网络异常重试 /
  失败不影响审批 create。

## 三、读写分离 ADR-010（C7/B8 PoC 口径）

- `shared/db.py`：`DbRouter(write_dsn, read_dsn)`——读操作且有有效副本（`read_dsn != write_dsn`）
  → 走 RO；写/无副本/副本=主 → 走主（**零行为差异**）。
- `settings.platform_ro_dsn`（`SCM_DB_RO_DSN`，默认空）；`main.py` lifespan 仅在有副本时创建
  `read_session_factory`（本机无副本 → 不建，行为与 D1 完全一致）。
- `docs/adr/ADR-010_读写分离.md`：L1 单主 → L2 读写分离（本步）→ L3 拆库三级演进 + 触发阈值
  + 一致性边界（报表允许最终一致，在线写恒走主）+ 回退方案。
- 测试 6 用例：读走 RO / 写走主 / 无副本回退 / 同 DSN 无副本 / 语义别名 / 多库构造。

## 四、部署与质量

- compose 加 `mcp-server` 服务（复用 `scm-backend:latest` 镜像，HTTP transport，
  `MCP_PORT=8765` 容器内 / 宿主 18765；healthcheck 用 TCP 端口探测——MCP HTTP 端点是
  流式协议，urllib GET 会 406 误判）。
- 全量回归 **600 passed**（含新增 19 用例）；ruff 0 error；mypy 0 error。

---

# W28 Day3 报告 · BI 图层（阶段五 · 口径统一与二期功能 第 3 天）

> 阶段五 SCM Copilot 第 2 周 Day3 ｜ 2026-08-21 ｜ 依据《W28学习执行手册》Day3
> 主题：经营日报从"数字文本"到"趋势图表"——图表数据 API + 前端 Plotly 三图（C3/B4 项）
> **Day3 验收：三图真数据渲染 ✓ / SQL 折叠可回溯 ✓ / admin:brief:read 权限闸 ✓ / API Key 令牌桶限流 ✓ / 全量回归绿 + ruff·mypy 0 ✓**

---

## 〇、Day3 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | 权限：新增 `admin:brief:read`（seed 13→14 权限 / 26→27 映射；`test_rbac`/`test_seed_platform` 同步） | ✅ 幂等 seed 后 14 权限 27 映射 |
| 2 | `domains/admin/brief_charts.py`：`GET /api/v1/admin/brief/charts`——近 7 日 GMV/延迟率趋势 + 最近一日 TOP5 + 三条 SQL 原文 + 9.91% 基准虚线 | ✅ 挂 `admin:brief:read` |
| 3 | 空值兜底（COALESCE 语义）：metrics 缺字段 → None，图整张不挂；无记录 → `latest_date=None` 空态 | ✅ |
| 4 | 限流：admin 面 API 也是 API——`rbac.require_permission → api_key_or_jwt` 自动过令牌桶（429 + Retry-After） | ✅ `test_api_key_rate_limit_429` |
| 5 | `frontend/pages/brief.py`：三图（GMV 折线含昨日标注点 / TOP5 横向柱状 rank1 在顶 / 延迟率+基准虚线）+ `gr.Accordion` SQL 折叠 + 空态提示 + Microsoft YaHei 字体 | ✅ |
| 6 | `frontend/_selftest.py`：Day3 新增 6 项图表数据函数自测（12/12 PASS） | ✅ |
| 7 | 容器重建 + 端到端验证：login → charts API 真数据（GMV 36,738,101.8 / TOP5 5 行 / SQL 3 条）→ operator 403 | ✅ |
| 8 | 全量回归（UTF-8 模式）+ ruff check + mypy 0 error | ✅ |

## 一、设计要点（面试题）

**图表 = 快照的可视化，不是新的计算路径**：
- 数据源是 `daily_briefs` 表 metrics/sqls JSON 快照（W25 Day3 起积累，已固化口径），
  图表 API 不现算——避免"BI 图层另算一套 → 与日报数字口径漂移"的风险
- SQL 原文一并回放（三条模板问题），"数字可回溯"卖点延续到图表层：
  图里每个点都能展开看它是怎么算出来的
- 延迟率 9.91% 基准虚线 = W25 首份日报实测值（`w25_day3_brief_eval.md`），
  作为趋势对比基线（当前值低于/高于基线的可视化判读）

**权限与限流**：
- 独立权限码 `admin:brief:read`（不塞进既有 admin 权限的复用）：图表是只读面板，
  权限语义精确；seed 单一事实来源 + 测试逐字对齐（W25 Day5 先例）
- 限流零额外代码：`rbac.require_permission` 依赖 `api_key_or_jwt`，API Key 每请求
  过令牌桶（容量 10 / 5 per min），超额 429 + Retry-After——"admin 面 API 也是 API"

## 二、容器内端到端验证（真实数据链路）

```text
POST /api/v1/auth/login (admin_t_huadong)            → 200
GET  /api/v1/admin/brief/charts (Bearer token)       → 200
  latest_date = 2099-08-19（演示记录，用后即删）
  points      = [ ... 2099-08-19 gmv=36738101.8 delay_rate=9.91 ]
  top_suppliers = 华东供应链A(823万) / 华南制造B(694万) / 华北物流C(571万)
                 / 西南电子D(448万) / 华中食品E(322万)   ← rank 补齐，金额降序
  sqls keys   = [gmv, delay_rate, top_suppliers]      ← SQL 原文可回溯
GET  /api/v1/admin/brief/charts (operator)            → 403（admin:brief:read 权限闸）
```

**空值兜底实测**：库里既有的 2026-08-20/21 两条日报（容器 mock NL2SQL 结果为空 →
metrics 全 null）被 API 原样返回为 `gmv=None/delay_rate=None`，图不炸——COALESCE
语义在真实环境生效。

## 三、前端三图

- **GMV 折线**：万元轴（hover 显示原值千分位，数字不因单位换算失真）+ 昨日标注点
- **TOP5 供应商**：横向柱状（反转 y 轴让 rank1 在顶）+ hover 原值
- **延迟率趋势**：折线 + `9.91%` 基准虚线（`add_hline`，后端 baseline 下发，前后端不双写）
- 中文字体：plotly 全局 `font_family="Microsoft YaHei"`（手册坑）
- SQL 折叠：`gr.Accordion("查看 SQL（数字可回溯）")`；无日报数据 → 空态提示引导手动触发

## 四、测试与质量

- 新增 `test_brief_charts_api.py` 9 用例：纯逻辑兜底（_as_float/_to_point/TOP5/SQL 空态）
  + 权限闸（operator 403 / 匿名 401）+ 真数据（7 日升序 + TOP5 + SQL 回溯）
  + 缺字段 COALESCE + API Key 429 限流
- 权限同步：`test_rbac`（admin 14 权限）/ `test_seed_platform`（14 权限 27 映射）全绿
- 全量回归：UTF-8 模式全绿（Windows GBK 编码坑用 `PYTHONUTF8=1` 规避，既有现象非本次引入）
- ruff check + mypy：0 error

## 五、观察项（非 Day3 范围）

- **容器内 daily_brief 空指标**：业务库 seed 固定到 2026-08-18（`BASE_DATE` 固定保证重放一致），
  日期推进后"昨日"（08-19 之后）无订单 → NL2SQL 返回 0 行 → metrics null。BI 图层忠实
  反映快照数据（null 不炸）；演示"图有数字"需先补业务数据（`make seed-biz` 或按 W25
  演示路径插入当日订单）再触发 daily_brief——留 D4/D5 演示准备时处理

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
