# ★ W23 SCM Copilot 平台 Makefile（Day1 建仓版 + Day2 seed）
# 用法：
#   make up        docker compose 起 MySQL（-d）
#   make down      停服务
#   make migrate   alembic upgrade head（Day2 生效）
#   make seed      平台库幂等种子（4 角色/12 权限/12 用户）
#   make test      pytest 平台单测 + coverage
#   make check     ruff lint + mypy（CI 同款，本地兜底）
PY := .venv/Scripts/python.exe
RUFF := $(PY) -m ruff
MYPY := $(PY) -m mypy
PYTEST := $(PY) -m pytest
COMPOSE := docker compose -f deploy/docker-compose.yml
# alembic.ini 在 backend/ 下，须在此目录运行（env.py 经 pythonpath 兜底）
BACKEND := backend

.PHONY: up down migrate seed test check lint-fix format help

## 默认：显示帮助
help:
	@echo "目标:"
	@echo "  make up        docker compose 起 MySQL（-d）"
	@echo "  make down      停服务"
	@echo "  make migrate   alembic upgrade head（Day2 生效）"
	@echo "  make seed      平台库幂等种子（4 角色/12 权限/12 用户）"
	@echo "  make test      pytest backend/tests + coverage"
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

## pytest + coverage
test:
	$(PYTEST) backend/tests --cov=backend --cov-report=term-missing

## lint + 类型检查（CI 同款）
check:
	@echo "== [1/2] ruff lint =="
	$(RUFF) check backend
	@echo "== [2/2] mypy =="
	$(MYPY) --explicit-package-bases --namespace-packages backend
	@echo "OK: make check 全过"

## 自动修复 lint
lint-fix:
	$(RUFF) check --fix backend

## 格式化
format:
	$(RUFF) format backend
