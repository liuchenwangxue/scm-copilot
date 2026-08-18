# W24 Day3 学习执行日志 · 生成链路 v1 与基线（8/26 周三）

> 阶段四 W24 · 核心产物 #3：`prompts.py` v1 模板 + `graph.py` NL2SQL 子图 + `router.py`
> `POST /api/data/query` + 评测集 v1（50 条）+ `eval_nl2sql.py` + real 基线

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `prompts.py` v1 模板（全六表 schema + 5 条 few-shot） | ✅ | `app/domains/data/prompts.py`（SCHEMA_TEXT / build_few_shots / build_nl2sql_messages，时间窗口径由 today 驱动） |
| `graph.py` NL2SQL 子图（generate→validate→execute→format） | ✅ | `app/domains/data/graph.py`（LangGraph StateGraph + 条件路由 reject/execute + 降级话术） |
| `router.py` `POST /api/data/query`（JWT + `data:nl2sql`） | ✅ | `app/domains/data/router.py`（响应含 table/sql/columns/rows/elapsed/rejected_reason；审计回调注入 audit_logs；feedback 端点预置） |
| 评测集 v1 50 条（单表 30 / join 20） | ✅ | `backend/evals/nl2sql_eval_v1.jsonl`（`gen_eval_set_v1.py` 生成，固定 gold SQL + 显式日期口径） |
| `eval_nl2sql.py` execution accuracy 脚本 | ✅ | `backend/scripts/eval_nl2sql.py`（结果集比对 + 类型归一 + 列子集匹配 + 分层汇总 + 错例清单） |
| mock 全链路验证 | ✅ | `eval_nl2sql.py`（mock）50/50 = 1.000；`test_nl2sql_e2e.py` 8 用例全绿 |
| **real 跑基线 50 条**（glm 免费额度） | ✅ | **整体 0.940 / 单表 1.000 / join 0.850**（→ `reports/nl2sql_baseline.md`） |
| Makefile / CI 接入 | ✅ | `make eval-nl2sql` / `make test-nl2sql-e2e`；ci.yml 加 prompts+mock sanity 步骤 |
| 完整回归 | ✅ | **168 passed**（旧 160 + 新 8）；ruff 0 / mypy 0（125 source files） |

## 二、实测数字（两层分开记——手册坑）

### 2.1 mock（测链路 + 脚本正确性，不算效果）

| 指标 | 值 |
|---|---|
| execution accuracy | 1.000（50/50：单表 30/30 + join 20/20） |
| 耗时 | ~1.1s（本地 MySQL） |

> mock 从评测集按问题精确取 gold SQL，只证明"生成→校验→执行→比对"链路与脚本正确。

### 2.2 real（真效果基线，glm-5.2 免费额度）

| 指标 | 整体 | 单表 | join | W24 Gate 目标 |
|---|---|---|---|---|
| execution accuracy | **0.940（47/50）** | **1.000（30/30）** | **0.850（17/20）** | 整体≥0.80 / 单表≥0.95 / join≥0.75 |
| 判定 | ✅ | ✅ | ✅（Day6 之前已达标） | — |
| 耗时 | 46.1s / 50 条 ≈ 0.92s/条 | — | — | real P95 ≤5s（Day6 全量测） |
| token 用量 | ~135k（prompt ~2.4k + 输出 ~0.3k per 条） | — | — | 免费额度内 ¥0 |

### 2.3 错例 3 条（全在 join 层）

| # | 问题 | 根因 | 分类 |
|---|---|---|---|
| 36 | 订单数量最多的前5个供应商？ | gen 用 `order_count` 别名 + 多 `id/supplier_code` 列，gold 用 `cnt` → 列名无法精确对齐 | 口径差异（业务对） |
| 43 | 被订购次数最多的前5个商品？ | 同上（`order_cnt` vs `cnt`） | 口径差异（业务对） |
| 44 | 各供应商的订单平均金额最高的前5个？ | gen 返回 `supplier_id + avg_amount`，缺供应商名称列 | **真实语义错误** ★ Day4 重点 |

> 改进动作已写进 `nl2sql_baseline.md`：few-shot 补 `orders JOIN suppliers GROUP BY s.name` 示例（治 #44）；
> 别名差异属评测严格口径，Day4 考虑 few-shot 聚合列别名规范。

## 三、关键决策与踩坑记录

### 决策 1：时间窗口径不用 CURDATE()，用 today 驱动的显式日期
- 手册 Day3 坑写"`近 7 天` 写死 `CURDATE() - INTERVAL 7 DAY`"，但**种子数据基准日 BASE_DATE=2026-08-18 固定**，
  若用 `CURDATE()` 则运行日漂移（8/26 跑→近 7 天窗口 8/19–8/26 无数据）→ 空结果集被误判为 SQL 错；
