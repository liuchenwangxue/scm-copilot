# W25 Day1 学习执行日志 · 调度基座（8/31 周一）

> 阶段四 W25 · 核心产物 #1：APScheduler + MySQL job store + leader 锁——任务"定义不丢、互斥执行、重启自动恢复"

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `platform/scheduler/__init__.py` 封装（AsyncIOScheduler + MySQLJobStore + 六任务注册表） | ✅ | `AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore})`；六任务集中注册表（name→cron→func→desc） |
| `platform/scheduler/leader.py` 任务级互斥装饰器（复用 W19 锁改造成 async 装饰器） | ✅ | `leader_lock(name, ttl)`：SETNX + owner 校验 Lua 释放 + fail-open 放行 + `SkipResult` |
| 六任务 job 骨架 | ✅ | `scheduler/jobs/` 六模块：`CRON` + `async def run()` 占位（Day2/3 填真实逻辑） |
| `scheduler_job_runs` 写入（running→success/failed；互斥记 skipped） | ✅ | `_run_job` 统一包装；双实例实测 A=success / B=skipped 各留一行 |
| lifespan 集成（startup start / shutdown wait=False 优雅停） | ✅ | `main.py` lifespan；启动失败 fail-open 降级不阻塞主服务 |
| 单测：互斥 / owner 校验 / TTL 过期重抢 | ✅ | `test_scheduler_leader.py` 4 用例全绿（FakeRedis 纯逻辑，CI 可跑） |
| 单测：job_runs 落库 + 重启持久性 | ✅ | `test_scheduler_jobs.py` 6 用例全绿（integration） |
| misfire/coalesce 验证 | ✅ | 错过触发点（next_run_time 设过去 1min）→ 启动后 300s 宽限期内补跑合并 1 次 |
| Makefile / README | ✅ | `make test-scheduler`；README §六·八 |

## 二、实测数字

- **health**：`GET /health` → `{"status": "ok", "db": "up", "scheduler": "running"}`
- **六任务注册 + next_run_time**（时区 +08:00 正确，与 MySQL TZ 一致）：

| job | cron | next_run（实测） |
|---|---|---|
| kb_increment_sync | `*/5 * * * *` | 19:55（每 5 分钟） |
| vector_cleanup | `0 3 * * *` | 次日 03:00 |
| audit_archive | `0 4 1 * *` | 2026-09-01 04:00 |
| daily_brief | `0 8 * * 1-5` | 次日 08:00 |
| eval_nightly | `0 2 * * *` | 次日 02:00 |
| cache_warmup | `0 7 * * *` | 次日 07:00 |

- **双实例互斥实测**（真实 Redis，实例 A/B 并发触发 `kb_increment_sync`）：

| run_id | status | instance | duration_ms | error |
|---|---|---|---|---|
| kb_increment_sync:20260818195501:backend-a1 | **success** | backend-a1 | 8 | — |
| kb_increment_sync:20260818195501:backend-a2 | **skipped** | backend-a2 | — | another instance holds lock |

> 零重复观测口径：按 (job, 秒窗口) 聚合，`status != skipped` 恰 1 行——双实例 job_runs 各留一行，互斥由 leader 锁保证。

- **重启持久性**：`start → shutdown → start`（同 MySQL job store）→ 六任务 id 与 next_run_time 完整恢复（`test_job_store_persistence_across_restart`）
- **misfire 补跑**：把 `next_run_time` 改到过去 1 分钟 → start 后宽限期内自动补跑合并 1 次（coalesce=True，不堆积）
- **手动触发**：`PlatformScheduler.trigger(job_name)` → job_runs 新增 1 条 success；原 cron job 仍保留（独立一次性 job，不覆盖）
- **测试**：`test_scheduler_leader.py` 4 passed + `test_scheduler_jobs.py` 6 passed + health 更新 2 passed；全量 `pytest backend/tests` 全绿；ruff 0 / mypy 0（153 source files）

## 三、关键决策与踩坑记录

### 坑 1（★ 手册未提，实测踩坑）：MySQL job store pickle 序列化要求回调模块级可导入
- **现象**：`add_job` 时传闭包（`PlatformScheduler._wrap.<locals>._entry`）→ 报
  `ValueError: This Job cannot be serialized since the reference to its callable could not be determined`
