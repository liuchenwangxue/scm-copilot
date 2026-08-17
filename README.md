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

# ★ W23 Day5：stage3 历史数据迁移（审批/反馈/审计/LangGraph 断点，幂等可重跑）
python scripts/migrate_sqlite_to_mysql.py    # 输出"4 表行数 + 校验和一致"

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

## 六、非目标（scope 纪律，详见《06》第 5 节）

等保正式化、OCR/Whisper、钉钉企微 IM、LoRA、多 GPU、BI 图表引擎、桌面客户端、行业多场景定制——一律进二期 backlog。

## 七、CI

`.github/workflows/ci.yml`：push/PR → Python 3.12 → ruff → mypy → **alembic migrate + seed（幂等两遍）** → pytest（含 MySQL service container 连通性与种子数据用例）→ coverage 上传。
