# W25 Day2 学习执行日志 · 数据闭环三任务（9/1 周二）

> 阶段四 W25 · 核心产物 #2：知识库新鲜度 + 向量卫生 + 审计归档——三个"运维自动化"任务上线

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `jobs/kb_increment_sync.py`（*/5min）：mtime 增量扫描 + 重切块重嵌入 + Qdrant 幂等 upsert + 删除同步 | ✅ | `scan_changes`（new/changed/deleted 三集合）；point id = `uuid5(内容)` 内容寻址；删除按 payload `source_doc_id` 过滤（非 point id） |
| 增量验证流程：改文档 → 计时到可检索 | ✅ | 真实环境 smoke：改段落 1.5s 后重同步，Qdrant 检索到新内容、旧内容无残留 |
| `jobs/vector_cleanup.py`（每日 03:00）：孤儿向量 + 语义缓存过期键扫描 | ✅ | scroll 全量 → 按 docs 表 active 判定孤儿；`scm:semcache:*:keys` TTL 漏网成员 + 版本失效标记清理 |
| `jobs/audit_archive.py`（每月 1 日 04:00）：CTAS 归档 + 行数校验 + 删主表 + 幂等批次 | ✅ | `audit_logs_YYYYmm` 归档；先校验后删主表；批次锁防并发 + 归档表存在即幂等跳过 |
| admin 调度面板 API：`GET /api/admin/scheduler/jobs` + `POST .../{name}/trigger` | ✅ | 六任务状态/上次运行/next_run + 手动触发写审计（`admin:scheduler:trigger` 落 audit_logs） |
| docs 元数据表（DocMeta） | ✅ | `alembic c3d4e5f6a7b8` 迁移；doc_id/file/mtime/hash/chunk_count/status |
| 水位推进语义：成功推进、失败不推进 | ✅ | `kb:sync:last_ts` Redis；`scan_ts` 取扫描开始时刻（防扫描期间改动漏检） |

## 二、实测数字

- **真实环境 smoke**（`scripts/kb_sync_smoke.py`，独立 collection `scm_kb_smoke_w25`，不碰正式数据）：

| 步骤 | 结果 |
|---|---|
| 首次全量（2 篇测试文档） | `new=2` → docs 表登记 + Qdrant 检索命中 2 文档 |
| 重复同步 | `new=0 changed=0`（uuid5 内容寻址幂等，向量数不变） |
| 改文档（100万 → 200万） | `changed=1`；检索"200万"命中新内容、旧内容无残留（先删后插） |
| 删文档（库存） | `deleted=1`；检索"库存盘点"不再命中该 doc（source_doc_id 过滤删） |

- **测试**：新增 27 用例全绿——`test_kb_increment_sync.py` 8 / `test_vector_cleanup.py` 7 / `test_audit_archive.py` 5 / `test_admin_scheduler_api.py` 4（含权限 403 / 未启用 503 / 真实调度器面板+触发+审计）
- **调度域回归**：Day1 leader 锁 4 + job_runs/持久化 6 + 新增 27 = 37 全绿
- **全量回归**：`pytest backend/tests -m "not model and not llm"` **265 passed**
- **静态检查**：ruff 0 / mypy 0（157 source files）；ruff format 对齐
- **面板 API 实测**：`GET /api/admin/scheduler/jobs` → 200，`scheduler.running=true`、6 job 带 cron/next_run/last_run；`POST trigger` → 200 + `audit_logs` 新增 `admin.scheduler.trigger`（actor=admin 用户）
- **归档实测**（integration）：预置 8 月审计 3 条 → `_archive_batch(now=2026-09-15)` → 归档表 3 行、主表 8 月数据删空、9 月数据保留；重跑 → `skipped (already exists)`

## 三、关键决策与踩坑记录

### 决策 1：docs 元数据表放平台库，变更检测以"表记录 mtime"为权威
- 手册说"docs 元数据表（stage3-a 已有）"——实际 scm-copilot 平台无此表，故新建 `docs` 表（alembic 迁移）
- 变更检测：文件 `mtime > 表记录 file_mtime`（严格 `>`，手册坑）且 `> 水位 last_ts`；`content_hash` 兜底（防 mtime 精度漏检）
- 首轮（表空）全量；删除文档保留记录（status=deleted），文件回归视为新文档重新入库

### 决策 2：幂等双保险——uuid5 内容寻址 + 先删后插
- **point id = `uuid5(text)`（内容寻址）**：同一文本块重复同步 → 同 id → upsert 覆盖，零重复（fail-open 双实例全跑也安全）
- **变更文档先按 `source_doc_id` 删旧向量再插新**：增量场景拿不到旧 point id（手册坑），过滤删除保证"chunk 数减少"时旧块不残留；实测改文档后旧内容检索不到

### 决策 3：水位 `scan_ts` 取扫描开始时刻
- 若取完成时刻：扫描期间改动的文件 mtime ∈ (开始, 完成) → 下轮 `mtime > 完成时刻` 不成立 → **漏检**
- 取开始时刻：扫描时已见文件 mtime ≤ 开始时刻（本轮处理、下轮跳过）；扫描中改动的文件 mtime > 开始时刻 → 下轮必处理

