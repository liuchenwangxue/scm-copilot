# 04 · 架构决策记录（ADR）与风险应对

> 使用场景：开发中每次"要不要换个做法"时先查这里；面试前每条 ADR 展开练 3 分钟（决策 → 备选 → 放弃原因 → 演进路径）。
> 01 执行计划中有 8 条速览版；本篇是**详版唯一权威来源**。

---

## 1. 八条 ADR 详版

### ADR-01 · 模块化单体，而非微服务

- **决策**：单 FastAPI 应用按域划分模块（kb / ops / data + platform 基座），域间只经内部 API 通信。
- **理由**：域间强关联（知识域引用操作域数据语义）；单机资源约束；部署运维成本；调试链路短。
- **备选与放弃原因**：微服务——团队/规模未到，过早拆分徒增复杂度。
- **演进路径**：域模块已按 API 边界隔离（不跨域 import 内部模块），需要时可按域直接拆出服务。拆分信号：①某域负载特征显著不同（如 NL2SQL 计算密集需独立扩缩容）②团队分工需要独立发布节奏 ③单库写入成为瓶颈。

### ADR-02 · MySQL 8 替代 SQLite 作权威库

- **决策**：`scm_platform` + `scm_biz` 两库，SQLAlchemy 2.0 async（asyncmy 驱动）+ Alembic 版本化。
- **理由**：并发写、行级锁、连接池；审批/审计等高并发写场景 SQLite 有锁瓶颈。
- **备选与放弃原因**：继续 SQLite——单写者锁在双实例下直接不可行。
- **演进路径**：SQLite 保留为 Redis 故障时 fail-open 降级目标（沿用 B 项目模式）。驱动选 `asyncmy`（aiomysql 维护弱，不选）。

### ADR-03 · 保留 Qdrant + BM25，不迁 Milvus/ES

- **决策**：向量检索沿用 Qdrant，混合检索 BM25(jieba)+向量 + RRF(k=60)。
- **理由**：W5 压测 432 QPS / P95 27ms 达标；单容器运维轻；HNSW 参数可控（做过实验，还发现 full_scan_threshold 默认值使小数据 HNSW 静默失效的坑）。
- **备选与放弃原因**：Milvus 集群（etcd+多角色）运维成本远超需求；ES 引入 JVM 栈不值。
- **演进路径**：数据量超千万或需分布式分片再评估迁移。面试话术："选型看场景与运维成本，不是越重越好。"

### ADR-04 · ip_hash → least_conn

- **决策**：nginx 负载均衡换 least_conn，前提是状态全外置。
- **理由**：状态外置后粘滞路由无意义且有害——同一 IP 长连接会压死单个实例，负载不均。
- **前提核销清单**：见 [03 第 4 节](03_核心技术方案.md)（会话/审批/缓存/锁/checkpointer 各自归宿）。
- **回退条件**：若任一状态外置不完全则回退 ip_hash。

### ADR-05 · APScheduler + Redis 锁，而非 Celery beat

- **决策**：APScheduler 3.x + MySQL job store + Redis leader 锁。
- **理由**：已有 RQ 承担异步任务；APScheduler 与 FastAPI 同进程生命周期简单；任务级互斥已够。
- **备选与放弃原因**：Celery beat——引入新 broker 语义与运维面。
- **演进路径**：任务量大/需分布式分片时再评估。

### ADR-06 · checkpointer 用 AsyncMySQLSaver

- **决策**：LangGraph 断点持久化进 MySQL。
- **理由**：复用主库与连接池；HITL 断点恢复持久化与业务库同源，便于事务一致性。
- **备选与放弃原因**：Redis checkpointer——恢复快但持久化弱；MySQL 足够。

### ADR-07 · sqlglot AST 校验，而非正则/关键词过滤

- **决策**：生成 SQL 必过四道闸（单语句 / 仅 SELECT·UNION / 子句级写拦截 / 危险函数黑名单 + 强制 LIMIT）。
- **理由**：正则可被注释、编码、嵌套绕过；AST 是确定性解析，子句级拦截无逃逸。
- **纵深防御**：校验层之外再配数据库层 `nl2sql_ro` 只读账号 + 3s 超时 + 行数上限——即使校验有漏，权限层兜底。
- **代码**：见 [03 第 1.1 节](03_核心技术方案.md)。

### ADR-08 · SDK 薄封装 httpx，而非 OpenAPI 代码生成

