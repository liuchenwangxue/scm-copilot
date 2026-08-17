# W23 Day5 报告：数据迁移 + checkpointer MySQL 化 + 语义缓存 Redis 化

> 日期：2026-08-21 ｜ 依据：《W23学习执行手册》Day5 + 《03》第 4/5 节 ｜ 状态：核心达标

## 1. 目标回顾

把 stage3 的历史数据（审批/反馈/审计/LangGraph 断点）无损搬进 MySQL 平台库，
checkpointer 从 SQLite 切 MySQL，审批服务运行存储迁 MySQL，语义缓存迁 Redis——
"状态全部外置"，为 Day6 双实例 least_conn 无状态化铺平。

## 2. 数据迁移（`scripts/migrate_sqlite_to_mysql.py`）

### 2.1 迁移对象与结果

| # | 数据 | 源 | 目标 | 行数 |
|---|---|---|---|---|
| 1 | approvals | stage3-b/data/approvals.db | scm_platform.approvals | 0（源库无历史数据，如实记录） |
| 2 | feedback | stage3-a/reports/feedback_store.jsonl | scm_platform.feedback | 13 |
| 3 | audit_logs | stage3-b/data/audit.log + scm/data/audit.log | scm_platform.audit_logs | 19（含 Day5 HITL 演练新增，去重后） |
| 4 | checkpoints/writes | stage3-b/data/biz_agent.db | MySQL checkpointer 表 | 358 断点 / 858 写入 |

### 2.2 校验与幂等

- **幂等**：可重跑（已连跑 3 遍验证）。approvals 用 `ON DUPLICATE KEY UPDATE`；
  feedback/audit 先查后插（无唯一键表）；checkpoints 走 `AsyncMySaver.aput`（内部 upsert）。
- **校验**：逐表 COUNT 比对 + 关键字段 `md5(concat)` 聚合比对 +
  存在性校验（源行都能在目标找到；audit 目标含平台自产数据故不做全表相等）。

```
[OK]  approvals    源    0 行 | 目标匹配    0 行
[OK]  feedback     源   13 行 | 目标匹配   13 行
[OK]  audit_logs   源   19 行 | 目标匹配   19 行
[OK]  checkpoints  源 358 断点 | 目标 382 断点 | 断点 358/358 匹配 | 写入 858/858 匹配
```
> 目标 382 断点 = 迁移 358 + Day5 HITL 演练新增 24（存在性校验，非全表相等）。

### 2.3 三个实测坑（写入脚本注释）

1. **collation 1267**：`langgraph-checkpoint-mysql` 的 SELECT_SQL 用 `json_table` 生成
   临时列，硬编码 `CHARACTER SET utf8mb4`（MySQL 8 默认 0900_ai_ci）；本库默认
   utf8mb4_unicode_ci → 读回报 `Illegal mix of collations`。解决：建表后
   `ALTER TABLE checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations
   CONVERT TO utf8mb4_0900_ai_ci`（只影响 langgraph 管理表）。
2. **blob 版本冲突**：SQLite 历史 channel_versions 跨 checkpoint 重复，而 MySQL
   checkpoint_blobs 以 (thread,ns,channel,version) 为主键且 INSERT IGNORE →
   **按 checkpoint_id 降序迁移**（最新先写），冲突时保留最新值。
3. **writes 的 idx 覆盖**：`aput_writes` 内部用 `WRITES_IDX_MAP` 固定 idx
   （如 messages→1），与历史实际 idx 冲突 → 同 checkpoint 内两条 writes 落到同一
   主键，后写丢失（实测 16 条）。修正：**writes 不走 aput_writes，直接 SQL 插入**
   （保留原始 idx 与 type/blob 字节；`blob` 是保留字需反引号）。
4. **异步连接绑定事件循环**：asyncmy 连接与创建它的 loop 绑定，测试间必须重建单例。

## 3. checkpointer 切 MySQL（`AsyncMySaver`）

- 依赖：`langgraph-checkpoint-mysql`（3.0.0，asyncmy 驱动版 `AsyncMySaver`）+ aiomysql/PyMySQL。
- `app/domains/ops/persistence.py`：`get_async_checkpointer()` 默认 MySQL，
  `CHECKPOINTER_BACKEND=sqlite` 回退 AsyncSqliteSaver（测试/无 MySQL 环境）。
