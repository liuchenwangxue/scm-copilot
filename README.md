# SCM Copilot 供应链智能运营平台

> 阶段四 · 模块化单体生产平台 ｜ 计划文档见 `learning-outputs/每周计划/阶段四_SCM_Copilot/`
> 目标：整合 stage3 双项目（知识问答 RAG + 业务操作 Agent）为统一平台——**问知识 / 查数据 / 办业务**，统一账号、审计、调度、监控与开放 SDK，双实例无状态水平扩展。

## 一、快速开始（W23 Day1 环境）

```bash
cd F:\code\agent\learning-outputs\scm-copilot
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"        # 或按 pyproject.toml dependencies 安装

# 起 MySQL 8（宿主端口 13306，本机 3306 已被其他容器占用）
make up                        # docker compose -f deploy/docker-compose.yml up -d

# 建平台库 + 幂等种子（W23 Day2）
make migrate                        # alembic upgrade head（12 表从零可重放）
make seed                           # 4 角色 / 12 权限 / 3 租户 × 4 角色测试用户

# ★ W24 Day1：业务库 scm_biz（NL2SQL 靶场）——六表 + 万级固定 seed + 只读沙箱
make init-biz-db                    # 建 scm_biz 库 + nl2sql_ro 只读账号（幂等）
make migrate-biz                    # 独立 alembic_biz 链建 scm_biz 六表
make seed-biz                       # 固定 seed（suppliers 40/orders 10000/items ~35000...）
make check-biz                      # 行数 + 校验和（重放一致性）
python -X utf8 scripts/verify_biz_data.py   # 数据质量验证（金额勾稽/延迟率/状态分布）

# ★ W23 Day5：stage3 历史数据迁移（审批/反馈/审计/LangGraph 断点，幂等可重跑）
python scripts/migrate_sqlite_to_mysql.py    # 输出"4 表行数 + 校验和一致"

# ★ W24 Day2：NL2SQL 安全四道闸 + 只读沙箱执行器（纯代码 + 测试，无需额外服务）
make test-sql-validator    # 四道闸每闸 ≥5 单测 + 20 条攻击用例 20/20 拦截（无 DB）
make test-executor         # 沙箱执行器：3s 超时 / 行数上限 / 字节截断 / 只读拒绝（需 MySQL）

# 验证
docker ps --filter name=scm-mysql   # 应显示 healthy
make test                           # pytest（/health + seed 数据验证）
make check                          # ruff 0 error + mypy 0 error
```

## 二、技术栈

Python 3.12 · FastAPI · SQLAlchemy 2.0 async（asyncmy）+ Alembic · MySQL 8.0 · Redis 7 · Qdrant v1.19 · LangGraph 1.2.x · APScheduler 3.x · RQ · PyJWT + bcrypt · LangFuse 2.95 · Prometheus + Grafana · pytest / ruff / mypy / GitHub Actions · Docker Compose + nginx（least_conn）

## 三、仓库结构

```text
scm-copilot/
├── backend/            # FastAPI 模块化单体
│   ├── app/
│   │   ├── main.py     # 入口（lifespan 建 engine/session）+ /health
│   │   ├── platform/   # 平台基座：settings / auth / rbac / audit（W23 逐日建设）
│   │   └── domains/    # kb / ops / data 三域（W23-D4 起迁入）
│   ├── alembic/        # 版本化迁移（W23-D2 初始化）
│   ├── tests/          # 单测 + 集成
│   └── scripts/        # seed / 迁移脚本
├── deploy/             # docker-compose（mysql 等基础服务）
├── frontend/           # Vue3 对话端（W24 后按需）
├── sdk/                # scm-copilot-client（W25）
├── docs/               # 架构/部署文档
└── reports/            # 周报告与 metrics 日志
```

## 四、开发数据库连接（默认）

| 项 | 值 |
|---|---|
| 宿主端口 | `13306`（本机 3306 已被 `yudao-mysql` 占用） |
| 容器 | `scm-mysql`，数据卷 `scm_mysql_data`（命名卷，防 NTFS 权限问题） |
| 平台库 DSN | `mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4` |
| 业务库 DSN | `mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4`（W24 用） |
| TZ | `Asia/Shanghai`（时区 +8，测试已断言） |

> 复制 `.env.example` → `.env` 可覆盖 DSN/JWT/Redis 配置（`.env` 已被 .gitignore 保护）。

