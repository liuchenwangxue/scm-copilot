# W24 Day6 学习执行日志 · 呈现与全量评测（8/29 周六）

> 阶段四 W24 · 核心产物 #6：`insight.py` 结果洞察 + 语义路由 data 分支 + 100 条三层评测 + R1 检查点
> 评测：real v2 全量 100 条 ｜ P95 38.9ms ｜ 洞察数字溯源双保险抽查 3 用例全 PASS

## 一、今日目标与达成

| 目标（手册 Day6） | 状态 | 证据 |
|---|---|---|
| `insight.py` 结果洞察（LLM 对结果集生成 ≤3 条摘要，禁止编造数字） | ✅ | `app/domains/data/insight.py`（prompt 给结果集 JSON 要求引用行 + **数字溯源校验**双保险） |
| 洞察接入编排服务 + API（`/api/data/query` 返回 `insights`） | ✅ | `service.py` `run_nl2sql_query`（多轮消解→子图→洞察 统一编排）；router 复用同一服务 |
| 语义路由 data 分支打通（查数→NL2SQL 域） | ✅ | `config.py` data 原型/阈值(0.80) + `semantic_router.py` 规则层 4 组高置信模式 |
| 对话入口流式返回 `data_table` 事件（SSE） | ✅ | `kb/router.py` `/api/kb/chat` data 分支：权限二次校验 → run_nl2sql_query → SSE `data_table` |
| 评测集扩满 100 条三层（单表 40 / join 40 / 聚合 20） | ✅ | `gen_eval_set_v1.py` 单表补强 10 条；mock 全量 100/100 |
| **real 全量 100 条**（v2 Schema Linking）：分层准确率 + P95 + token/条 | ✅ | `eval_nl2sql.py --prompt-version v2` → **整体 0.970**（见 §二） |
| 修复评测脚本多列分组排序 bug | ✅ | `eval_nl2sql.py` `_sort_key` 改整行排序（多列分组不再误判） |
| 修复评测集质量问题 2 条（并列截断不稳定 / 口径不对齐） | ✅ | #64（delay_days 并列 15 LIMIT5 截断不确定→改 COUNT 题）、#78（问题没说"前3"却 LIMIT3→去 LIMIT）、#96（措辞歧义→换题） |
| R1 检查点决策（对照《04》第 2 节） | ✅ | **整体 ≥0.80 → 过，进 W25**（见 §三） |
| 回归 | ✅ | **231 passed**；ruff 0 / mypy 0（139 source files） |

## 二、实测数字（real v2 全量 100 条，周 Gate 验收）

**评测集**：`backend/evals/nl2sql_eval_v1.jsonl`，100 条三层（单表 40 / join 40 / 聚合 20），固定 seed 可复现。
**模型**：kimi-k2.7-code ｜ 基准日 2026-08-18 ｜ Schema Linking v2 注入。

| 指标 | 数值 | W24 周 Gate | 判定 |
|---|---|---|---|
| **execution accuracy 整体** | **0.970（97/100）** | ≥0.80 | ✅ |
| 单表 | 0.975（39/40） | ≥0.95 | ✅ |
| join | 0.950（38/40） | ≥0.75 | ✅ |
| 聚合 | **1.000（20/20）** | ≥0.60 | ✅ |
| 攻击拦截（Day2 复用） | 20/20 | 20/20 | ✅ |
| **real P95 执行耗时** | **38.9ms**（max 72.3ms） | ≤5s | ✅ |
| prompt token/条 | 495.6（v2 召回注入） | 较 v1 降 ≥50%（Day4 已验 53.5%） | ✅ |
| SQL 100% 透出 | ✅ 每条响应含 sql + 审计 | 可审计可纠错 | ✅ |

**mock 全量**（测链路）：100/100 = 1.000（不算效果，仅证明评测脚本 + graph 链路正确）。

### 错例 3 条（真实模型错误，可解释）

| # | 层 | 问题 | 根因 | 分类 |
|---|---|---|---|---|
| 38 | single | 近30天发货的延迟订单有多少？ | gen 用 `o.created_at` 时间窗 + JOIN orders，gold 用 `sh.shipped_at`——**时间列选择错误**（"发货"应用发货时间） | 真实语义错误 |
| 61 | join | 各类目商品的销量最高的前5个商品？ | gen 按 `p.category, p.name` 分组（gold 只按 name）且无 LIMIT——**分组粒度过细 + 漏 TOP-N** | 真实语义错误 |
| 67 | join | 近30天发货订单的总金额是多少？ | 同上时间列：gen 用 `o.created_at`，gold 用 `sh.shipped_at` | 真实语义错误 |

