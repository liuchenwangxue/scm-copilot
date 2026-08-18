# NL2SQL Day3 基线报告：execution accuracy v1

> 日期：2026-08-26（W24 Day3）｜ 依据：《W24学习执行手册》Day3 + 《03》1.2 节
> 评测集：`backend/evals/nl2sql_eval_v1.jsonl`（50 条：单表 30 / join 20）
> 脚本：`backend/scripts/eval_nl2sql.py`（--out reports/nl2sql_eval_real_day3.json）
> 基准日：2026-08-18（= seed BASE_DATE，固定口径防运行日漂移）

---

## 1. 基线数字（real，glm-5.2 免费额度）

| 指标 | 整体 | 单表 | join | 达标线（W24 Gate） |
|---|---|---|---|---|
| execution accuracy | **0.940（47/50）** | **1.000（30/30）** | **0.850（17/20）** | 整体 ≥0.80 / 单表 ≥0.95 / join ≥0.75 |
| 判定 | ✅ | ✅ | ✅ | **已超 Day6 Gate 目标** |
| 耗时 | 46.1s / 50 条 | — | — | real P95 ≤5s（待 Day6 全量测） |

> mock 链路验证：50/50 = 1.000（mock 只测链路与脚本正确性，**不算效果数字**——手册坑位）。

## 2. 错例清单（3 条，全部在 join 层）

| # | 问题 | gold 列 | gen 列 | 根因分类 |
|---|---|---|---|---|
| 36 | 订单数量最多的前5个供应商？ | `[name, cnt]` | `[id, supplier_code, name, order_count]` | **别名+辅助列差异**（业务正确） |
| 43 | 被订购次数最多的前5个商品？ | `[name, cnt]` | `[id, sku, name, order_cnt]` | **别名+辅助列差异**（业务正确） |
| 44 | 各供应商的订单平均金额最高的前5个？ | `[name, avg_amount]` | `[supplier_id, avg_amount]` | **真实语义错误**（缺名称列） |

### 分析
- **#36/#43 实为评测口径问题，非模型错**：
  - gen SQL 返回了正确供应商/商品名 + 正确聚合值，只是额外带 `id/sku/supplier_code` 从属列，且计数别名用 `order_count`/`order_cnt` 而非 `cnt`；
  - 评测脚本的"列名子集匹配"要求列名精确一致才对齐，别名不同 → 无法语义对齐 → 判错；
  - 处理：已在评测脚本支持"gold 列 ⊆ gen 列（同名）"提取比对；别名差异保留判错（严格口径），Day4 可在 few-shot 增加"聚合列别名规范"引导。
- **#44 是真错（★ Day4 重点）**：
  - 模型只返回 `supplier_id + avg_amount`，没有供应商名称——用户问"各供应商"却看不到名字；
  - 根因：prompt 的 join 聚合 few-shot 只有 `orders+order_items` 与 `orders GROUP BY region`，缺"orders+suppliers JOIN 后按名称分组"的示例 → 模型选了最省事路径（单表按 supplier_id 分组）；
  - 改进方向（Day4 schema linking + few-shot 补强）：加 1 条 `orders JOIN suppliers GROUP BY s.name` few-shot；表描述补充"供应商名称=业务主键，展示须带 name"。

## 3. 评测方法论要点（面试 Q4 素材）

**为什么 execution accuracy 不做字符串比对？**
- 同义 SQL（`LEFT JOIN` vs 子查询、列序不同、别名不同、`COUNT(i.id)` vs `COUNT(*)`）结果一致但字符串不同，字符串比对会大量误判为错；
- 评测目标是"答对数据"不是"写出一模一样的 SQL"。

**Day3 实测暴露的第三个维度：列对齐**
- 字符串比对之外，结果集比对也要处理"模型多返回辅助列"（`id/sku`）与"别名差异"（`cnt` vs `order_count`）；
- 实现：gold 列名 ⊆ gen 列名 → 按列名提取对齐比对（多列不误判）；列数一致 → 按位置比对；列名无法对齐 → 判错（结构差异不可语义对齐）。

**规范化规则（脚本 `_results_equal`）**
1. 类型归一：Decimal→float、datetime→isoformat（executor 层已完成）；
2. 排序键统一：按第一列排序（NULL 放最后）；
3. 列对齐：子集匹配 > 位置匹配 > 判错。

## 4. mock vs real（手册坑位：两个数字分开记）

| provider | 用途 | 数字 | 说明 |
|---|---|---|---|
| mock | 测链路 + 脚本正确性（CI 可跑） | 1.000 | 从评测集查 gold SQL，不反映模型效果 |
| real | 真效果基线 | 0.940 | glm-5.2，free tier；错例可复现 |

> 生产/演示走 real；评测集 mock 模式下 CI 全绿（`make eval-nl2sql`）。

## 5. 耗时与成本

- 50 条顺序执行耗时 46.1s → 单条约 0.9s（不含评测脚本 overhead），P95 留 Day6 全量 100 条测；
- real 用量：50 条 × 1 轮 ≈ prompt 全 schema ~2.4k token + 输出 ~0.3k token ≈ 135k tokens，glm 免费额度内，未产生实际费用。

## 6. Day4 衔接

- [ ] few-shot 补强：`orders JOIN suppliers GROUP BY s.name` 示例（治 #44）；
- [ ] schema linking 换 v2 prompt（token 降 ≥50%，准确率不降）；
- [ ] join 评测集补强至 40 条（本周 90 条在途）；
- [ ] 错例按"表召回 or SQL 生成"分类计数。