## 五、平台库 schema 与测试用户（W23 Day2）

**平台库 `scm_platform`**（Alembic 版本化，`make migrate` 从零可重放）：

| 分组 | 表 | 说明 |
|---|---|---|
| 三级模型 | `users` / `roles` / `permissions` / `role_permissions` / `user_roles` | 用户-角色-权限，权限码即接口权限 |
| 审计 | `audit_logs` | 全平台写操作审计（actor/event/trace_id） |
| 业务支撑 | `approvals` / `feedback` | 审批单（含 before/after diff JSON）、引用/SQL 纠错 |
| SDK | `api_keys` + `quota_usage` | 机器身份与配额记账（W25 用，本周只建表） |
| 调度 | `scheduler_job_runs` | 调度任务运行记录（W25 用，防重/可观测） |
| 会话 | `conversations` | 多轮会话历史 |

**种子数据（`make seed`，幂等连跑两遍一致）**：
- 4 角色：`admin`(13 权限) / `operator`(7) / `analyst`(4) / `viewer`(2)
- 13 权限码：kb 3 + ops 4 + data 2 + admin 4（★ W25 Day5 新增 `admin:apikey:manage`）
- 3 租户 × 4 角色测试用户：`<role>_<tenant>`，**明文密码统一 `Passw0rd!`**（仅开发环境，入库为 bcrypt 哈希）
  - 租户：`t_huadong` / `t_huabei` / `t_huanan`
  - 例：`admin_t_huadong` / `operator_t_huabei` / `analyst_t_huanan` / `viewer_t_huadong`

## 六、业务库 scm_biz 与只读沙箱（W24 Day1）

**业务库 `scm_biz`**（独立 Alembic 链 `alembic_biz.ini`，与平台库版本树完全隔离）：

| 表 | 规模 | 说明 |
|---|---|---|
| `suppliers` | 40 | 华东/华北/华南/西南各 10；评分 60–95 |
| `products` | 500 | SKU + 类目 8 种 + 单价 10–5000 |
| `orders` | 10,000 | `SO-YYYYMMDD-XXXX`；近 180 天分布（近 30 天加密 40%）；状态 draft5/paid20/shipped40/done30/cancelled5 |
| `order_items` | ~35,000 | 每单 2–5 行；`amount = quantity × unit_price` 与订单金额勾稽 |
| `shipments` | ~7,000 | 仅 shipped/done 有发货记录；延迟发货率 ~8%（daily_brief 原料） |
| `inventory` | 500 | 每商品一条；~15% 低库存（qty < safety_qty，评测用） |

**只读沙箱账号**（纵深防御第二道保险，第一道是 W24-D2 sqlglot 四道闸）：
- 用户 `nl2sql_ro` / 密码 `ro_pass_2026_dev`，**仅 `GRANT SELECT ON scm_biz.*`**
- 写操作被 MySQL 拒绝：`ERROR 1142 (42000) UPDATE command denied`
- 初始化脚本 `deploy/initdb/01_create_ro_user.sql`（compose 首次建卷自动执行）
  + `scripts/init_biz_db.py`（幂等，已有数据卷的环境 / CI 用：`make init-biz-db`）

## 六·五、NL2SQL 安全四道闸与沙箱执行器（W24 Day2）

**第一道防线 `sql_validator.py`**（确定性 AST 校验，不依赖模型"听话"）：

| 闸 | 规则 | 拦截示例 |
|---|---|---|
| 闸1 | 单语句（`;` 堆叠/换行注入 → `multi-statement`） | `SELECT 1; DROP TABLE orders` |
| 闸2 | 根节点仅 SELECT/UNION（含 UNION ALL）→ `not-select` | `UPDATE/DELETE/DROP/INSERT/...` |
| 闸3 | 子句级写操作（伪装嵌写 → `write-op`） | `SELECT (DELETE ...)`、`WITH x AS (DELETE ...)` |
| 闸4 | 危险函数黑名单（→ `dangerous-func`） | `sleep/benchmark/load_file/outfile`（含 `SlEeP`/`SLEEP/**/` 混淆） |
| 扩展 | FOR UPDATE 锁读（→ `for-update`）+ 表名白名单六表（→ `unknown-table`） | `... FOR UPDATE`、UNION 探测 `users`、跨库 `scm_platform.*` |

- 兜底：**强制 `LIMIT 200`**（无 LIMIT 才加，防全表扫描；30.x 对 Union 根节点用 `set("limit")`）
- 拒绝抛 `SQLRejected(reason)`，reason 机器可读落审计

