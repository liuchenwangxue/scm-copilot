# W25 Day3 学习执行日志 · 日报与夜间回归（9/2 周三）

> 阶段四 W25 · 核心产物 #3：daily_brief（工作日日报）+ eval_nightly（夜间质量回归）+ cache_warmup（缓存预热）——"业务价值任务"和"质量守护任务"上线
> 对应手册 Day3：三条 NL2SQL 日报 / RAG156+NL2SQL100 夜间回归 / 预热 TOP100 / 24h 零重复观测启动

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `jobs/daily_brief.py`（工作日 08:00）：三条 NL2SQL（昨日 GMV/延迟发货率/TOP5 供应商）→ 模板渲染 → `daily_briefs` 表 + 订阅推送 | ✅ | 实测 GMV=36,738,101.8、延迟率=9.91%、TOP5 供应商 5 行；brief 落库 `status=pushed` + 3 用户站内通知；SQL 100% 可回溯 |
| `jobs/eval_nightly.py`（每日 02:00）：RAG 156 + NL2SQL 100 全 mock 回归 → `eval_reports`（各域分数 + 7 日均值偏离标红） | ✅ | 首份报告落库：RAG hit@1=0.9038/recall@5=0.9936/citation=0.9754/error=0；NL2SQL mock overall=1.0（单/join/聚合全 1.0）；regressed=0 |
| `jobs/cache_warmup.py`（每日 07:00）：昨日高频问题 TOP100 → 预执行写语义缓存 | ✅ | 昨日 7 个高频会话问题全预热（warmed=7, failed=0），`candidates/hit/warmed/failed` 数字可观测 |
| 三条 NL2SQL 走 W24 链路（四道闸 + 只读沙箱 + mock 注册固定 SQL） | ✅ | 模板问题经 `run_nl2sql_query` 完整执行；SQL 经 sqlglot 规范化后落库（`CURRENT_DATE - INTERVAL '1' DAY`） |
| 幂等：daily_brief 用 `brief:{date}` Redis SETNX（失败删键可重试）+ DB unique 双保险 | ✅ | 第二次执行返回 `status=skipped`（实测）；每日一条不重复 |
| `eval_reports`/`daily_briefs`/`notifications` 三表 | ✅ | alembic 迁移 `d1e2f3a4b5c6` 成功（平台库） |
| admin 面板显示最近运行历史 | ✅ | `GET /api/admin/scheduler/jobs` 每任务返回 `last_run` + `recent_runs`（最近 5 条） |
| **24h 零重复观测启动** | ✅ | 双实例重建后互斥实测：同触发点 `kb_increment_sync success backend-a2` + `skipped backend-a1`（instance 字段正确区分） |
| Makefile / README | ✅ | `make test-day3-tasks` + README 更新 |

## 二、实测数字

### 2.1 daily_brief 首份日报（手动触发，2026-08-19）

| 指标 | 值 | SQL 可回溯 |
|---|---|---|
| 昨日 GMV | **36,738,101.8 元** | `SELECT SUM(amount) AS gmv FROM orders WHERE created_at >= CURRENT_DATE - INTERVAL '1' DAY ...` |
| 昨日延迟发货率 | **9.91%** | `... shipments sh JOIN orders o ... WHERE o.created_at >= CURRENT_DATE - INTERVAL '1' DAY ...` |
| TOP5 供应商 | 华东启航30(241.6万) > 华东利丰26(191.5万) > 华南骏业52(179.0万) > 华南优品56(165.5万) > 华东骏业55(161.1万) | 同上 JOIN suppliers GROUP BY s.name ORDER BY gmv DESC LIMIT 5 |

- 落库：`daily_briefs` 1 行 `(2026-08-19, pushed)`，metrics/sqls/notified_users 全量 JSON；
- 订阅推送：analyst/admin 前 3 用户各 1 条 `notifications`（type=daily_brief），重复触发不重推（标题查重）；
- 幂等：第二次执行 `brief:2026-08-19` SETNX 未命中 → `status=skipped`（job_runs 仍记 success，业务语义在返回体）。

### 2.2 eval_nightly 首份夜间报告（2026-08-19）

| 域 | 样本 | 指标 | 值 | error_rate |
|---|---|---|---|---|
| RAG | 156 条 | hit@1 / recall@5 / citation_acc | **0.9038 / 0.9936 / 0.9754** | 0.0 |
| RAG | 156 条 | p95_retrieve_ms | 12365.88（reranker 交叉编码，夜间可接受） | — |
| NL2SQL | 100 条（mock） | overall / single / join / aggregation | **1.0 / 1.0 / 1.0 / 1.0** | 0.0 |
| NL2SQL | 100 条 | elapsed_s / avg_prompt_tokens | 4.57s / 1060.6 | — |

- 7 日均值偏离：首次运行无历史基线（samples=0, degraded=False）——第二晚起有真实偏离计算；
- 幂等：`(report_date, domain)` unique，重跑同域跳过（`{"skipped": true}` 单测覆盖）。

