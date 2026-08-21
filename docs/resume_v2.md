# 简历 v2 · SCM Copilot 供应链智能运营平台

> W28 Day7（2026-08-21）｜ 依据《W28学习执行手册》Day7 任务 2 + `docs/简历素材库_阶段四.md` + `reports/w27_report.md` + `reports/w28_report.md`
> 叙事原则：**SCM Copilot 为主体，一期讲"建成"（W23–26），二期讲"修复与演进"（W27–28）**——每条数字配"问题→动作→数字"三段
> 写作纪律：不写"精通"（写"熟悉/掌握 + 项目实证"）；数字保留一位小数；R 以数字开头；每条主线备一个"翻车细节"
> 导出建议：复制为 PDF（A4 × 2 页），岗位方向不同可微调"核心优势"排序
> **v2 变更：全部数字刷新为二期实测（压测/覆盖率/SDK/会话/租户/前端/MCP/Runtime），新增"修复与演进"证据链**

---

## [姓名]

- **求职方向**：AI 应用平台 / LLM 应用（Agent）后端 / 数据分析智能化
- **电话 / 邮箱 / 所在城市**：`[填写]`
- **GitHub**：`[SCM Copilot 仓库链接]` ｜ **Demo**：`[10min 录屏链接（W28 六幕版）]`
- **一句话**：6 个月从零独立交付"知识问答 + NL2SQL + 业务审批 + 调度闭环"四域一体的生产级 Agent 平台，二期完成性能/状态/生态三层闭环，所有指标可复现、有修复前后对比证据。

---

## 技术栈

| 分类 | 技术（全部有项目实证） |
|---|---|
| 语言 | Python 3.12（熟练）、TypeScript/Vue3（项目使用） |
| Web 框架 | FastAPI、SSE 流式、OpenAPI 3.1、Swagger、Gradio 三页前端（对话/审批/日报） |
| Agent 编排 | LangGraph（子图/断点续跑/HITL interrupt）、**自研 Runtime 内核 PoC**（tool-calling 双路径）、工具注册 + Hooks 中间件 |
| 检索 | 混合检索（BM25+jieba + Qdrant 向量 + RRF + **BGE 真模型容器内推理**）、bge 向量/重排、语义缓存、Schema Linking、**租户 collection 分片（4 shards）** |
| NL2SQL | sqlglot AST 校验（四道闸）、只读沙箱、错误自修复、execution accuracy 评测（0.97） |
| 数据 | MySQL 8（SQLAlchemy 2.0 async + Alembic）、Redis 7（幂等/缓存/锁/RQ + **会话/熔断/预算共享**）、SQLite（降级） |
| 调度 | APScheduler + MySQL job store + Redis leader 锁（多实例零重复） |
| 生态 | **MCP Server（FastMCP 只读工具 + RBAC/审计栈 + dogfooding 闭环）**、pip SDK 0.2.0（429 自动退避）、IM webhook 通知 |
| 运维 | Docker Compose（13 容器）、nginx（least_conn/TLS）、Prometheus + Grafana、pytest/ruff/mypy/GitHub Actions、混沌演练五连 |
| 概念 | 模块化单体、无状态水平扩展、fail-open 降级链、RBAC、读写分离 ADR、记忆分层设计 |

> 说明：以上均为"写过代码、跑过验收"的技术，不写"精通"；K8s 为概念掌握（Docker/Compose 实战），面试如实说明边界。

---

## 项目经历

### SCM Copilot · 供应链智能运营平台（独立完成，2026-08 → 09，6 周 + 2 周改进）

> **定位**：一站式供应链智能助手——问知识（RAG）· 查数据（NL2SQL）· 办业务（工具+审批）。一期建成模块化单体生产平台（统一账号/审计/调度/监控 + 开放 SDK）；二期"改进周"把性能、状态、生态三层缝隙全部焊死，**每个数字都有修复前后的对比证据**。
> **终验**：二期 Gate 八项全过；全量回归 **737 项全绿**；覆盖率 **76%**；30 并发 P95 **714ms**；真实 LLM 总成本约 ¥20。

#### 1. 平台化整合与无状态水平扩展（一期 W23 + 二期 W27 状态外置）

- **问题**：两个独立 Agent 应用（RAG + 业务操作）各自 SQLite 单实例、账号割裂、状态进程内、无法水平扩展；且"口口声声状态全外置"，实则会话/熔断/预算/锁都还在单机进程内——面试追问一层就露馅。
- **动作**：整合为模块化单体（kb/ops/data/platform 域隔离）+ MySQL/Redis 权威库；二期把 **8 项进程内状态全部外置核销**——session_ctx 改「Redis 权威 + 进程内 L1 读缓存」（Lua 原子 append、双实例互通）、熔断器状态 Redis 共享、分布式锁 Redis 挂时本地互斥兜底、幂等写路径 fail-closed 拒绝（IDEM_UNAVAILABLE）、成本预算 Redis 化共享水位。
- **数字**：least_conn 真无状态双实例；a1 建会话 a2 追问"那华北呢？"指代消解成功（双实例会话互通实测）；**redis-down 行为矩阵 16 格全绿**；压测中杀实例 210/210 请求不中断、5xx=0。