**第二道防线 `executor.py`**（只读沙箱执行，与平台库 engine 隔离）：
- `nl2sql_ro` 独立连接池；3s 超时（`asyncio.timeout`）；行数上限 200；结果集 >1MB 截断
- 类型规范化：`Decimal→float`、`datetime→isoformat`（评测脚本可直接序列化）
- 审计回调钩子：执行成功/失败/超时都发事件（含 SQL 原文），由调用方写入 `audit_logs`

> 纵深防御叙事：即使四道闸有未知绕过，MySQL 权限层兜底拒绝写操作（Day1 已实测 `ERROR 1142`）。

## 六·六、NL2SQL 生成链路与 Schema Linking（W24 Day3–Day4）

**生成链路**（`app/domains/data/`）：
- `prompts.py`：v1 全 schema 注入 / v2 Schema Linking 召回注入，`PROMPT_VERSION=v1|v2` 环境变量切换；
  时间窗口径由 `today` 驱动生成显式日期（评测传 `BASE_DATE=2026-08-18` 固定 seed 基准，防运行日漂移）；
- `graph.py`：LangGraph 子图 `generate→validate→execute→format`，SQL 被拒时条件路由到降级话术；
- `router.py`：`POST /api/v1/data/query`（JWT + `data:nl2sql` 权限），响应透出 `table/sql/columns/rows/elapsed/rejected_reason`；
- `service.py`：`run_nl2sql_query` 统一编排（多轮消解→子图→洞察），router 与对话入口复用；
- `schema_linker.py`：表/列双语料 + bge-small 向量召回 Top-3 → 按相对分数裁剪注入（≥0.75×top1）；
  精简 DDL 注入（省略低价值列）+ few-shot 与召回表联动、按重叠度动态排序；
- `insight.py`：结果洞察（≤3 条，prompt 给结果集 JSON + **数字溯源校验**双保险，禁止编造数字）；
- `mock_sql.py`：mock 生成器（从评测集取 gold SQL，只测链路不算效果）。

**评测**（`backend/evals/` + `backend/scripts/`）：
- `nl2sql_eval_v1.jsonl`：100 条三层（单表 40 / join 40 / 聚合 20），固定 seed 保证 gold 结果稳定；
- `eval_nl2sql.py`：execution accuracy（**结果集比对非字符串比对**：类型归一 + 列子集/同义别名对齐 + 整行排序键）；
  `--ab` 模式跑 v1 vs v2 A/B（准确率 + prompt token 降幅）；`--prompt-version v2` 单版本全量；
- `eval_link_recall.py`：召回准确率（gold 表 ⊆ Top-3，sqlglot 从 gold SQL 提取标注）；
- `gen_eval_set_v1.py`：评测集生成（含冗余 join 检测，gold SQL 全部可执行且非空）；
- `recompute_eval.py`：比对逻辑修复后复用报告中的 gen SQL 重算（省 token，快速验证）。

**验收数字**（Day6 real 全量 100 条，kimi-k2.7-code）：**整体 0.970**（单表 0.975 / join 0.950 / 聚合 1.000）；
召回准确率 1.000（100/100）；token 降幅 53.3%；P95 38.9ms；攻击 20/20。

```bash
make gen-eval             # 评测集 100 条
make eval-nl2sql          # execution accuracy（默认 mock 测链路；LLM_PROVIDER=real 测效果）
make eval-ab              # A/B 对比 v1 vs v2（token 降幅 ≥50% 验收）
make eval-link-recall     # Schema Linking 召回准确率（≥90% 验收）
make eval-day6            # Day6 real 全量 100 条（v2，含 P95）
make test-nl2sql-e2e      # NL2SQL e2e 链路测试
```

## 六·七、呈现链路与语义路由 data 分支（W24 Day6）

**结果洞察 `insight.py`**（禁止编造数字双保险）：
- prompt 给结果集 JSON（前 10 行）+"只允许引用结果集中的数值"硬性规则；
- 输出后 `verify_insight_digits` **确定性数字溯源**：摘要中每个数字必须能在结果集数值单元格中找到
  （支持 %/万/千/亿 量纲换算 + 1% 容差），查无出处的整条丢弃；
- 业务字符串中的数字可溯源（供应商名"华东宏图44"里的 44 合法）、日期字符串不溯源（防编造撞上）。

