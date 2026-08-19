# SCM Copilot 项目综合分析报告

## 一、项目全貌

**一句话定位**：模块化单体架构的企业级 Agent 平台——把 RAG 知识问答、NL2SQL 数据分析、运维业务操作（含 HITL 审批）三个域整合到统一平台层上，4 周（W23–W26）按计划收官，验收 17/18 项达成，344 测试全绿，总成本 ¥19.95。

### 分周建设历程

| 周   | 主题       | 核心交付                                                     |
| ---- | ---------- | ------------------------------------------------------------ |
| W23  | 平台地基   | MySQL 12 表 + JWT/RBAC + 双域迁入 + 状态外置 + 双实例水平扩展 |
| W24  | NL2SQL 域  | 业务库六表 + 四道闸安全 + Schema Linking + 自修复 + 多轮     |
| W25  | 调度与开放 | APScheduler 六任务 + SDK + OpenAPI/API Key + TLS + 监控      |
| W26  | 收官验收   | Grafana 面板 + 混沌五连 + 压测终版 + 简历/模拟面试           |

## 二、结构与功能分析

### 2.1 分层架构

```
backend/app/
├── domains/          业务域（三个独立 LangGraph 图）
│   ├── data/   NL2SQL：generate→validate→{execute|reject|repair}→format
│   ├── kb/     RAG：清洗→语义缓存→语义路由→混合检索→生成+双校验
│   │           （含 answer_validator 24KB、CRAG、MCP 工具客户端、租户过滤）
│   └── ops/    运维 Agent：intent→approval_gate(HITL)→execute→respond
├── platform/         横切层：auth/apikeys/rbac/audit/hooks/scheduler
└── shared/           基础设施：rag(9模块)/llm(双Provider)/reliability(7模块)/obs
sdk/                  scm_client：chat_stream/nl2sql/approvals
deploy/               10服务 compose + nginx TLS + Prometheus/Grafana + chaos脚本
```

### 2.2 设计亮点（面试卖点，均有量化证据）

1. **NL2SQL 安全纵深**：sqlglot AST 四道闸（单语句/SELECT 白名单/子句写操作拦截/危险函数黑名单）+ `nl2sql_ro` 只读账号 + 3s/200行/1MB 资源约束——攻击 20/20 拦截，准确率 0.970
2. **mock-first 工程纪律**：全链路双 Provider，开发零成本跑通、real 只做指标采样（¥19.95 / 预算 ¥100）
3. **真无状态水平扩展**：8 项状态全外置（会话进 MySQL、高频进 Redis、身份进 JWT），杀实例 210/210 成功、5xx=0
4. **可观测的降级链**：Qdrant 挂→BM25-only、LLM 超时→按 tag 降级 mock dict、MySQL 挂→JWT fail-open——混沌五连当天修 3 个真 bug 并补 8 个测试
5. **调度数据闭环**：30/30 窗口零重复，KB 改文档 5min 内可检索，日报 SQL 全程可回溯

### 2.3 关键指标

| 维度           | 指标                              | 值                               |
| -------------- | --------------------------------- | -------------------------------- |
| NL2SQL         | execution accuracy / 自修复救回率 | 0.970 / 0.933                    |
| Schema Linking | 召回 / token 节省                 | 1.000 / 53.3%                    |
| RAG            | Hit@1 / Recall@5                  | 0.9038 / 0.9936                  |
| 性能           | 30并发 P95（正式基线）/ 40并发    | 1268.8ms / 2087.1ms              |
| 质量           | pytest / 覆盖率                   | 344 passed / **56%（未达 75%）** |

## 三、缺陷与不足

### 3.1 架构级局限（影响最大）

| #    | 问题                                                         | 证据                                                         |
| ---- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 1    | **AsyncMySaver 单连接串行**——40 并发瓶颈根因，ops_query P95 达 3468ms | loadtest_final.md；二期 backlog B1                           |
| 2    | **frontend 是空壳**——只有 README，所有"SSE 呈现"止于后端事件 | `frontend/` 目录                                             |
| 3    | **双实例下的"伪分布式"组件**：熔断器、成本预算都是进程内状态，双实例各算各的 | `circuit_breaker.py`（单机三态）、`cost_budget.py:68-70`（进程内 dict） |
| 4    | **fail-open 无兜底**：分布式锁 Redis 挂直接放行，双实例下可能并发写 | `distributed_lock.py:41-46`                                  |
| 5    | **幂等 fail-open 退化为单机**：Redis 挂→sqlite 降级，跨实例幂等失效 | `idempotency.py:193-221`                                     |

### 3.2 代码债