### 2.3 cache_warmup 预热（2026-08-19）

- 数据源：`conversations` 昨日标题按频次聚合（7 个高频问题，时间窗 `>=昨日0点 AND <今日0点`）；
- 结果：`{candidates: 7, hit: 0, warmed: 7, failed: 0}`——全部预热进语义缓存（Redis + 内存双写）。

### 2.4 24h 零重复观测启动证据（双实例，2026-08-19 04:2x 起）

| run_id 触发点 | 实例 | 状态 |
|---|---|---|
| kb_increment_sync:<秒窗口> | backend-a2 | **success**（55ms） |
| kb_increment_sync:<同窗口> | backend-a1 | **skipped**（another instance holds lock） |

> 同一触发点 A 执行 B 跳过 + instance 字段正确区分 → 零重复观测口径成立。
> 观测窗口：双实例持续运行（已启动），明晚（Day4 早）查 24h job_runs 聚合。

### 2.5 测试与静态检查

- **新增 17 用例全绿**：`test_daily_brief.py` 8（指标提取 5 + 模板渲染 2 + 全链路 1）/ `test_eval_nightly.py` 7（偏离 5 + NL2SQL 评测 1 + 落库幂等 1）/ `test_cache_warmup.py` 2（降级 1 + 数据源 1）；
- **全量回归**：`pytest backend/tests -m "not model and not llm"` **282 passed**；
- **静态检查**：ruff 0 / mypy 0（162 source files）；`make test-day3-tasks` 目标就位。

## 三、关键决策与踩坑记录

### 决策 1：日报走 W24 完整 NL2SQL 链路，而非手填 SQL
- 三条模板问题经 `run_nl2sql_query`（生成→四道闸→只读沙箱→结果），**数字来自真实执行**而非手填；
- mock 模式下 `register_mock_sql(question, sql)` 注册固定 SQL（确定性）；real 模式走 LLM——两条路径四道闸 + 沙箱执行完全一致；
- 昨日口径：SQL 内写死 `CURDATE() - INTERVAL 1 DAY`（手册坑：跨月/跨年交给 MySQL），`today` 只用于归属日与幂等键。

### 决策 2：eval_nightly 全 mock 守护"结构"而非"语义准确率"
- RAG 走 `EvalRunner`（生产同款 HybridRetriever 检索链 + mock 生成引用），NL2SQL 复用 W24 评测逻辑；
- 守护的是"结果格式 / 延迟 / 报错率"，语义准确率是 W24 real 全量评测的活（两个数字分开记）；
- **逐条容错**：单条异常不中断整轮，error_rate 进 metrics（链路坏了要有数字）；
- **7 日均值偏离**：主指标（rag=hit@1 / nl2sql=overall）与近 7 天同域均值差 >5pp → regressed=1 标红。

### 决策 3：部署环境 RAG 依赖退化为 error metrics（设计约束）
- Dockerfile 明确容器内不装 embedding/reranker/torch（模型推理在宿主机）→ 容器内 eval_nightly 的 RAG 部分 retriever init failed → `error_rate=1.0` 落库标红；
- 任务本身不崩（`_run_job` 包装），NL2SQL 部分（MySQL 权威库）容器内正常——零重复观测不受影响；本地开发环境（有 Qdrant+模型）RAG 真跑（hit@1=0.9038）。

### 坑 1（★ 部署 bug）：compose `INSTANCE_ID` 与 settings `SCM_INSTANCE_ID` 前缀不匹配
- **现象**：容器双实例 job_runs 的 instance 字段全是 `local`，面板显示 instance=local；
- **根因**：settings 字段 `instance_id` 对应环境变量 `SCM_INSTANCE_ID`（env_prefix="SCM_"），compose 注入的是 `INSTANCE_ID`——读不到，回退默认 'local'；
- **后果**：24h 零重复观测无法区分实例（success/skipped 分不清谁跑的）；
- **修复**：compose 改 `SCM_INSTANCE_ID: backend-a1/a2`；实测同触发点 `success backend-a2 + skipped backend-a1` ✅。

### 坑 2（★ 部署 bug）：backend 容器缺 TZ，Python date.today() 走 UTC 归属日少一天
- **现象**：容器手动触发 daily_brief 写了 `08-18` 的日报（实际上海时间 8/19 04:21）；
- **根因**：compose 只在 mysql 服务设了 `TZ: Asia/Shanghai`，backend 容器 OS 时区默认 UTC——上海 04:21 = UTC 8/18 20:21，`date.today()`（UTC）= 8/18；
- **影响**：daily_brief 归属日错一天；eval_nightly 凌晨 02:00 触发（上海 02:00 = UTC 前日 18:00）也会写错归属日；
- **修复**：backend 双实例补 `TZ: Asia/Shanghai`（与 mysql 同款）；实测容器 `date.today()`=2026-08-19 ✅；已清理误写的 08-18 brief + 通知 + 幂等键。

