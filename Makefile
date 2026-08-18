# ★ W23 SCM Copilot 平台 Makefile（Day1 建仓 + Day2 seed + Day3 认证 + Day4 双域并入
#   + Day5 数据迁移 + Day6 无状态双实例；W24 起加入业务库/NL2SQL 目标）
# 用法：
#   make up-mysql   只起 MySQL+Redis（本地迁移/seed 用）
#   make up         起全栈（mysql/redis/mock-biz/backend-a1/a2/nginx）
#   make down       停全栈
#   make build      构建 backend + mock-biz 镜像
#   make migrate    alembic upgrade head（平台库）
#   make seed       平台库幂等种子（4 角色/12 权限/12 用户）
#   make migrate-biz   scm_biz 迁移（独立 alembic_biz 链）
#   make seed-biz      scm_biz 固定 seed（幂等 TRUNCATE 后重插）
#   make reseed-biz    重建 scm_biz 六表 + seed（drop→create→seed）
#   make check-biz     scm_biz 行数 + 校验和（重放一致性验收）
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

.PHONY: up up-mysql down build migrate seed init-biz-db migrate-biz seed-biz reseed-biz check-biz test test-auth test-sql-validator test-executor test-nl2sql-e2e test-repair test-session-ctx test-scheduler gen-eval eval-nl2sql eval-repair eval-multiturn gen-multiturn eval-day6 test-integration check lint-fix format loadtest drill help

## 默认：显示帮助
help:
	@echo "目标:"
	@echo "  make up-mysql  只起 MySQL+Redis（本地迁移/seed 用）"
	@echo "  make up        起全栈（mysql/redis/mock-biz/backend-a1/a2/nginx）"
	@echo "  make down      停全栈"
	@echo "  make build     构建 backend + mock-biz 镜像"
	@echo "  make migrate   alembic upgrade head（平台库）"
	@echo "  make seed      平台库幂等种子"
	@echo "  make init-biz-db  scm_biz 建库+只读账号（幂等）"
	@echo "  make migrate-biz  scm_biz 迁移（独立链）"
	@echo "  make reseed-biz   scm_biz 重建六表+seed（Day1）"
	@echo "  make check-biz    scm_biz 行数+校验和"
	@echo "  make test      pytest backend/tests + coverage"
	@echo "  make test-auth 仅跑认证/RBAC"
	@echo "  make test-sql-validator  NL2SQL 四道闸+攻击用例（Day2，无需 DB）"
	@echo "  make test-executor      只读沙箱执行器（Day2，需 MySQL）"
	@echo "  make test-repair        错误自修复测试（Day5）"
	@echo "  make test-session-ctx   多轮会话测试（Day5）"
	@echo "  make test-scheduler     调度基座测试（Day1：leader 锁互斥 + job_runs）"
	@echo "  make gen-eval           生成评测集 v1（Day3）"
	@echo "  make eval-nl2sql        execution accuracy 评测（Day3）"
	@echo "  make eval-link-recall   Schema Linking 召回评测（Day4）"
	@echo "  make eval-ab            v1 vs v2 A/B 对比（Day4）"
	@echo "  make gen-multiturn      生成多轮评测集（Day5）"
	@echo "  make eval-repair        修复救回率评测（Day5，30 条坏 SQL）"
	@echo "  make eval-multiturn     多轮指代消解评测（Day5，10 条过 8）"
	@echo "  make eval-day6          real 全量 100 条 v2（Day6，含 P95 + token/条）"
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

# ==================== W24：业务库 scm_biz（NL2SQL 靶场） ====================

## scm_biz 库 + nl2sql_ro 只读账号初始化（幂等；已存在数据卷的环境先跑一次）
init-biz-db:
	$(PY) -X utf8 scripts/init_biz_db.py

## scm_biz 迁移（独立 alembic_biz 链，隔离 platform 版本树）
migrate-biz:
	cd $(BACKEND) && $(PY) -X utf8 -m alembic -c alembic_biz.ini upgrade head

## scm_biz 固定 seed（幂等：TRUNCATE 后重插；连跑两遍校验和一致）
seed-biz:
	$(PY) -X utf8 scripts/seed_biz.py