| #    | 问题                                                         | 证据                                     |
| ---- | ------------------------------------------------------------ | ---------------------------------------- |
| 1    | `real_provider.py` 28KB 职责过重：模型池+重试+错误分类+cost+LangFuse+流式+降级全在一个文件 | 建议拆 5 个模块                          |
| 2    | Data 域多轮会话靠**进程内 LRU dict**（`_SESSIONS` 无锁保护，超限直接 `clear()`），重启即丢，与“状态全外置”的主张矛盾 | `session_ctx.py:204-216`                 |
| 3    | 限制常量分散：`MAX_ROWS=200` 在 validator 和 executor 重复定义 | `sql_validator.py:59` / `executor.py:43` |
| 4    | ops `execute_node` 硬编码 `if tool_name == ...` 分支，registry 没有真正被用于分发——新增工具要改图 | `ops/agent/graph.py:179-199`             |
| 5    | `MockSQLGenerator` 每次调用重新加载评测集文件                | `mock_sql.py:45-67` + `graph.py:81-82`   |
| 6    | 魔法数遍布：数据基准日 `date(2026,8,18)` 硬编码、`DATA_BASE_DATE` 写死在 prompts | `data/prompts.py:37`                     |

### 3.3 测试与质量缺口

- **覆盖率 56% vs 目标 75%**：短板集中在 `reranker.py` 0%、`real_provider.py` 22%、`otel.py` 0%、`pdf/word_parser` 6–10%
- **无独立测试**：reranker 重排路径、real_provider 错误分类/模型切换、distributed_lock、idempotency、cost_budget、query_rewriter
- **夜间回归只积累 2 晚**（Day4 `down -v` 清空数据卷），时间积累指标实际未闭环

### 3.4 产品/演示层面

- **SDK 不自动重试 429**：能识别 `ScmQuotaError.retry_after` 但要调用方自己退避，文档未明示
- **容器内无 embedding/reranker 模型**：Dockerfile 明确不装，容器内检索质量≠本机实测质量，压测数据存在环境口径差
- **demo 录屏、简历 PDF** 仍是人工待办

## 四、改进方向（按优先级）

### P0 —— 直接堵住面试追问的软肋

1. **AsyncMySaver 连接池化**（backlog B1）：每请求独立连接或 `aiomysql.Pool`，目标 40 并发 P95 ≤1.5s。这是唯一被正式记录“未达标”的硬指标，被追问概率最高
2. **Data 域多轮会话外置**：`session_ctx` 迁到 Redis（TTL 天然解决，顺带消灭无锁 race），与“8 项状态外置”叙事对齐
3. **SDK 加 429 自动退避**：`ScmQuotaError` 捕获后按 `retry_after` 重试（默认开、可关），补文档说明

### P1 —— 补测试与拆债

4. **覆盖率冲 75%**：优先补 reranker 降级路径、real_provider 错误分类（`_retryable()` 的边界用例）——这两块是逻辑复杂但纯函数、易测
5. **拆 `real_provider.py`**：model_pool / retry / cost / langfuse / stream 五模块，顺手把模型池写进 `settings.py` 而不是代码常量
6. **统一资源约束常量**：`MAX_ROWS/timeout/bytes` 收敛到 config，消灭双处定义
7. **ops execute_node 改 registry 分发**：`if/elif` 换成 `TOOL_REGISTRY[name].invoke()`，让工具注册表名副其实

### P2 —— 分布式完整性（讲“演进到生产”的故事）

8. **分布式一致性补齐**：熔断状态进 Redis（或接受单机并文档化）、分布式锁 fail-open 加本地互斥兜底、幂等降级时至少拒绝高危写操作
9. **容器内装轻量模型**（或 ONNX 量化 bge-small），让压测口径 = 演示口径

### P3 —— 二期方向（已有 backlog B4–B8）

10. 前端最小可用（哪怕 Streamlit/Gradio 演示层）、BI 图层、IM 审批推送、多租户分片、自研 runtime 内核

## 五、总体评价

**作为学习项目，完成度和工程纪律远超一般水平**——计划 24 项 Gate 全过、mock-first 控制成本、混沌演练修真 bug、报告可复现，这套“验收证据链”本身就是最大卖点。**主要短板是三处“叙事与实现的缝隙”**：状态外置主张 vs 进程内 session_ctx/熔断器、双实例架构 vs 单机可靠性组件、检索质量实测 vs 容器内无模型。这些缝隙正是面试官最可能追问的地方，建议按 P0→P1 顺序在面试期前各修一两处，其余准备好“我知道边界在哪、二期怎么修”的应答即可。