**对话入口 data 分支**：`/api/v1/kb/chat` 语义路由新增 `data` 类目（查数→NL2SQL 域）：
- 规则层 4 组高置信组合模式（延迟/发货+多少、近N天+订单+多少、各区域+订单/库存、TOP N+供应商）+ 样本 kNN；
- data 分支权限二次校验（`data:nl2sql`，viewer 无权限礼貌拒答）→ `run_nl2sql_query` → SSE `data_table` 事件
  （columns/rows/sql/insights/elapsed，前端表格 + SQL 折叠面板 + 洞察 + 反馈按钮的数据源）。

## 六·八、调度域与六任务（W25 Day1）

**调度基座 `app/platform/scheduler/`**（APScheduler 3.x 稳定线，锁死 <4）：

| 组件 | 说明 |
|---|---|
| `leader.py` | ★ 任务级互斥装饰器：`SET lock:job:{name} NX EX 300` + owner 校验 Lua 释放；Redis 挂 → fail-open 放行（任务幂等兜底） |
| `__init__.py` | `AsyncIOScheduler` + MySQL job store（`scm_platform.apscheduler_jobs` 内建表）→ 重启任务定义不丢；六任务集中注册表 + `misfire_grace_time=300` + `coalesce=True`（错过补跑合并）；`_run_job` 模块级入口（★ MySQL job store pickle 要求回调可序列化，闭包会炸） |
| `jobs/` | 六任务：kb_increment_sync `*/5` / vector_cleanup `0 3` / audit_archive `0 4 1` / daily_brief `0 8 1-5` / eval_nightly `0 2` / cache_warmup `0 7`（Day2 前三个 + Day3 后三个，**六任务全部实现**） |

- **双实例防重**：调度器全实例跑（高可用）+ 任务级互斥（leader 锁，未抢到记 `skipped`）+ 任务幂等键双保险；
  每次执行写 `scheduler_job_runs`（running → success/failed/skipped + instance + duration_ms）——24h 零重复观测依据
- **lifespan 集成**：startup `scheduler.start()` / shutdown `wait=False` 优雅停；启动失败 fail-open 降级（不阻塞主服务）；
  `/health` 返回 `scheduler: running|off`
- 测试：`make test-scheduler`（leader 锁互斥纯逻辑 + job_runs 落库/重启持久性 integration，需 MySQL）
- 手动触发：`PlatformScheduler.trigger(job_name)`（独立一次性 job，不覆盖原 cron——reschedule 会吞掉 cron 定义的坑）

**数据闭环三任务（★ W25 Day2）**：

| 任务 | 实现要点 |
|---|---|
| `kb_increment_sync` `*/5` | docs 目录 vs `docs` 表（DocMeta）三集合扫描（new/变更/删除）；point id = `uuid5(内容)` 内容寻址幂等 upsert；变更/删除按 payload `source_doc_id` 过滤删向量（非 point id）；水位 `kb:sync:last_ts` 存 Redis，成功推进/失败不推进；collection 检测到 stage3 旧格式自动重建 |
| `vector_cleanup` `0 3` | scroll 全量 → payload `source_doc_id` 不在 docs 表（active）即孤儿向量 → 过滤删除；语义缓存 `scm:semcache:*:keys` 过期成员（TTL 漏网 + version 失效标记）清理 |
| `audit_archive` `0 4 1` | 上月 `audit_logs` → `audit_logs_YYYYmm`（CTAS + 行数校验 + 删主表）；归档表存在即幂等跳过；Redis 批次锁仅防两段间并发（完成释放，失败不残留） |

**业务与守护三任务（★ W25 Day3）**：

| 任务 | 实现要点 |
|---|---|
| `daily_brief` `0 8 1-5` | 三条固定模板问题走 W24 NL2SQL 完整链路（四道闸 + 只读沙箱，mock 注册固定 SQL）→ 模板渲染（数字点开即 SQL 可回溯）→ `daily_briefs` 表（brief_date unique）+ 订阅用户站内通知（`notifications`，analyst/admin 前 3）；幂等：`brief:{date}` Redis SETNX（失败删键可重试）+ DB unique 双保险；昨日口径写死 `CURDATE() - INTERVAL 1 DAY`（跨月/年交给 MySQL） |
| `eval_nightly` `0 2` | RAG 156 条（生产同款 HybridRetriever）+ NL2SQL 100 条（W24 评测逻辑）全 mock 守护"结构"（格式/延迟/报错率，非语义准确率）→ `eval_reports`（(report_date, domain) unique 幂等 + 7 日均值偏离 >5pp 标红）；逐条容错 error_rate 进指标 |
| `cache_warmup` `0 7` | `conversations` 昨日标题频次 TOP100 → 生产同款检索 + mock 生成 → 语义缓存预写（已命中跳过）；`{candidates/hit/warmed/failed}` 可观测 |

