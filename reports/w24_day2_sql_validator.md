# W24 Day2 学习执行日志 · 安全四道闸与只读沙箱（8/25 周二）

> 阶段四 W24 · 核心产物 #2：`sql_validator.py` 四道闸 + `executor.py` 只读沙箱 + 20 攻击用例 20/20 拦截

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `sql_validator.py` 四道闸（唯一权威实现） | ✅ | `app/domains/data/sql_validator.py`（闸1 单语句 / 闸2 仅SELECT / 闸3 禁写 / 闸4 危险函数 + 扩展：锁读/表白名单） |
| `executor.py` 只读沙箱执行器 | ✅ | `app/domains/data/executor.py`（独立 engine / 3s 超时 / 行数上限 200 / 1MB 截断 / 审计回调） |
| 每闸 ≥5 单测分支 | ✅ | `test_sql_validator.py` 53 用例（堆叠/写根/伪装嵌写/危险函数/混淆/白名单/合法 0 误拦） |
| 20 条攻击用例 20/20 拦截 0 逃逸 | ✅ | `test_attack_cases.py` 参数化 20 条，每条 reason 正确 |
| executor 三约束测试 | ✅ | `test_executor.py` 12 用例（超时/行数/字节/只读/审计） |
| CI 接入 | ✅ | ci.yml 增加 `sql_validator + attack cases` 快速安全回归步骤（纯 AST 无 DB） |
| settings 只读 DSN | ✅ | `settings.biz_ro_dsn`（CI 用 `SCM_BIZ_RO_DSN` 覆盖） |

## 二、实测数字

- **攻击用例 20/20 拦截，0 逃逸**：
  - 堆叠 3 条（`;` 拼接/换行注入）→ `multi-statement`
  - 写根 7 条（UPDATE/DELETE/INSERT/DROP/ALTER/CREATE/GRANT）→ `not-select`
  - 伪装嵌写 3 条（`SELECT (DELETE...)` / `IN (UPDATE...)` / CTE 嵌写）→ `parse-error` 或 `write-op`
  - 危险函数 4 条（sleep/SlEeP/benchmark/load_file）→ `dangerous-func`
  - 越权/锁读 3 条（UNION 探测 users / 跨库 scm_platform / FOR UPDATE）→ `unknown-table` / `for-update`
- **合法查询 0 误拦**：join/聚合/HAVING/子查询/CTE/UNION ALL/已带 LIMIT/OFFSET/DISTINCT 全过
- **强制 LIMIT**：无 LIMIT 自动注入 `LIMIT 200`（不重复包一层）；`LIMIT 10` 保留原样
- **executor 实测**（本地 MySQL，seed 已就位）：
  - 近 7 天按区域聚合：4 行，22.8ms
  - 订单+供应商 join：200 行（行数截断），24.3ms
  - `UPDATE orders` 直连 → `ExecutionError: (1142) UPDATE command denied to user 'nl2sql_ro'`
  - `SELECT SLEEP(5)` + timeout=1s → `QueryTimeoutError`
- **测试**：新增 85 用例（53+20+12）；完整回归 **160 passed**；ruff 0 / mypy 0（106 source files）

## 三、关键决策与踩坑记录（sqlglot 30.x 实测）

### 决策 1：sqlglot 版本锁 30.x 而非手册写的 25.x
- 环境已装 `sqlglot 30.17.0`（非手册"25.x 系"）。**升级确实改了 AST 结构**，以下行为全部实测 30.x 验证：
  - `read="mysql"` 必传，否则 `LIMIT ... OFFSET` 方言漂移
  - `exp.Union` 根节点覆盖 `UNION ALL`（天然）
- 已 `pyproject.toml` pin `sqlglot>=30,<31`，并注释 30.x 与 25.x 的差异点

### 坑 1：Anonymous 函数名取 `.name`，命名函数取 `.sql_name()`
- `SELECT sleep(5)` / `SlEeP(5)` / `SLEEP/**/(5)` 解析为 `exp.Anonymous`，`sql_name()` 返回 `"ANONYMOUS"`，函数名在 `.name`
- `MD5('x')` 是命名函数 `exp.MD5`，`.name` 返回 `'x'`（参数！），函数名在 `.sql_name()`
- **修复**：`_func_name` 分型取（Anonymous→`.name`，否则→`.sql_name()`），再 `lower()` 比对黑名单

### 坑 2：`SELECT (DELETE FROM orders)` / `IN (UPDATE ...)` 在 30.x 直接 ParseError
- 手册闸3"伪装嵌写"用例在 25.x 解析为 AST（`find` 拦截）；30.x 直接抛 `ParseError`
- **处理**：ParseError 一律转 `SQLRejected("parse-error")`——拒绝语义等价，测试断言 reason ∈ {parse-error, write-op}