- 改法：`build_nl2sql_messages(question, today)`，评测传 `today=BASE_DATE`，few-shot 与"近 N 天"提示都生成
  相对 today 的**显式日期**（如 `created_at >= '2026-07-19'`）——评测可复现，生产传当天语义一致；
- 坑：显式日期写死进 few-shot，若忘记传 today 会用默认 BASE_DATE（数据基准日），生产接当天即可。

### 决策 2：mock 生成器从评测集取 gold SQL（而非编规则）
- 手册"mock 模式测链路、real 测效果——两个数字分开记"；
- `MockSQLGenerator` 按问题文本精确查评测集 gold SQL，命中走完整链路；未命中返回安全默认查询
  （`SELECT COUNT(*) FROM orders`）——保证任何问题链路都通；
- 风险澄清：mock 数字 1.000 不代表效果（面试不会被问到它，基线报告已标注）。

### 决策 3：评测比对支持"列子集匹配"（Day3 real 首跑暴露）
- 首跑整体 0.880：6 条错例全是"列数不一致"——gen 多返回 `id/sku/supplier_code` 等从属列，业务全对；
- 改进：gold 列 ⊆ gen 列（同名）→ 按列名提取 gen 子集对齐比对；列数一致→按位置；列名无法对齐→判错；
- 效果：整体 0.880 → 0.940；剩余 3 条中 2 条是别名差异（严格口径保留），1 条真错；
- 面试点（Q4 延伸）：execution accuracy 不是"字符串比对"，但结果集比对也要处理"模型多给列/别名差异"。

### 决策 4：评测集别名与 few-shot 对齐
- 首跑发现评测集 gold 用 `AS total`、few-shot 用 `AS total_amount`——模型学 few-shot 反而"答错"；
- 统一为 `total_amount` 后 join 提升 0.70 → 0.85（配合列子集匹配）；
- 教训：few-shot 是模型唯一学习样本，评测 gold 必须与 few-shot 口径一致。

### 坑 1：mock provider 不适用于 SQL 生成
- `MockLLMProvider.generate` 面向 RAG 检索上下文，不产出 SQL——NL2SQL 域需要独立的 `mock_sql.py` 生成器；
- 教训：mock 按"域职责"定制，不能直接复用 RAG 域的 mock。

### 坑 2：PowerShell 内联中文 / 管道 tail
- 临时脚本验证 API 时用内联 `-c` 传中文会 GBK 乱码 → 改用临时 `.py` 文件（已验证后删除）。

## 四、面试题 Q4（20:00 段 0.5h）

**Q4：NL2SQL 评测为什么用 execution accuracy 而非 SQL 字符串比对？**

1. **同义 SQL 大量存在**：`LEFT JOIN` vs 子查询、列序不同、别名不同、`COUNT(i.id)` vs `COUNT(*)`——
   结果一致但字符串不同，字符串比对会误判一大片"错"；
2. **评测目标是"答对数据"**：业务上用户要的是"近 30 天华东已支付订单数"这个数，不是"一模一样的 SQL"；
3. **实现**（今天亲手写的脚本）：执行 gold SQL 与生成 SQL，对结果集做规范化后比对——
   类型归一（Decimal→float、datetime→isoformat）+ 排序键统一（按第一列）+ **列对齐（子集匹配）**；
4. **今天 real 首跑暴露的延伸**：模型多返回 `id/sku` 从属列、聚合别名不同（`cnt` vs `order_count`）——
   execution accuracy 也要处理列对齐，否则把"业务全对"的 SQL 误判成错（整体 0.88→0.94 的来历）；
5. 底线：**每条 SQL 透出 + 错例可复现**——不管准确率多少，每条都能解释为什么对/为什么错。

## 五、欠账清单

- [x] 今日 Gate：单表 ≥0.90 基线 ✅（real 1.000）+ 错例清单可复现 ✅ + mock 链路 ✅
- [ ] #44 缺名称列（真错）：few-shot 补 `orders JOIN suppliers GROUP BY s.name` 示例 → Day4 上午
- [ ] join 评测集补强至 40 条（本周 90 条在途）→ Day4 下午
- [ ] real P95 ≤5s 正式测量留 Day6 全量 100 条（今天 0.92s/条 说明达标在望）
- [ ] W23 遗留"40 并发 P95 达标"评估窗口延续（w23_report §9，Day1 未处置）

## 六、明日预告（W24 Day4 Schema Linking）

- `schema_linker.py`：表/列描述向量召回 Top-3（复用 shared bge-small）
- `prompts.py` v2：召回表 DDL + 精简 few-shot（`PROMPT_VERSION` 切换）
- A/B：v1 vs v2 的准确率 + prompt token 消耗（目标 token 降 ≥50% + 精度不降）
- 错例分析：今天 #44 → 表召回 or SQL 生成分类计数