> 错例归因：3 条全在"发货时间窗"（shipments.shipped_at vs orders.created_at）与"分组粒度"两处，
> few-shot 里没有"按发货时间过滤"示例——W25 eval_nightly 的优化抓手（补 2 条 few-shot 可救回）。
> 评测集本身质量问题（#64 并列截断 / #78 口径 / #96 措辞）已当日修复并重跑，**非刷数据**。

### 洞察摘要抽查（禁止编造数字双保险，real 3 用例）

| 用例 | 返回条数 | 数字溯源 | 样例 |
|---|---|---|---|
| 各区域的订单总金额？ | 2 | PASS | "华东区域订单总金额最高，达7.93亿元，显著领先其他区域" |
| 订单数量最多的前5个供应商？ | 3 | PASS | "订单量TOP1供应商为华东宏图44有限公司，共计361单" |
| 各类目商品的销售金额？ | 2 | PASS | "机械配件类目销售金额最高，达3.51亿元" |

> ★ 当日实测修复：供应商名称含编号（"华东宏图44有限公司"），LLM 引用公司名时其中的数字（44）
> 被首版溯源逻辑误杀 → 修复为"业务字符串中的数字可溯源、日期字符串不溯源"（`_result_numeric_values`）。

## 三、R1 检查点决策（对照《04_ADR与风险应对》第 2 节 R1）

- **现状**：整体 0.970 / 单表 0.975 / join 0.950 / 聚合 1.000
- **R1 判定阈值**：整体 ≥0.80 → 过，进 W25 ✅
- **决策**：**通过 R1 检查点，进入 W25**。剩余 3 条错例非评测集问题（真实模型时间列/分组粒度错误），
  已给 W25 eval_nightly 明确优化抓手（few-shot 补发货时间窗示例）；聚合层 1.000 无需特别干预。
- **记录**：如实记录分层指标 + 错例归因（面试讲边界：0.97 不是 1.0，3 条错在可解释的时间列语义上）。

## 四、关键决策与踩坑记录

### 决策 1：洞察摘要"禁止编造数字"用 prompt + 数字溯源双保险
- **prompt 层**：给结果集 JSON（前 10 行），硬性规则"只允许引用结果集中出现过的数值"；
- **校验层**（确定性兜底，防 LLM 不听话）：`verify_insight_digits` 把摘要中的每个数字与结果集
  数值单元格比对（支持 %/万/千/亿 量纲换算 + 1% 四舍五入容差），查无出处的整条丢弃；
- **效果**：真实抽查 3 用例 7 条摘要全 PASS，编造数字（如金额 99999999）被确定性拦截；
  这比只靠 prompt 更可信（面试点：prompt 是第一层，确定性校验是第二层，模型"不听话"也拦得住）。

### 决策 2：语义路由 data 分支规则层用"组合模式"而非裸关键词
- 手册坑：别用裸关键词（"采购金额超过多少必须招标采购"含"多少"但是制度问题）。
- 规则层只拦高置信组合：`延迟/发货 + 多少/占比`、`近N天 + 订单/发货 + 多少`、
  `各区域/各仓库 + 订单/金额/库存`、`TOP N + 供应商/商品`——防误杀 RAG 制度问（有单测回归）。

### 决策 3：编排服务 `run_nl2sql_query` 收口多轮消解→子图→洞察
- router（POST /api/data/query）与对话入口（kb/chat data 分支）复用同一实现，结果结构一致；
- 域间解耦：kb 只依赖 data 域的 service 公共 API（ADR-01"域间只经内部 API 通信"），不 import 内部模块。

### 坑 1：评测脚本 `_sort_key` 只按第一列排序 → 多列分组误判（★ 当日发现）
- "各区域各状态"（region,status）gold/gen 第一列相同但第二列顺序不同 → 被误判错；
- 修复：`_sort_key` 按**整行**排序（NULL 放最后），3 条聚合多列分组题全部由错转对；
- 教训：execution accuracy 的"排序键统一"要覆盖多列分组，否则合法 SQL 误杀。

### 坑 2：评测集并列截断不稳定（#64）
- "延迟发货天数最多前5" gold `ORDER BY delay_days DESC LIMIT 5`——delay_days 大量并列 15，
  LIMIT 截断的行不确定，gold/gen 各取 5 行不同 → 评测不稳定；