**调度面板 API**（`app/domains/admin/scheduler_api.py`，权限 `admin:scheduler:manage`）：
- `GET /api/v1/admin/scheduler/jobs`：六任务 cron/desc/enabled/next_run + `last_run` + `recent_runs`（最近 5 条运行历史）
- `POST /api/v1/admin/scheduler/jobs/{name}/trigger`：手动触发（独立一次性 job）+ 审计 `admin.scheduler.trigger`
- scheduler 未启用 → 503（CI/单测环境属预期）

**部署配置要点（★ W25 Day3 实测踩坑）**：
- backend 容器必须设 `TZ: Asia/Shanghai`——否则 Python `date.today()` 走 UTC，daily_brief/eval_nightly 归属日少一天（实测容器把 8/19 日报写成 8/18）
- 实例标识环境变量是 `SCM_INSTANCE_ID`（settings 前缀 SCM_），compose 旧值 `INSTANCE_ID` 读不到 → 双实例 job_runs 的 instance 全是 `local`，零重复观测无法区分实例

```bash
make test-scheduler    # 调度基座测试（需 MySQL，Redis 可选）
make test-kb-sync      # 数据闭环三任务 + 面板 API（Day2；纯逻辑 CI 可跑，全流程需 MySQL）
make test-day3-tasks   # 日报/夜间回归/预热三任务（Day3；纯逻辑 CI 可跑，全流程需 MySQL+Redis）
make kb-sync-smoke     # kb_increment_sync 真实环境验收（隔离 collection，不碰正式数据）
```

## 六·九、开放生态：OpenAPI / SDK / API Key（W25 Day4–Day5）

**OpenAPI 规范化（★ W25 Day4）**：`/api/v1` 版本化 + 三分组 tag（auth/kb/ops/data/admin）+ 统一错误码 `Err{code,message,trace_id}`（`app/platform/errors.py` 单一事实来源）+ 响应模型 100% 注解 + `openapi-spec-validator` 契约校验（`make test-openapi`）。

**API Key 机器身份（★ W25 Day5）**：`app/platform/apikeys.py`
- `sk-` 前缀 + secrets 生成（192bit 熵），**sha256 哈希落库**（高频路径不用 bcrypt——与密码策略分开的权衡），明文只在创建时返回一次
- 双轨认证 `api_key_or_jwt`：Bearer `sk-` → API Key（动态加载 owner 用户权限），否则 JWT（静态 claims）——集成方无登录态也能调受保护端点
- **Redis 令牌桶限速**（Lua 原子，容量 10 / 速率 5/min）→ 超额 `429 + Retry-After`；Redis 挂 → fail-open 放行（配额是软约束）
- 管理 API：`POST/GET /api/v1/admin/apikeys` + `DELETE .../{id}`（吊销软删除保审计，权限 `admin:apikey:manage`）

**官方 SDK `sdk/scm-copilot-client`（★ W25 Day5）**：薄封装 httpx 零重依赖（ADR-08：三接口手写可控，端点增后再切 openapi-python-client）
```python
from scm_client import ScmCopilot
client = ScmCopilot("http://localhost:8000", api_key="sk-xxx")
for event in client.chat_stream("供应商准入需要哪些资质？"):
    print(event.delta, end="", flush=True)          # SSE 流式（四型事件迭代器）
result = client.nl2sql("近30天延迟发货 TOP5 供应商", as_dataframe=True)
print(result.sql)                                    # SQL 100% 透出可审计
pending = client.approvals.list_pending()            # 审批（含 HITL 恢复上下文）
client.approvals.decide(pending[0].approval_id, "approve", session_id=pending[0].session_id)
```
- 审批列表数据源：`GET /api/v1/ops/approvals`（★ W25 Day5 新增，含 session_id 断点恢复上下文）
- 错误映射：`ScmAuthError`(401/403) / `ScmQuotaError`(429+Retry-After) / `ScmError`（对齐平台 Err 契约）
- 发布：`cd sdk && python -m build && twine upload --repository testpypi dist/*`（备选名 `scm-copilot-client-dev`）