### 坑 3：CTE 的 WITH 挂在 `args["with_"]` 不是 `args["with"]`
- `WITH c AS (SELECT 1) SELECT * FROM c` 根节点是 `exp.Select`，CTE 子句在 `with_`
- 白名单校验需排除 CTE 名（`WITH c1, c2` 的 `c1/c2` 会出现在 `find_all(exp.Table)`），否则合法 CTE 被误拒

### 坑 4：Union 根节点 `.limit()` 无效，须 `set("limit", ...)`
- `tree.limit(200)` 对 `exp.Union` 根节点不生效（30.x 行为）；Select 根正常
- **修复**：统一用 `tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))`——对 Select/Union/With 全兼容

### 坑 5：`date.isoformat()` 无 `sep` 参数（mypy 报错）
- `datetime.isoformat(sep=" ")` 合法；`date.isoformat(sep=...)` 报 call-arg 错
- **修复**：`_normalize` 中 datetime/date 分型调用

### 坑 6：FOR UPDATE 需额外扩展闸
- 手册坑："`SELECT ... FOR UPDATE`（锁读）拒绝"。sqlglot 解析后 `exp.Lock` 节点（`find(exp.Lock)` 可检测）
- 已实现为扩展闸：`for-update`

### 决策 2：表白名单作为"越权拦截"第五道扩展闸
- 手册四道闸拦不住 `SELECT * FROM orders UNION SELECT password FROM users`（Union 根合法、无写、无危险函数）
- **但这是手册 Day2 攻击用例之一**——必须加表白名单（业务库六表）才能 20/20
- 实现：`allowed_tables` 参数默认 `SCM_BIZ_TABLES`，遍历 `find_all(exp.Table)` 校验；跨库（`tab.db` 非空）一并拒绝

### 决策 3：executor 审计用"回调注入"而非直接 import platform
- 域间协作纪律：data 域不 import platform 内部模块。审计写 `audit_logs` 的实现由调用方注入
- 事件结构：`{event, sql 原文, status, error, elapsed_ms, rows}`——SQL 原文落审计，取证可回放

## 四、纵深防御叙事（面试题 20:00 段）

**Q3：四道闸分别防什么？只靠它够吗？**

1. **闸1 防堆叠**：`;` 拼接多语句注入——`SELECT 1; DROP TABLE orders` 直接 multi-statement 拒绝。这是注入最粗的攻击面。
2. **闸2 防非查询**：根节点必须 SELECT/UNION——UPDATE/DELETE/INSERT/DDL 全拒。AST 白名单而非黑名单：**默认拒绝、只放行查询**。
3. **闸3 防伪装嵌写**：`SELECT (DELETE FROM orders)`、CTE 里藏 DELETE——递归 `find` 子句级拦截，防"看起来是查询实际在写"。
4. **闸4 防拖库/读文件**：sleep/benchmark 拖库、load_file/outfile 读写服务器文件——黑名单兜住模型"听话"不掉的危险函数。
5. **扩展**：FOR UPDATE 锁读（只读分析不该拿锁）+ 表白名单（只查业务库六表，越权/跨库拒）。

**只靠它够吗？不够，所以配了第二道防线（Day1 只读账号）**：
- 校验闸是"确定性"的，但任何解析器都有边界 case（方言新特性、未知绕过）
- `nl2sql_ro` 只有 SELECT——即使恶意 SQL 穿过四道闸，MySQL 权限层兜底拒绝（ERROR 1142，今日实测）
- 我的完整答案：**AST 白名单（默认拒绝）+ DB 最小权限（即使漏了也写不进）+ 双重审计（闸层 reason + 执行层 SQL 原文）**

**追问"你敢让 LLM 生成的 SQL 直接跑吗"**：不敢。我做了两层——第一层 AST 确定性校验，第二层只读账号；而且执行层有 3s 超时 + 行数/字节上限三重资源约束，慢查询拖不垮服务。

## 五、欠账清单

- [x] 今日 Gate：攻击 20/20 拦截 0 逃逸 ✅ + 合法查询 0 误拦 ✅ + 超时/行数单测绿 ✅ + CI 接入 ✅
- [ ] 无新增欠账
- [ ] W23 遗留"40 并发 P95 达标"评估窗口延续（w23_report §9）

## 六、明日预告（W24 Day3 生成链路 v1 与基线）

- `prompts.py` v1 模板（全六表 schema + 5 条 few-shot）
- `graph.py` NL2SQL 子图（generate → validate → execute → format，LangGraph StateGraph）
- `router.py` `POST /api/data/query`（JWT + `data:nl2sql` 权限）
- 评测集 v1 50 条（单表 30 / join 20）+ `eval_nl2sql.py` execution accuracy 脚本
- mock 全链路验证 → real 基线 50 条（glm 免费额度）
- 今日成果直接复用：validator 已就位（graph 的 validate 节点）、executor 已就位（graph 的 execute 节点）
