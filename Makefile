# ★ W23 SCM Copilot 平台 Makefile（Day1 建仓 + Day2 seed + Day3 认证 + Day4 双域并入）
# 用法：
#   make up        docker compose 起 MySQL（-d）
#   make down      停服务
#   make migrate   alembic upgrade head（含 Day3 token_blacklist）
#   make seed      平台库幂等种子（4 角色/12 权限/12 用户）
#   make test      pytest 平台单测 + 双域回归 + coverage
#   make test-auth 仅跑认证/RBAC（Day3，需 MySQL 已起 + seed）
#   make test-integration  双域集成回归脚本（审批/幂等/工具/validator，需 mock_biz）
#   make check     ruff lint + mypy（0 error 才算过）
PY := .venv/Scripts/python.exe
RUFF := $(PY) -m ruff
MYPY := $(PY) -m mypy
PYTEST := $(PY) -m pytest
COMPOSE := docker compose -f deploy/docker-compose.yml
# alembic.ini 在 backend/ 下，须在此目录运行（env.py 经 pythonpath 兜底）
BACKEND := backend

.PHONY: up down migrate seed test test-auth test-integration check lint-fix format help

## 默认：显示帮助
help:
	@echo "目标:"
	@echo "  make up        docker compose 起 MySQL（-d）"
	@echo "  make down      停服务"
	@echo "  make migrate   alembic upgrade head（含 Day3 token_blacklist）"
	@echo "  make seed      平台库幂等种子（4 角色/12 权限/12 用户）"
	@echo "  make test      pytest backend/tests + coverage"
	@echo "  make test-auth 仅跑认证/RBAC（Day3，需 MySQL 已起 + seed）"
	@echo "  make test-integration  集成回归（审批/幂等/工具/validator）"
	@echo "  make check     ruff lint + mypy（0 error 才算过）"

## 起 MySQL（W23 Day1）
up:
	$(COMPOSE) up -d

## 停服务
down:
	$(COMPOSE) down

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