```bash
make test-apikeys        # API Key + 令牌桶 + 审批列表（纯逻辑 CI 可跑；集成需 MySQL+Redis）
make test-sdk-unit       # SDK 单元测试（MockTransport 离线可跑）
make test-sdk-integration  # SDK 集成测试（需真实平台：SCM_SDK_BASE_URL 默认 http://localhost:8000）
```

## 六·十、三吸收项：Hooks / 基础监控 / TLS（W25 Day6）

**工具调用 Hooks**（`app/platform/hooks.py`，learn-claude-code s04 机制的实物落点）：
- `PreToolUse`：参数校验（ToolSpec 契约 required）+ 高危标记 + 审计埋点（`tool_pre_use`，before 状态）
- `PostToolUse`：结果审计（`tool_post_use`：after + 耗时 + 熔断状态）+ 语义缓存失效（写类工具 update/cancel 命中后失效同源 query 缓存）
- ops 域 4 工具全接 `execute_node`；`approval_gate` 复用 `make_after_state` 的 before/after diff（单一来源防漂移）
- 钩子抛错记日志放行（横切关注点故障不影响工具调用——ADR 修订记录，见 `reports/w25_report.md`）
- 测试：`make test-hooks`（17 用例，纯逻辑 CI 可跑）

**基础监控**（`deploy/prometheus.yml` + compose 4 服务）：
- `node-exporter`（宿主机/VM 指标）+ `cAdvisor`（容器指标，Windows 下仅 linux 容器有效）+ Prometheus（三组抓取：backend 应用 /metrics + node + cAdvisor）+ Grafana（预置 Prometheus 数据源 + `SCM Platform 核心指标` 面板）
- `/metrics` 端点：`MetricsMiddleware` 自动记录 QPS/P95/成功率/in-flight（`app/main.py` 白名单挂载）
- 验证：`make monitor` → Prometheus targets 全 UP（http://localhost:19090/targets）→ Grafana（http://localhost:13001，admin/admin123）面板有数据

**本地 TLS**（mkcert）：
- `make tls`：`mkcert -install`（本地根 CA）+ `mkcert localhost scm.local` → `deploy/nginx/certs/`
- nginx：80 → 301 → 443（`http2 on` + `X-Forwarded-Proto` + `proxy_buffering off` SSE 保持）→ https://localhost:18443
- 生产换正式证书（Let's Encrypt / 公司 CA）见 `docs/deploy.md`（证书与配置解耦：换文件不换配置）

```bash
make test-hooks   # 工具钩子测试（纯逻辑 CI 可跑）
make tls          # mkcert 本地 TLS 证书
make monitor      # 起监控栈（node-exporter/cadvisor/prometheus/grafana）
```

## 七、非目标（scope 纪律，详见《06》第 5 节）

等保正式化、OCR/Whisper、钉钉企微 IM、LoRA、多 GPU、BI 图表引擎、桌面客户端、行业多场景定制——一律进二期 backlog。

## 八、CI

`.github/workflows/ci.yml`：push/PR → Python 3.12 → ruff → mypy → **★ W24-D2 sql_validator 四道闸 + 攻击用例（纯 AST 无 DB，安全第一道闸）** → **★ W25-D6 hooks sanity（Pre/PostToolUse 钩子纯逻辑）** → **platform alembic migrate + seed（幂等两遍）** → **biz init_biz_db（建库+只读账号）→ alembic migrate + seed + 校验和** → pytest（含 MySQL service container 连通性、种子/只读沙箱/executor 用例）→ coverage 上传。

**★ W25 Day5 新增 `sdk` job**：装包（`pip install -e ".[dev]"` + `pip install -e ./sdk`）→ migrate + seed → 后台起 uvicorn（`SCM_SCHEDULER_ENABLED=0`）→ SDK 单元测试（离线）→ SDK 集成测试（干净环境真实调平台，十行脚本 + 吊销 401；429 用例在无 Redis 的 CI 自动 skip，本地部署环境真跑）。

> 教训（W24 Day1）：**不要用 volumes 把工作区子目录挂进 CI service 容器**——容器内 root 改写目录所有权，重跑时 checkout 清理工作区报 EACCES。建库/建用户改由 job 步骤显式执行 `scripts/init_biz_db.py`。