- 连接管理：进程内单例 + 首次调用时 `setup()` 建表 + collation 修复（不在 import 时连库）。
- **老会话续跑验证**：迁移前的 thread（如 `reg-c`）在新库读取，状态完整
  （intent=update_order, message=「把 PO-0002 的金额改成 9500」）。

## 4. 审批服务迁 MySQL（Day4 欠账清单落项）

- `app/domains/ops/security/approval.py`：sqlite3 → **pymysql**（同步驱动，图节点 approval_gate 是同步函数）。
- 平台 approvals 表补充 3 列（Alembic `b1c2d3e4f5a6`）：`operation` / `reason` / `idem_key`，
  保证接口语义无损（ApprovalRequest 不变）。
- 无状态化核销清单「审批单 SQLite→MySQL」✓。

## 5. HITL 断点迁移验证（`scripts/verify_hitl_resume.py`）

用**两个独立进程**模拟"杀进程重启"（状态真正在 MySQL，跨进程可见）：

```
[P1] interrupt! approval_id=...（断点落 MySQL，next=approval_gate）
[P2] 新进程读取断点: next=('approval_gate',) tasks=1
[P2] reply=订单 PO-0002 已更新：金额 ¥9500.0，交期 2026-09-15，状态 草稿。
[P2] tool_result.success=True
```

> 注：approval_gate 节点因 LangGraph interrupt 重放会重新 create 审批单（原 stage3 语义），
> resume 后审批的是新单；这属于既有行为，非迁移引入，如实记录。

## 6. 会话历史（conversations）写入路径

- `app/platform/conversation.py`：`touch_conversation()` 幂等 upsert（thread_id 唯一）。
- kb 域 `/api/kb/chat` 会话开始即落库（user_id/tenant_id/title）。
- 集成测试 3 例全绿（创建/幂等/空 thread 跳过）。

## 7. 语义缓存 Redis 化（`scm:semcache:*`）

- `app/shared/rag/semantic_cache.py`：内存 LRU → **Redis 权威共享 + 内存兜底**。
  key：`scm:semcache:{version}:{query_hash}`（条目）+ `:keys`（索引 set）。
- 双实例验证（`scripts/verify_semcache_redis.py`，mock embedder）：
  实例 A 写入 → 实例 B（全新进程）命中，sim=0.9342。
- RedisClient 补充 sadd/smembers/srem/delete_many（fail-open）。
- 坑：类作用域方法 `set` 遮蔽内置 `set`，注解 `set[str]` 求值失败 → `builtins.set[str]`。

## 8. 回归与质量门禁

```
pytest backend/tests → 68 passed（旧 63 + conversations 3 + checkpointer_mysql 2）
ruff check backend scripts → All checks passed
mypy backend scripts → Success: no issues found in 109 source files
```

## 9. 欠账清单（→ Day6/Day7 或 W24）

| 项 | 说明 |
|---|---|
| 老会话跨库续跑 | 已验证 MySQL checkpointer 读取历史断点 + 同 thread 续跑；跨库逐 thread 全量回归留 W24 |
| 语义缓存真实模型命中验证 | mock embedder 验证了 Redis 共享机制；真实 bge 模型命中率留 W24（环境装 sentence-transformers） |
| 审计日志 626→19 差异 | 迁移脚本只迁移 stage3 审计（19 条）；平台运行期自产审计不迁移（本就该留在平台库），如实记录 |

## 10. 面试素材

- **数据迁移方法论**：行数比对只是入门，关键字段校验和 + 幂等可重跑 + 迁移前后双写窗口；
  空源如实记录（approvals 0 行迁移不造假）。
- **collation 排障**：1267 的根因是 json_table 临时列 collation 与表不一致——暴露
  "生产环境字符集/排序规则必须统一规划"的教训。
- **checkpointer 换库**：358 断点零丢失迁移 + HITL 杀进程续跑成功——无状态化的核心证据。
- **异步驱动陷阱**：asyncmy 连接绑定事件循环，跨 loop 复用报 `NoneType.send`。