## 重建 scm_biz 六表 + seed（drop→create→seed，Day1 验收）
reseed-biz:
	$(PY) -X utf8 scripts/seed_biz.py --reseed

## scm_biz 行数 + 校验和（只读，不写库）
check-biz:
	$(PY) -X utf8 scripts/seed_biz.py --check

## ★ W23 Day5：stage3 历史数据迁移（审批/反馈/审计/LangGraph 断点，幂等可重跑）
migrate-data:
	$(PY) scripts/migrate_sqlite_to_mysql.py

## pytest + coverage
test:
	$(PYTEST) backend/tests --cov=backend --cov-report=term-missing

## 仅跑认证/RBAC（Day3，需 MySQL 已起 + seed）
test-auth:
	$(PYTEST) backend/tests/test_auth.py backend/tests/test_rbac.py -v

## ★ W24 Day2：NL2SQL 四道闸 + 攻击用例（纯 AST 逻辑，无需 DB）
test-sql-validator:
	$(PYTEST) backend/tests/test_sql_validator.py backend/tests/test_attack_cases.py -v

## ★ W24 Day2：只读沙箱执行器（需 MySQL + scm_biz seed + nl2sql_ro）
test-executor:
	$(PYTEST) backend/tests/test_executor.py -v

## ★ W24 Day3：NL2SQL e2e 链路（mock 生成 + 四道闸 + 执行 + API 权限）
test-nl2sql-e2e:
	$(PYTEST) backend/tests/test_nl2sql_e2e.py -v

## ★ W24 Day3：生成评测集 v1（Day4 扩至 90 条：单表30/join40/聚合20，固定 gold SQL）
gen-eval:
	$(PY) -X utf8 backend/scripts/gen_eval_set_v1.py

## ★ W24 Day3：execution accuracy 评测（默认 mock 测链路；real 用 LLM_PROVIDER=real）
eval-nl2sql:
	$(PY) -X utf8 backend/scripts/eval_nl2sql.py

## ★ W24 Day4：Schema Linking 召回准确率（gold 表 ⊆ Top-3，≥90% 达标）
eval-link-recall:
	$(PY) -X utf8 backend/scripts/eval_link_recall.py

## ★ W24 Day4：A/B 基线对比 v1 vs v2（准确率 + prompt token 降幅 ≥50%）
eval-ab:
	$(PY) -X utf8 backend/scripts/eval_nl2sql.py --ab

## ★ W24 Day5：错误自修复测试（修复 prompt/路由 mock 单测 + 修复链集成；需 MySQL+seed）
test-repair:
	$(PYTEST) backend/tests/test_repair.py -v

## ★ W24 Day5：多轮会话测试（消解规则 mock 单测 + 全链路集成；需 MySQL+seed）
test-session-ctx:
	$(PYTEST) backend/tests/test_session_ctx.py -v

## ★ W25 Day1：调度基座测试（leader 锁互斥纯逻辑无需 DB + job_runs 落库/重启持久性需 MySQL）
test-scheduler:
	$(PYTEST) backend/tests/test_scheduler_leader.py backend/tests/test_scheduler_jobs.py -v

## ★ W24 Day5：生成多轮评测集（10 条对话 × 2–3 轮，固定 gold SQL）
gen-multiturn:
	$(PY) -X utf8 backend/scripts/gen_multiturn_eval.py

## ★ W24 Day5：错误自修复救回率评测（30 条坏 SQL：错列名/错表名/语法错各 10；mock 测链路，real 测效果）
eval-repair:
	$(PY) -X utf8 backend/scripts/eval_repair.py

## ★ W24 Day5：多轮指代消解评测（10 条过 8；mock 测链路，real 测效果）
eval-multiturn:
	$(PY) -X utf8 backend/scripts/eval_multiturn.py

## ★ W24 Day6：real 全量 100 条（v2 Schema Linking，含 P95 + token/条）
eval-day6:
	$(PY) -X utf8 backend/scripts/eval_nl2sql.py --prompt-version v2 --out reports/nl2sql_eval_day6_real_v2.json

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