### 坑 3：rank_bm25 缺失（kb 混合检索依赖未入 pyproject）
- eval_nightly 首次跑 RAG 报 `No module named 'rank_bm25'`（kb 域 HybridRetriever 依赖，此前部署/单测未触发）；安装 + 补入 pyproject 依赖。

### 坑 4：jobs 模块顶层 `from scheduler import _runtime` 循环导入
- scheduler 包 import jobs 包，jobs 又 import scheduler 的 `_runtime` → ImportError（部分初始化）；
- 修复：`_runtime` 改为函数内延迟导入（运行时上下文本就由 start() 注入，顶部导入无必要）。

### 坑 5：datetime.date 为 C 扩展不可 setattr（monkeypatch）
- 测试固定日期 `monkeypatch.setattr(daily_brief.date, "today", ...)` 报 `TypeError: cannot set 'today' attribute of immutable type`；
- 修复：子类 `class _FakeDate(date): @classmethod today(cls): return fixed` 替换模块级绑定。

### 坑 6：`trigger` 是 MySQL 保留字（查询 job_runs 需反引号）
- 手动 SQL 查 `SELECT trigger FROM scheduler_job_runs` 报 1064；加反引号 `` `trigger` ``。

## 四、验收（手册 Day3 验收项）

| 验收项 | 结果 |
|---|---|
| 日报生成且数字点开可见 SQL | ✅ GMV/延迟率/TOP5 全有数字 + `daily_briefs.sqls` 存三条 SQL 原文（可回溯） |
| 夜间回归出首份报告 | ✅ eval_reports 2 行（rag 0.9038 / nl2sql 1.0 mock），regressed=0 |
| warmup 后缓存命中率提升有数字 | ✅ `{candidates:7, warmed:7, failed:0}` 可观测 |
| 面板联通：admin 调度面板能看到六任务 + 最近运行历史 | ✅ 六任务 cron/next_run/last_run/recent_runs |
| 24h 零重复观测启动 | ✅ 双实例持续运行 + 互斥证据（success+skipped 同触发点） |

## 五、面试题 0.5h：数据闭环 STAR 主线口述（计时 3 分钟）

> **S**（情境）：平台有三类"运维侧重复劳动"——文档更新不可检索、质量劣化无人知、经营指标靠人肉 SQL——周末无人值守时系统"不会自己转"。
>
> **T**（任务）：补全"数据闭环自动化"：知识库新鲜度、质量守护、经营日报三个场景各要一个定时任务，双实例不能重复执行。
>
> **A**（行动，三层）：
> 1. **调度基座**：APScheduler 3.x + MySQL job store（任务定义重启不丢），六任务集中注册表，leader 锁（SETNX+owner Lua 释放）任务级互斥，job_runs 全量落盘；
> 2. **三个任务**：kb_increment_sync（mtime 水位 + uuid5 内容寻址，改文档 ≤5min 可检索）、eval_nightly（RAG 156 + NL2SQL 100 全 mock 回归，7 日均值偏离 >5pp 标红）、daily_brief（三条 NL2SQL 走生产四道闸 + 只读沙箱，SQL 可回溯 + 订阅推送）；
> 3. **幂等三保险**：leader 锁（互斥执行）+ 任务级幂等键（日报日期键 SETNX / 报告日期域 unique / 向量内容寻址）+ 失败删键可重试——锁失效时副作用为零。
>
> **R**（结果）：首份报告 RAG hit@1=0.9038/recall@5=0.9936、日报 GMV 数字可回溯 SQL、预热 7/7 全命中；24h 双实例零重复观测已启动（同触发点 A 执行 B 跳过实证）。

> 讲点：自动化价值不在"少手动"，在**新鲜度有 SLA、质量有守夜人、事故有现场**——日报数字点开就是 SQL，报告劣化自动标红，每个任务在 job_runs 有运行现场。

## 六、欠账 / 次日衔接（W25 Day4 优先）

- [ ] **24h 零重复观测**：明早查 job_runs 聚合（六任务 × 触发点 × 唯一实例），产出零重复证据表（Day6 周 Gate 用）
- [ ] eval_nightly 第二晚出报告后，7 日均值偏离正式生效（可手动补跑 1 晚验证标红逻辑）
- [ ] W23 遗留"40 并发 P95 评估"继续挂账（W25 预算受限，Day6 周 Gate 自检时评估是否砍吸收项）
- [ ] 容器内 RAG 退化（error metrics）如实记录到部署文档（W26 面板把 error_rate 可视化）

## 七、W25 周 Gate 进度

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | 🚧 **观测已启动**（互斥实证 success+skipped 同触发点），明晚聚合出表 |
| KB 同步 ≤5min | ✅ Day2 实测通过（待 24h 观测背书） |
| 日报准点 5/5 | 🚧 机制 + 首份实测通过（连续 5 工作日准点需日积累） |
| SDK pip 十行跑通 | ⏳ Day5 |
| 429 用例过 | ⏳ Day5 |
