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

# 验证
docker ps --filter name=scm-mysql   # 应显示 healthy
make test                           # pytest 2/2 + /health 探活
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

## 五、非目标（scope 纪律，详见《06》第 5 节）

等保正式化、OCR/Whisper、钉钉企微 IM、LoRA、多 GPU、BI 图表引擎、桌面客户端、行业多场景定制——一律进二期 backlog。

## 六、CI

`.github/workflows/ci.yml`：push/PR → Python 3.12 → ruff → mypy → pytest（含 MySQL service container 连通性用例）→ coverage 上传。