- **决策**：`scm-copilot-client` 手写三接口（chat_stream / nl2sql / approvals）。
- **理由**：三接口规模手写更可控；SSE 迭代器体验优于生成代码。
- **演进路径**：端点增长后可切 openapi-python-client 自动生成，OpenAPI 3.1 规范已就绪。

---

## 2. 风险与降级详版

| # | 风险 | 概率 | 影响 | 缓解 / 降级 |
|---|---|---|---|---|
| R1 | **NL2SQL 准确率不达标**（整体 <0.70） | 中 | 高 | D12 检查点：①限定 10 张高频白名单表做强化 few-shot ②schema linking 召回阈值调优 ③仍不达标如实记录分层指标 + 改进路线（面试讲边界反而真实）。**禁止刷数据/造假** |
| R2 | **第 3 周倦怠**（脱产特有，概率最高） | **高** | 高 | 19:00 硬止损；周日下午强制休息；连续 2 天同一问题卡住 → 强制换任务或写下来 AI 结对求助。**节奏纪律 > 进度** |
| R3 | **周 Gate 延期累积** | 中 | 中 | 欠账清单制：当日 Gate 未过记入，次日优先清欠；连续欠 2 天则砍 D18 吸收项保主线（Hook 优先保留，监控/TLS 可弃）；优先砍多轮追问（保单轮准确率），**不砍安全与回归** |
| R4 | **单日过度优化** | 中 | 中 | 每任务先过 Gate 再优化；"优化想法"记 backlog 不当场做 |
| R5 | **单机资源不足**（双实例+全家桶） | 中 | 中 | 容器内存限额 + D20 实测；不足则压测降为 30 并发如实记录；embedding/reranker 保持 CPU torch（沿用 W20 决策） |
| R6 | **scope creep**（又想加 BI/多模态/IM） | **高** | 中 | 非目标清单贴墙上（见 [06 第 5 节](06_资产盘点与方向决策.md)）；每周日复盘对照差距清单——新想法一律进二期 backlog 不进当周 |
| R7 | **真实 LLM 超预算** | 低 | 中 | 开发期全走 mock 路径（A 项目双路径基建已就绪）；real 全量跑仅 D12/D21 两次；采样用模型池免费额度优先（glm→kimi→qwen）；夜间评测用 mock 断言结构 |

### mock-first 原则（贯穿四周）

LLM 抽象层 `LLM_PROVIDER=mock|real`、契约级 mock_biz_server、评测集固定 seed、无 Key 自动降级——这套已验证的组合拳继续作为默认开发模式，**真实 API 只在指标采样与验收时启用**（预算：100 条 × 2 轮 ≈ ¥10–20 + 日报演示 + 录屏，总 ≤¥100）。

---

## 3. 决策记录的维护纪律

- 每条 ADR 若在执行中被推翻（如 D6 发现某状态无法外置），**不删原文，追加"修订记录"**：日期 + 原因 + 新决策——面试时"决策演变过程"比"一次性正确"更有说服力
- 周 Gate 延期、砍项、降级预案触发都记入本篇末尾的执行日志
- 二期 backlog（Runtime 内核 / BI 图层 / IM / Agent 中台演进）统一挂在本篇，面试讲"已规划未实施"的演进路径

### 修订记录（执行中追加）

- **ADR 修订-1（W25 Day6）· 钩子故障处理 = 放行**：`hooks.py`（Pre/PostToolUse 工具钩子）回调抛错 try/except 记日志放行，不让工具调用失败（横切关注点故障隔离，手册坑落地）。若未来要"钩子阻断业务"，需显式配置 allow/deny 层（对齐 s04 的 deny/ask 不变式），本版不做——在 `w25_report.md` §六 有记录。
- **ADR 修订-2（W25 Day6）· SDK 增加 `verify` 参数**：`ScmCopilot(..., verify=True)` 默认 True（生产安全）；本地 mkcert TLS 平台 / 企业内网自签 CA 用 `SCM_SDK_VERIFY=0` 或显式 `verify=False`。默认行为不变，纯增量能力。
- **ADR 修订-3（W26 Day2）· 认证存储依赖 = fail-open**（故障演练一杀 MySQL 暴露）：`auth.get_current_user` 的吊销名单/用户存活查库从"强依赖"调整为 **DB 挂时 fail-open 信任 JWT claims**——签名校验（本地 HS256）是安全边界（篡改 token 照常 401，安全收益不降），查库是增强项（DB 挂时信任已签发 claims，恢复自动回查）。login/审批端点 DB 异常 → 503 明确提示。避免"存储故障拖垮全部请求"的单点依赖。实现见 `backend/app/platform/auth.py`、`ops/router.py`。
- **ADR 修订-4（W26 Day2）· LLM 降级链守接口契约**（演练四 LLM 全超时暴露）：`RealLLMProvider._degrade_or_raise` 按 tag 分派——`generate_json` 降级返回 mock 结构化 dict（含 `degraded=True` 标记），`generate`/`stream` 返回 `[WARNING]` 前缀文本。**"降级是响应语义，不是类型破坏"**——降级路径也必须守住调用方契约。实现见 `backend/app/shared/llm/real_provider.py`。