### 决策 4：collection 旧格式自动重建
- stage3 旧数据 point id 是数字偏移、payload 无 `source_doc_id` → 无法过滤删除，与新增点并存会造成重复
- `_ensure_collection`：collection 存在但首点无 `source_doc_id` → 首轮重建（一次性成本，57 篇全量嵌入几分钟）

### 决策 5：归档幂等以"归档表存在"为真相，Redis 批次锁只防并发
- **坑修正**：最初用 Redis `SETNX` 作幂等标记——若任务中途失败，锁已设置 → 下月/手动重试被永久跳过（归档永不执行）
- 改为：归档表已存在 → 跳过（数据库真相）；Redis `archive:batch:{batch}` 仅作两段间防并发（CTAS→校验→删主表不可打断），`finally` 释放，失败不残留

### 坑记录：alembic 多 head
- 新迁移 down_revision 误指 `a1b2c3d4e5f6`，而实际 head 是 `b1c2d3e4f5a6`（approvals_extend）→ `upgrade head` 报"Multiple head revisions"
- 修正 down_revision 后迁移成功（平台库已有 approvals 扩展列，说明此前已升级到 b1c2d3e4f5a6）

### 坑记录：TestClient 同步环境跑 async
- `svc.start()`（AsyncIOScheduler 需 running loop）与 async DB 断言不能直接跑 → 经 `client.portal.call()` 在 TestClient 的 lifespan loop 内执行

### 坑 4（★ 重要）：测试触发真实任务 = 副作用事故
- **现象**：`test_scheduler_jobs.py` 的 `_run_job("kb_increment_sync")` 在 Day1 是无副作用 stub，Day2 换成真实实现后直接**全量同步 57 篇文档 + 重建 scm_kb_v1 collection**（stage3 旧格式无 source_doc_id → `_ensure_collection` 判定重建）
- **连锁**：kb_sync_smoke 用临时 docs 目录，把不在目录的正式文档全部误标 `deleted`（57 条）；测试 `clean_docs` fixture 又用 `DELETE FROM docs` 清空整表——正式登记两次被污染
- **修复三层**：
  1. `test_scheduler_jobs` 用轻量 stub 替换任务 func（机制验证与业务实现解耦）
  2. smoke/测试的清理改为**快照恢复**：运行前记录 docs 表 (doc_id, status)，结束后删新增 + 还原被改状态（不用 `DELETE FROM docs` 全清）
  3. `_ensure_collection` 重建是有意设计（平台接管旧数据），但触发源应从"部署首轮"而非"测试"
- **教训**：调度任务一旦从 stub 换真实实现，所有"会执行任务"的测试路径都要审计副作用边界

## 四、验收（手册 Day2 验收项）

| 验收项 | 结果 |
|---|---|
| 改文档 ≤5min 可检索（*/5min 任务触发） | ✅ smoke 实测：改段落后重同步 → 检索到新内容；水位/幂等机制保证 ≤5min 内收敛 |
| 删除文档向量同步消失 | ✅ smoke 实测：删除后 `source_doc_id` 过滤删向量，检索不再命中 |
| 手动触发有审计 | ✅ `POST trigger` → `audit_logs` 落 `admin.scheduler.trigger` |
| 三任务 job_runs 正常 | ✅ 注册表 cron 正确（`*/5 / 0 3 / 0 4 1`）；`_run_job` 统一包装 running→success/skipped/failed |

## 五、面试题 0.5h：KB 增量同步的幂等设计

**uuid5 内容寻址 + last_sync_ts 水位，重复执行零副作用**：

1. **内容寻址幂等**：Qdrant point id = `uuid5(chunk_text)`——同一文本块重复同步是"覆盖写"而非新增，即使 Redis 挂（fail-open 双实例全跑）也不会产生重复向量
2. **水位推进语义**：`kb:sync:last_ts` 只在任务成功后才推进（失败不推进，下轮重扫）；水位取扫描开始时刻，保证"扫描期间改动的文件"不被跳过
3. **删除路径幂等**：变更文档"按 source_doc_id 过滤删旧向量 → 重切块 → upsert"，删除操作本身可重复执行
4. **双保险**：leader 锁（互斥执行）+ 任务幂等键（最终一致性）——锁失效时副作用为零，锁正常时互斥兜底

> 讲点：幂等不是"加个判断"，而是**让重复执行的结果等于执行一次**——内容寻址覆盖写 + 水位收敛 + 过滤删除，三层各管一件事。

## 六、欠账 / 次日衔接（W25 Day3 优先）

- [ ] 六任务仍有两个 stub：daily_brief / eval_nightly / cache_warmup → Day3 实现
- [ ] 24h 零重复观测：Day3 双实例挂后台启动（机制已就绪：leader 锁 + job_runs + 幂等键）
- [ ] 正式 collection（scm_kb_v1）首轮重建：由任务自动完成（`_ensure_collection`），部署环境跑一次即可
- [ ] W23 遗留"40 并发 P95 评估"（W24 欠账，继续挂账）

## 七、W25 周 Gate 进度

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | 🚧 机制就绪（leader 锁 + job_runs + 幂等键），Day3 启动 24h 观测 |
| KB 同步 ≤5min | ✅ 机制 + smoke 实测通过（待双实例 24h 观测背书） |
| 日报准点 5/5 | 🚧 Day3 实现 |
| SDK pip 十行跑通 | ⏳ Day5 |
| 429 用例过 | ⏳ Day5 |