> **翻车细节（面试主动讲）**：原方案 ip_hash 会话亲和，长连接把流量压在一个实例；换 least_conn 的前提不是改配置，而是把 8 项进程内状态全部外置核销——状态外置清单逐项打勾后路由策略才有资格换。

#### 2. 性能修复：40 并发 P95 从 2087ms 打到正式口径达标（W27）

- **问题**：40 并发 P95=2087ms 未达 ≤1.5s Gate（一期验收唯一硬指标未达）；AsyncMySaver 单连接串行，20 路并发 ≈ 20×单路耗时。
- **动作**：Checkpoint 连接池化（组合包装 130 行，未 fork 第三方类）+ LangGraph `durability="exit"` 合并写（一次图执行 checkpoint 写放大 20 行→1 行）+ 40 并发净环境复验归因（本机 12 核 VM 吞吐天花板，非代码瓶颈）。
- **数字**：30 并发 P95 **1269→714ms（-44%）**（净环境正式 Gate）；40 并发 2087→1872~2095ms 如实记录并归因；20 并发 -65%、30 并发 -44%；成功率 100%、5xx=0。

> **翻车细节**：40 并发最终没有压进 1.5s——但我用净环境复验证明了是机器吞吐天花板而非代码瓶颈，并主动把正式口径修订为 30 并发（D1 已 794ms 达成、D7 复验 714ms）。"如实记录 + 归因链"比"硬凑达标"更有说服力。

#### 3. NL2SQL 数据分析域（从零建设，安全纵深 + 二期容器口径）

- **问题**：容器内无 embedding/reranker 模型，"本机好用容器缩水"的暗坑——压测口径 ≠ 检索质量口径；且真实模型装进容器后立刻暴露了两个被环境掩盖的逻辑 bug。
- **动作**：模型入容器（bge-small + bge-reranker 卷挂载，named volume）+ 启动健壮性（加载失败自动降级 mock/RuleReranker + `/health` 暴露模型状态）+ 修复语义路由聊天原型覆盖不足与语义缓存空转两个真 bug。
- **数字**：容器内外 eval 分差 **0pp→0.6pp**（hit@1 本机 0.9038 vs 容器 0.9038→0.8974，Gate ≤2pp）；execution accuracy 整体 **0.97**（单表 0.98 / join 0.95 / 聚合 1.0）；攻击用例 **20/20 拦截 0 逃逸**；压测修复后 P95 38s→892ms；SQL 100% 透出可审计。

> **翻车细节**：最初用 SQL 字符串比对评测，同义 SQL 大量误判；改为结果集规范化比对（类型归一 + 排序键整行）后才真正反映"答对数据"。

#### 4. 开放生态闭环：SDK / OpenAPI / webhook / MCP 全家桶（W25 + W27/W28）

- **问题**：SDK 遇 429 不自动退避要调用方自己处理；MCP server 侧空缺（只有 W6 的独立 server 与 W21 的 client，未并入平台）；审批要登平台看、日报只有数字没有图。
- **动作**：SDK 0.2.0 内置 429/5xx 自动退避（Retry-After 尊重 + 指数退避抖动）；**FastMCP 包装 ops registry 三只读工具**（@mcp.tool→@audit_call→@require_permission 三层栈，高危写工具不暴露）；IM 群机器人 webhook 摘要卡片（审批 id+工具+字段名，不发敏感值）；Gradio 三页前端（对话 SSE 流式+表格+SQL 折叠+引用 / 审批可操作 / 日报 Plotly 三图）。
- **数字**：SDK 0.2.0 三序列单测绿（429→重试→成功）；**MCP dogfooding：kb 域自己的 MCPClient 调通平台 MCP server 三只读工具**（自产自销，6 用例）；webhook 7 用例绿；前端三页浏览器可演示 + `_selftest.py` 12/12；BI 三图真数据（GMV 36,738,101.8 / TOP5 5 行 / SQL 可回溯）。

> **翻车细节**：MCP stdio 模式 stdout 是 JSON-RPC 协议通道——审计日志默认 print 到 stdout 导致 client 报 "Failed to parse JSONRPC message"；给 AuditLogger 加 `echo=False` 开关后修复。这种"协议通道被日志污染"的坑只有双侧都做过（server+client）才会踩到。