---

## 4. 执行日志（W23–W26 过程决策与降级预案触发）

| 日期 | 事件 | 记录 |
|---|---|---|
| W23-D6 | 40 并发 P95 超限（W23 首次出现） | 与 R5 同源：AsyncMySaver 单连接串行 + 本机 Docker 共享 IO；正式口径取 30 并发（P95 1.3s）达标——沿用至今 |
| W25-D1 | 调度基座选型确认 | APScheduler + MySQL job store + Redis leader 锁（ADR-05 落地，未触发变更） |
| W26-D1 | eval_nightly 容器内路径 bug | `pip install .` 使 `Path(__file__).parents[4]` 解析到 site-packages，夜间评测一直快速失败假成功——新增 `_find_eval_dir()` 多候选探测，修复后容器内首次跑通 100 条 mock 评测 |
| W26-D2 | 故障演练五连 + 3 处修复 | 认证 fail-open（修订-3）/ BM25-only 降级（检索）/ generate_json 契约（修订-4）；新增 8 测试 |
| W26-D3 | 验收口径固化 | 18 项指标 17 达成 + 1 如实标注（40 并发 P95，R5 降级路径）；覆盖率 56% 未达 75% 如实记录 |
| W26-D4 | Makefile migrate 路径 bug | `PY := .venv/Scripts/python.exe` 相对路径随 `cd backend` 解析失败 → 改 `$(CURDIR)` 绝对路径；一键起 from-scratch 14/14 |
| W26-D6 | **项目冻结（终验 Gate 7/7）** | 终验 Gate 7/7；此后只修 bug 不加功能（R6 纪律延续到面试期）。**注：git tag v1.0.0 实查未打、未推送 remote——由用户自行补打（D7 已核实）** |
| W26-D7 | **四周总复盘 + 投递准备（阶段四收官）** | `reports/w26_final_retro.md`：对比 stage3 四项进化（Hit@1 0.9038 保持 / NL2SQL 0.970 / 双实例 40 并发 100% / 用例 344）＋三段式（做得好/踩坑/若重来）＋四项决策回看（预算实花 ¥19.95 / ¥100 内）；累计欠账 = 0；9/14 启动投递 |

## 5. 二期 backlog（已规划未实施，面试演进路径弹药）

> 纪律：面试期只修 bug；B1/B2 为可选加分项；其余待 offer 后评估。每条都有"动机 → 触发 → 演进路径"可讲。

| # | 项 | 动机 / 触发 | 优先级 |
|---|---|---|---|
| B1 | AsyncMySaver 连接池化（或每请求独立连接） | 40 并发 P95=2.09s 根因（ops_query P95 3.4s） | P1 |
| B2 | 覆盖率 ≥75%：real_provider 降级链分支 mock httpx 测试 + reranker 逻辑分支 | 当前 56%，估 +8–10pp（acceptance_final §一改进路线） | P1 |
| B3 | W19 遗留独立脚本清理（ops_day3_tools_test / ops_day4_approval_test） | 签名已过时，覆盖由 pytest + SDK 集成替代 | P2 |
| B4 | BI 图层（报表可视化 / 图表引擎） | 面试期轻量刷新话题选项（01 §五） | P2 |
| B5 | Runtime 内核（自研 agent loop 解耦 LangGraph） | harness 能力沉淀（learn-claude-code s01–s20） | P3 |
| B6 | IM 集成（钉钉 / 企微审批推送） | 审批通知真实场景 | P3 |
| B7 | 多租户规模化：租户哈希分片（Qdrant）/ 分库分表（MySQL） | 05 §6.1 多租户话术，按真实负载特征再拆 | P3 |
| B8 | 单库拆分（data/ops 独立库或读写分离） | ADR-01 拆分信号③（数据量 100 倍先崩点） | P3 |