- **根因**：`SQLAlchemyJobStore` 用 pickle 持久化 job，`func_ref` 必须是 `module:func` 字符串引用；闭包/局部函数无法定位
- **解决**：任务入口改为**模块级 `_run_job(job_name)`**（注册表查找 + 抢锁 + 落库），运行时上下文（session_factory/instance_id）经模块级 `_runtime` 字典注入，由 `start()` 写入；job store 只存 `app.platform.scheduler:_run_job`

### 坑 2：run_id 秒级时间戳双实例冲突
- **现象**：双实例并发时 run_id 相同（同秒），success 与 skipped 互相覆盖同一行（状态混杂，只留 1 行）
- **解决**：run_id = `{job}:{yyyyMMddHHmmss}:{instance}`——每实例各留一行，观测口径清晰

### 坑 3：手动触发不能 `reschedule_job`
- **现象**：`reschedule_job` 把原 cron job 换成一次性 DateTrigger → 触发后被移除，**原 cron 调度丢失**
- **解决**：`trigger()` 注册一个**独立**一次性 job（id 带毫秒时间戳），跑完自删，原 cron job 不受影响

### 决策 1：job store 用同步 pymysql engine（与 asyncmy 池独立）
- `SQLAlchemyJobStore` 是同步 SQLAlchemy API；DSN 由 settings 从 `platform_dsn` 派生（`asyncmy→pymysql`），CI 与本地自动同库
- 表 `apscheduler_jobs` 内建在 `scm_platform`（job store 自带建表，无需 alembic）

### 决策 2：双实例同时 start 的 store 初始化交给 APScheduler 自带锁
- 手册坑提示"别自己再加全局锁死锁"——`SQLAlchemyJobStore` 初始化表/锁由 APScheduler 内部协调，实测双实例 `start()` 无冲突

### 决策 3：测试环境默认关闭随应用的调度器
- 每个 TestClient 起 AsyncIOScheduler 会连 MySQL job store（建表 + 注册 + 后台循环），拖慢单测且污染测试库
- `conftest.py` 设 `SCM_SCHEDULER_ENABLED=0`；调度功能由 `test_scheduler_jobs.py` 专项覆盖；部署环境默认开启

## 四、验收（手册 Day1 验收项）

| 验收项 | 结果 |
|---|---|
| 重启后任务定义/next_run 完整 | ✅ `test_job_store_persistence_across_restart` |
| 互斥单测绿 | ✅ leader 4 用例（并发互斥 / owner 校验 / TTL 重抢 / fail-open） |
| job_runs 有记录 | ✅ 双实例实测 2 行（success + skipped）+ misfire 补跑 + 手动触发 |

## 五、面试题 0.5h：Q5——APScheduler 多实例如何防重复执行？

**三层防重复**（今天亲手全写了）：
1. **调度器全实例跑**：不是"单实例跑调度"——任何实例挂了，其他实例的调度器照常触发（更高可用）
2. **任务级互斥（leader 锁）**：每个触发点所有实例都收到事件，但只有抢到 `SET lock:job:{name} NX EX 300` 的实例执行；未抢到的记 `skipped`（job_runs 可观测）；owner 校验 Lua 释放防误删；TTL 超时锁自动让出防死锁
3. **任务幂等键双保险**：即使锁失效（如 Redis 挂 fail-open 全实例跑），任务自身幂等兜底——KB 用 uuid5 内容寻址、日报用日期键（Day2/3 实现）

> 讲点：三层分别解决"高可用"、"互斥"、"最终一致性"三个问题；Redis 挂时 fail-open 宁可多跑不可卡死，副作用由幂等键兜底。

## 六、欠账 / 次日衔接（W25 Day2 优先）

- [ ] 六任务为占位 stub——Day2 实现 kb_increment_sync / vector_cleanup / audit_archive
- [ ] Day2 接 admin 调度面板 API：`GET /api/admin/scheduler/jobs` + `POST .../trigger`（trigger() 已就绪，挂路由 + 审计）
- [ ] W23 遗留"40 并发 P95 评估"（W24 欠账，Day1 未评估——原计划半天，建议 Day2 上午插缝或明确记二期 backlog）
- [ ] 24h 零重复观测：需双实例挂后台跑，Day3 启动观测

## 七、W25 周 Gate 进度

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | 🚧 机制就绪（leader 锁 + job_runs），Day3 启动 24h 观测 |
| KB 同步 ≤5min | 🚧 Day2 实现 |
| 日报准点 5/5 | 🚧 Day3 实现 |
| SDK pip 十行跑通 | ⏳ Day5 |
| 429 用例过 | ⏳ Day5 |