- 修复：改为确定性 COUNT 题（"延迟天数>10 的订单数"），保留 join 语义。

### 坑 3：评测集问题与 gold 口径不对齐（#78/#96）
- #78 问题没问"前3个"但 gold 却 `LIMIT 3` → 去 LIMIT（返回全部承运商）；
- #96 "各区域订单总金额的合计" 措辞歧义（模型理解为按区域分组）→ 换成无歧义的聚合题；
- 教训：评测集质量影响准确率数字的公信力——**宁可修题也不刷数**（R1 红线）。

### 坑 4：`contextlib.suppress` 不支持 `async with`（kb/chat data 分支）
- `async with contextlib.suppress(Exception): await _audit(...)` 报
  "suppress object does not support the async context manager protocol"；
- 修复：改 `with contextlib.suppress(Exception):`（suppress 是同步上下文管理器，包住 await 即可）。

### 坑 5：kb 域的 `_audit_sink(request)` 与 data executor 审计回调契约不同
- kb 已有 `_audit_sink(request)`（log_route 的同步 sink），data 域要求 `{event, sql, status...}` 异步回调；
- 修复：在 data 分支内联定义 `_nl2sql_audit_sink(event)` 适配 data 契约，避免误用 kb 的 sink。

## 五、面试题（0.5h）：完整口述 NL2SQL STAR 主线（《05》第 1 节，目标 3 分钟）

**S**：供应链平台要能"用自然语言查数据"，但两个致命问题：①模型生成的 SQL 可能写错 ②写对了也可能不安全。
**T**：从零建 NL2SQL 域——scm_biz 六表万级种子 + sqlglot 四道闸 + 只读沙箱 + Schema Linking +
错误自修复 + 结果洞察，配 100 条三层评测集。
**A**：
1. **安全**：四道闸（AST 确定性校验，攻击 20/20 拦截）+ `nl2sql_ro` 只读账号（ERROR 1142 兜底）+ 3s 超时/行数上限；
2. **效果**：Schema Linking 召回（bge 表列召回 Top-3，token 降 53.5% 且精度反升）+ 自修复（救回率 0.933）；
3. **可解释**：每条 SQL 透出可审计、修复轨迹可回放、洞察数字可溯源（禁止编造双保险）；
4. **评测**：100 条三层 execution accuracy（结果集比对非字符串比对），real 整体 0.970。
**R**：整体 0.970（单表 0.975 / join 0.950 / 聚合 1.000），P95 38.9ms，攻击 20/20；3 条错例归因到
时间列语义（发货时间 vs 创建时间），W25 eval_nightly 有明确优化抓手。

## 六、欠账清单

- [x] 今日 Gate：整体 ≥0.80 ✅ / 单表 ≥0.95 ✅ / join ≥0.75 ✅ / 聚合 ≥0.60 ✅ / 攻击 20/20 ✅ / P95 ≤5s ✅
- [x] 洞察 3 用例抽查全 PASS（数字溯源双保险验证）
- [x] 评测集质量问题 3 条修复 + 重跑（非刷数）
- [x] 回归 231 passed / ruff 0 / mypy 0
- [ ] W23 遗留"40 并发 P95 达标"评估窗口延续
- [ ] 错例 #38/#61/#67 优化抓手：few-shot 补发货时间窗示例（W25 eval_nightly 消化）
- [ ] 多实例会话持久化（进程内 LRU → MySQL conversations）→ W25

## 七、W25 衔接预告

| W25 主题 | 与本日的关系 |
|---|---|
| eval_nightly 夜间回归 | 100 条评测集 + 3 条错例抓手（few-shot 补发货时间窗）成夜间任务输入 |
| daily_brief 经营日报 | 洞察摘要链路可直接生成日报语句（数字可溯源） |
| feedback 回流 | `POST /api/data/query/{id}/feedback` 已预置（fb_type=sql），W25 进评测集增量 |
| 多实例会话持久化 | 进程内 LRU（session_ctx.py）临时方案 → W25 迁 MySQL conversations |

> W24 收官：业务库/四道闸/生成链路/Schema Linking/自修复多轮/洞察呈现 + 全量评测六件套全部就位。
> **0.970 不是终点——3 条错例可解释、评测集可重放、每条 SQL 可见**，这就是"可信的 NL2SQL"。
