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
- 4 角色：`admin`(12 权限) / `operator`(7) / `analyst`(4) / `viewer`(2)
- 12 权限码：kb 3 + ops 4 + data 2 + admin 3
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
- `router.py`：`POST /api/data/query`（JWT + `data:nl2sql` 权限），响应透出 `table/sql/columns/rows/elapsed/rejected_reason`；
- `schema_linker.py`：表/列双语料 + bge-small 向量召回 Top-3 → 按相对分数裁剪注入（≥0.75×top1）；
  精简 DDL 注入（省略低价值列）+ few-shot 与召回表联动、按重叠度动态排序；
- `mock_sql.py`：mock 生成器（从评测集取 gold SQL，只测链路不算效果）。

**评测**（`backend/evals/` + `backend/scripts/`）：
- `nl2sql_eval_v1.jsonl`：90 条三层（单表 30 / join 40 / 聚合 20），固定 seed 保证 gold 结果稳定；
- `eval_nl2sql.py`：execution accuracy（**结果集比对非字符串比对**：类型归一 + 列子集/同义别名对齐 + 排序键统一）；
  `--ab` 模式跑 v1 vs v2 A/B（准确率 + prompt token 降幅）；
- `eval_link_recall.py`：召回准确率（gold 表 ⊆ Top-3，sqlglot 从 gold SQL 提取标注）；
- `gen_eval_set_v1.py`：评测集生成（含冗余 join 检测，gold SQL 全部可执行且非空）。

**验收数字**：召回准确率 1.000（90/90）；real A/B 50 条 v1 0.980 → v2 1.000，prompt token 降幅 53.5%。

```bash
make gen-eval             # 评测集 90 条
make eval-nl2sql          # execution accuracy（默认 mock 测链路；LLM_PROVIDER=real 测效果）
make eval-ab              # A/B 对比 v1 vs v2（token 降幅 ≥50% 验收）
make eval-link-recall     # Schema Linking 召回准确率（≥90% 验收）
make test-nl2sql-e2e      # NL2SQL e2e 链路测试
```

## 七、非目标（scope 纪律，详见《06》第 5 节）

等保正式化、OCR/Whisper、钉钉企微 IM、LoRA、多 GPU、BI 图表引擎、桌面客户端、行业多场景定制——一律进二期 backlog。

## 八、CI

`.github/workflows/ci.yml`：push/PR → Python 3.12 → ruff → mypy → **★ W24-D2 sql_validator 四道闸 + 攻击用例（纯 AST 无 DB，安全第一道闸）** → **platform alembic migrate + seed（幂等两遍）** → **biz init_biz_db（建库+只读账号）→ alembic migrate + seed + 校验和** → pytest（含 MySQL service container 连通性、种子/只读沙箱/executor 用例）→ coverage 上传。

> 教训（W24 Day1）：**不要用 volumes 把工作区子目录挂进 CI service 容器**——容器内 root 改写目录所有权，重跑时 checkout 清理工作区报 EACCES。建库/建用户改由 job 步骤显式执行 `scripts/init_biz_db.py`。