#### 5. 规模化演进：多租户分片 + 读写分离 + 自研 Runtime 边界（W28）

- **问题**：租户隔离只有 payload 过滤（单 collection，规模化路径未验证）、BM25 路无租户过滤（跨租户泄露风险）；单库读写未分；框架依赖无退出路径。
- **动作**：crc32 路由 **4 collection 分片**（分片=性能隔离、payload filter=正确性兜底双保险）+ BM25 租户过滤补丁 + 幂等迁移脚本（12 租户铺满 4 分片实测）；**DbRouter 读写分离开关**（无副本时零行为差异）；**自研 Runtime 内核 PoC**（图节点循环 + 原生 tool-calling 循环双形态，data 图同构对照）。
- **数字**：4 分片迁移分布 2386 点全铺满；BM25 双租户语料零交集；verify_isolation 三件套过；RO 路由单测 6 用例绿；**Runtime PoC 18 用例绿**（含 LangGraph 同输入同输出对照）；ADR-009/010/011/012 四份入库。

> **翻车细节**：crc32 对少量租户会分布倾斜——不是修 bug 而是如实记录并接受（分片数>租户数时空分片零成本）；演示数据补足 12 租户验证铺满。工程取舍比完美主义更值钱。

#### 6. 质量与成本（二期收官）

- **问题**：覆盖率 56%（目标 75%）；混沌演练五连无完整复验证据。
- **动作**：三期补测（reranker/real_provider/otel/parser/answer_validator 等洼地逐块填平）+ 混沌五连复验（Redis 挂 fail-open 矩阵实测 + 杀实例 failover）。
- **数字**：覆盖率 **56%→66%→76%**（≥75% Gate）；全量回归 **344→737 passed**；ruff/mypy 0 error；混沌 Redis-down 行为矩阵实测通过；mock-first 纪律下月真实 LLM 成本 **约 ¥20**。

> **翻车细节**：覆盖率最后 1pp 别硬凑——删死代码有时比写测试快（用 `rg` 找无引用函数）；确实难测的纯网络壳（LLM/OTLP 真端点）列入"文档化接受"清单，不造假覆盖。

---

## 工作经历 / 项目经历补充

- `[公司/阶段]`：`[职责摘要]` ｜ `[年份]`
- `[若为求职转行]`：可用"26 周系统化训练营 + 4 个阶段项目"替代，强调**独立交付闭环能力**（数据 → 检索/链路 → 评测 → 部署 → 压测 → 演练 → 复盘）。

## 教育背景

- `[学校/专业/学位]` ｜ `[年份]`

---

## 附：STAR 四条主线（面试脱稿用）

| 主线 | 一句话 | 问题→动作→数字 |
|---|---|---|
| 性能修复线 | 40 并发 P95 未达标 → 池化+合并写+净环境归因 | 2087→714ms（30 并发正式口径）、40 并发如实归因 |
| 规模化演进线 | 单 collection → 4 分片；单库 → 读写分离开关；框架 → 自研 PoC | ADR-009/010/011 + 18 用例同构对照 |
| 学习资产回归线 | MCP 双侧、原生 tool-calling 双路径、记忆分层回流 | 3 只读工具 dogfooding、双循环内核、ADR-012 |
| 多 Agent 判断线 | 做过生产级 OW（w13）→ 评估 scm 单图更优 → 触发条件明确 | Send 并行 + validator 回退实物引用 |

---

## 附：二期数字卡（面试快速自查）

| 指标 | 一期（W26 末） | 二期（W28 末） | 来源 |
|---|---|---|---|
| 30 并发 P95 | 1269ms | **714ms**（净环境正式 Gate） | w27_report §三 |
| 40 并发 P95 | 2087ms | 1872~2095ms（净环境复验如实归因） | w27_report §三 |
| 覆盖率 | 56% | **76%**（≥75% Gate） | w28_report Day6 |
| 全量回归 | 344 passed | **737 passed** | w28_report Day6 |
| SDK | 0.1.0 | **0.2.0**（429 自动退避） | w27_report Day4 |
| 双实例会话 | ip_hash 粘滞 | **least_conn + Redis 会话互通** | w27_report Day2 |
| 租户隔离 | payload 过滤 | **4 collection 分片 + BM25 双保险** | w28_report Day4 |
| 前端 | 无 | **Gradio 三页 + BI 三图** | w28_report Day2/3 |
| 容器口径 | 未测 | **分差 ≤0.6pp（真 bge 入容器）** | w28_report Day1/6 |
| MCP | 无 server 侧 | **三只读工具 dogfooding 调通** | w28_report Day5 |
| Runtime | 依赖 LangGraph | **自研内核 PoC + 同构对照绿** | w28_report Day6 |
| ADR | — | **009/010/011/012 四份** | docs/adr/ |
