# ★ W23 SCM Copilot 平台 Makefile（Day1 建仓 + Day2 seed + Day3 认证 + Day4 双域并入
#   + Day5 数据迁移 + Day6 无状态双实例）
# 用法：
#   make up-mysql   只起 MySQL+Redis（本地迁移/seed 用）
#   make up         起全栈（mysql/redis/mock-biz/backend-a1/a2/nginx）
#   make down       停全栈
#   make build      构建 backend + mock-biz 镜像
#   make migrate    alembic upgrade head（含 Day3 token_blacklist）
#   make seed       平台库幂等种子（4 角色/12 权限/12 用户）
#   make test       pytest 平台单测 + 双域回归 + coverage
#   make test-auth  仅跑认证/RBAC（Day3，需 MySQL 已起 + seed）
#   make test-integration  双域集成回归脚本（审批/幂等/工具/validator，需 mock_biz）
#   make check     ruff lint + mypy（0 error 才算过）
#   make loadtest   40 并发 × 200 压测（打 nginx :18000）
#   make drill      压测中段杀 backend-a1 演练（5xx=0 验证）
PY := .venv/Scripts/python.exe
RUFF := $(PY) -m ruff
MYPY := $(PY) -m mypy
PYTEST := $(PY) -m pytest
COMPOSE := docker compose -f deploy/docker-compose.yml
# alembic.ini 在 backend/ 下，须在此目录运行（env.py 经 pythonpath 兜底）
BACKEND := backend

.PHONY: up up-mysql down build migrate seed test test-auth test-integration check lint-fix format loadtest drill help

## 默认：显示帮助
help:
	@echo "目标:"
	@echo "  make up-mysql  只起 MySQL+Redis（本地迁移/seed 用）"
	@echo "  make up        起全栈（mysql/redis/mock-biz/backend-a1/a2/nginx）"
	@echo "  make down      停全栈"
	@echo "  make build     构建 backend + mock-biz 镜像"
	@echo "  make migrate   alembic upgrade head"
	@echo "  make seed      平台库幂等种子"
	@echo "  make test      pytest backend/tests + coverage"
	@echo "  make test-auth 仅跑认证/RBAC"
	@echo "  make test-integration  集成回归"
	@echo "  make check     ruff lint + mypy（0 error）"
	@echo "  make loadtest  40 并发 × 200 压测（nginx :18000）"
	@echo "  make drill     压测中段杀 backend-a1 演练（5xx=0）"

## 只起 MySQL+Redis（本地迁移/seed/单测用）
up-mysql:
	$(COMPOSE) up -d mysql redis

## 起全栈（W23 Day6：双实例 + nginx）
up:
	$(COMPOSE) up -d --build

## 停全栈
down:
	$(COMPOSE) down

## 构建 backend + mock-biz 镜像（Day6）
build:
	$(COMPOSE) build backend-a1 mock-biz

## 迁移数据库（须在 backend/ 下运行，alembic.ini 在此）
migrate:
	cd $(BACKEND) && $(PY) -m alembic upgrade head

## 平台库幂等种子（4 角色/12 权限/映射/3 租户 × 4 角色用户）
seed:
	$(PY) scripts/seed_platform.py

## ★ W23 Day5：stage3 历史数据迁移（审批/反馈/审计/LangGraph 断点，幂等可重跑）
migrate-data:
	$(PY) scripts/migrate_sqlite_to_mysql.py

## pytest + coverage
test:
	$(PYTEST) backend/tests --cov=backend --cov-report=term-missing

## 仅跑认证/RBAC（Day3，需 MySQL 已起 + seed）
test-auth:
	$(PYTEST) backend/tests/test_auth.py backend/tests/test_rbac.py -v

## 双域集成回归（Day4 迁移自 stage3 的脚本；审批/幂等自动拉起 mock_biz）
test-integration:
	$(PY) -X utf8 scripts/ops_day4_approval_test.py
	$(PY) -X utf8 scripts/ops_day4_idempotency_test.py
	$(PY) -X utf8 scripts/ops_day3_tools_test.py
	$(PY) -X utf8 scripts/kb_day4_validator_test.py

## lint + 类型检查（CI 同款）
check:
	@echo "== [1/2] ruff lint =="
	$(RUFF) check backend scripts
	@echo "== [2/2] mypy =="
	$(MYPY) --explicit-package-bases --namespace-packages backend scripts
	@echo "OK: make check 全过"

## 自动修复 lint
lint-fix:
	$(RUFF) check --fix backend

## 格式化
format:
	$(RUFF) format backend

## ★ W23 Day6：30 并发压测（正式 Gate 数据，P95=1275ms；40 并发极限用 --concurrency 40）
loadtest:
	$(PY) -X utf8 deploy/load_test.py --concurrency 30 --per 7 --out deploy/reports/day6_load.json

## ★ W23 Day6：杀实例演练（压测中段 stop backend-a1 → 5xx=0 → 自动恢复）
drill:
	$(PY) -X utf8 deploy/load_test.py --concurrency 30 --per 7 --kill-instance a1 --kill-at-pct 0.4
