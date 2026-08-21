# scripts 目录导航

三个脚本目录各管一段，找脚本前先看这里。

## 目录分工

| 目录 | 职责 | 运行环境 |
|---|---|---|
| `scripts/`（本目录） | 基础设施与数据运维：seed、迁移、TLS、诊断 | 宿主机（.venv） |
| `backend/scripts/` | NL2SQL 评测链路：评测集生成 + 各维度评测 | 宿主机；被调度任务 `eval_nightly` 以模块方式复用 |
| `deploy/verify_*.py` | 部署级验收：容器口径验证 + 端到端场景 | 容器内或宿主机，见下表 |

## scripts/（基础设施与数据运维）

| 脚本 | 用途 | Makefile 入口 |
|---|---|---|
| `seed_platform.py` | 平台库幂等种子（4 角色/12 权限/12 用户） | `make seed` |
| `seed_biz.py` | scm_biz 固定 seed（NL2SQL 靶场数据） | `make seed-biz` / `make reseed-biz` / `make check-biz` |
| `init_biz_db.py` | scm_biz 建库 + nl2sql_ro 只读账号 | `make init-biz-db` |
| `migrate_sqlite_to_mysql.py` | stage3 历史数据迁移（幂等可重跑） | `make migrate-data` |
| `migrate_sharding.py` | Qdrant 单 collection → 4 分片（幂等） | — |
| `verify_sharding.py` | 分片路由 + BM25 隔离验收（需 Qdrant） | — |
| `verify_biz_data.py` | scm_biz 种子数据业务真实性检查 | — |
| `verify_hitl_resume.py` | HITL 断点迁移验收（MySQL checkpointer） | — |
| `verify_semcache_redis.py` | 语义缓存 Redis 化双实例命中验证（mock embedder） | — |
| `kb_sync_smoke.py` | kb_increment_sync 真实环境验收（改文档 ≤5min 可检索） | `make kb-sync-smoke` |
| `gen_tls_certs.py` | mkcert 本地 TLS 证书生成 | `make tls` |
| `diag_mysql.py` | MySQL 连通性/时区/字符集诊断 | — |
| `daily_commit.sh` | 每日一键提交（Git Bash 运行，规避 GBK 乱码） | — |

## backend/scripts/（NL2SQL 评测链路）

| 脚本 | 用途 | Makefile 入口 |
|---|---|---|
| `gen_eval_set_v1.py` | 评测集 v1 生成（100 条三层，固定 gold SQL） | `make gen-eval` |
| `eval_nl2sql.py` | execution accuracy 评测（默认输出 `reports/nl2sql_eval_day3.json`） | `make eval-nl2sql` / `make eval-ab` / `make eval-day6` |
| `eval_link_recall.py` | Schema Linking 召回（gold 表 ⊆ Top-3） | `make eval-link-recall` |
| `eval_repair.py` | 错误自修复救回率（30 条坏 SQL） | `make eval-repair` |
| `eval_multiturn.py` | 多轮指代消解评测 | `make eval-multiturn` |
| `gen_multiturn_eval.py` | 多轮评测集生成（10 条 × 2–3 轮） | `make gen-multiturn` |
| `recompute_eval.py` | 评测重算调试工具（复用报告中的 gen SQL） | — |

## deploy/verify_*.py（部署级验收，注意运行口径）

**容器内跑**（依赖容器内 site-packages 的 app 模块与模型卷，`make verify-*` 自动完成 docker cp + exec）：

| 脚本 | 验证点 | Makefile 入口 |
|---|---|---|
| `verify_semcache_container.py` | 语义缓存命中（真实 bge 512 维） | `make verify-semcache` |
| `verify_route_container.py` | 语义路由分类（真实 bge） | `make verify-route` |
| `verify_session_redis.py` | 双实例会话跨实例指代消解 | `make verify-session` |
| `verify_durability_exit.py` | durability=exit 行为（低危直通 + 高危 resume） | `make verify-durability` |
| `verify_merge_write_count.py` | durability=exit checkpoint 写入行数量化 | `make verify-merge-count` |
| `verify_eval_container.py` | RAG 156 条评测（真实 bge，约 20 分钟） | `make verify-eval` |

**宿主机跑**（走 nginx HTTPS 入口，需全栈已起）：

| 脚本 | 验证点 | Makefile 入口 |
|---|---|---|
| `verify_e2e_day3.py` | 六域 14 项端到端冒烟 | `make smoke` |
| `verify_hitl_resume_d7.py` | HITL resume 不重复建审批单（需 `SCM_API_KEY`） | `make verify-hitl` |
