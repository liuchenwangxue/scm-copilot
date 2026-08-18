# W24 周报告 · NL2SQL 数据分析域（8/24–8/30）

> 阶段四 SCM Copilot 第 2 周 ｜ 依据《W24学习执行手册》+《01》第三节 ｜ 周 Gate：execution accuracy 整体 ≥0.80 ✓
> **本周 Gate 判定：通过 → 进入 W25（调度域 + 开放能力 + 三吸收项）**

## 一、周 Gate 验收（全部达标）

| 验收项 | 目标 | 实测 | 判定 |
|---|---|---|---|
| execution accuracy **整体** | ≥0.80 | **0.970（97/100）** | ✅ |
| 单表 | ≥0.95 | 0.975（39/40） | ✅ |
| join | ≥0.75 | 0.950（38/40） | ✅ |
| 聚合 | ≥0.60 | **1.000（20/20）** | ✅ |
| 攻击用例拦截 | 20/20 | 20/20（0 逃逸） | ✅ |
| real P95 | ≤5s | **38.9ms** | ✅ |
| SQL 100% 透出 | 可审计可纠错 | 每条响应含 sql + 审计 | ✅ |

## 二、六件套核心产物（★ 缺一不可，全数就位）

| # | 产物 | 达标要求 | 实测证据 |
|---|---|---|---|
| 1 | ★ scm_biz 六表 + 只读沙箱 | 种子可重放；nl2sql_ro 写被拒 | suppliers 40/products 500/orders 10,000/items 34,934/shipments 6,951/inventory 500；`ERROR 1142` |
| 2 | ★ sqlglot 四道闸 | 攻击 20/20 拦截 0 逃逸；每闸单测分支 | `test_attack_cases.py` 20/20；validator 53 用例 |
| 3 | ★ 评测集 + execution accuracy | 100 条三层；结果集比对非字符串比对 | `nl2sql_eval_v1.jsonl` 100 条（单40/join40/聚合20） |
| 4 | ★ Schema Linking | 召回 ≥90%；token 降 ≥50%；精度不降 | 召回 1.000；token 降 53.5%；v2 精度 ≥ v1（1.000 vs 0.980） |
| 5 | ★ 自修复 + 多轮 | 救回 ≥50%；多轮 10 条过 8 | 救回 0.933；多轮 9/10 |
| 6 | ★ 洞察摘要 + 呈现 | ≤3 条；禁止编造数字 | `insight.py` 数字溯源双保险；SSE `data_table` 事件 |

## 三、分层准确率（real，kimi-k2.7-code，100 条）

| 层 | 条数 | 正确 | 准确率 | 周 Gate |
|---|---|---|---|---|
| 整体 | 100 | 97 | **0.970** | ≥0.80 ✅ |
| 单表 | 40 | 39 | 0.975 | ≥0.95 ✅ |
| join | 40 | 38 | 0.950 | ≥0.75 ✅ |
| 聚合 | 20 | 20 | **1.000** | ≥0.60 ✅ |

**错例 3 条**（全为真实模型错误，可解释，非评测集问题）：
1. #38 近30天发货的延迟订单 → 时间列选错（`shipped_at` vs `created_at`）
2. #61 各类目销量 TOP5 商品 → 分组粒度过细 + 漏 TOP-N
3. #67 近30天发货订单总金额 → 时间列选错（同上）

> 优化抓手（W25 eval_nightly 消化）：few-shot 补"按发货时间过滤"示例 2 条，可覆盖 #38/#67；
> #61 补"分组粒度"提示（按商品名而非类目+商品分组）。

## 四、token 与成本

| 版本 | avg prompt token/条 | 降幅 | 说明 |
|---|---|---|---|
| v1 全 schema 注入 | 1060.6 | — | Day3 基线 |
| v2 Schema Linking | 495.6 | **-53.3%** | Day6 全量（Day4 A/B 同口径 -53.5%） |

- real 采样次数：Day3 基线 50 条 + Day4 A/B 50 条 + Day6 全量 100 条（共 2 轮正式采样，符合 R7 预算纪律）；
- 本周 real 总消耗 ≤¥20 预算内（mock 开发零成本）。

## 五、安全侧（纵深防御叙事）

| 层 | 拦截能力 | 证据 |
|---|---|---|
| 闸1–闸4 + 扩展（AST 白名单） | 堆叠/写操作/伪装嵌写/危险函数/FOR UPDATE/越权表 | 攻击 20/20 |
| 只读账号 `nl2sql_ro` | 写操作被 MySQL 拒绝（权限层兜底） | ERROR 1142 实测 |
| 执行器三约束 | 3s 超时 / 行数 200 / 1MB 截断 | test_executor 12 用例 |
| 洞察数字溯源 | LLM 编造数字被确定性拦截 | verify_insight_digits 抽查 |

## 六、各日产出索引

| Day | 主题 | 日志 |
|---|---|---|
| D1 | 业务库与只读沙箱 | [w24_day1_biz_db.md](w24_day1_biz_db.md) |
| D2 | 安全四道闸 | [w24_day2_sql_validator.md](w24_day2_sql_validator.md) |
| D3 | 生成链路 v1 与基线 | [w24_day3_generation_chain.md](w24_day3_generation_chain.md) |
| D4 | Schema Linking | [w24_day4_schema_linking.md](w24_day4_schema_linking.md) |
| D5 | 自修复与多轮 | [w24_day5_repair_multiturn.md](w24_day5_repair_multiturn.md) |
| D6 | 呈现与全量评测 | [w24_day6_presentation_eval.md](w24_day6_presentation_eval.md) |

## 七、R1 检查点决策

- 整体 0.970 ≥ 0.80 → **过，进 W25**（对照《04》第 2 节 R1）；
- 3 条错例归因到可解释的语义错误（时间列/分组粒度），已给 W25 eval_nightly 明确抓手；
- **如实记录分层指标，不刷数据**（评测集质量问题 3 条当日修复重跑，非放宽口径）。

## 八、欠账清单

- [x] 周 Gate 七项全达标
- [ ] W23 遗留"40 并发 P95 达标"评估窗口延续
- [ ] 错例优化抓手（few-shot 补发货时间窗/分组粒度）→ W25 eval_nightly
- [ ] 多实例会话持久化（进程内 LRU → MySQL conversations）→ W25
- [ ] 前端呈现组件改造（表格/SQL 折叠/反馈按钮，当前后端 SSE `data_table` 事件已就绪）→ 按需

## 九、面试数字卡（简历 NL2SQL 段）

> NL2SQL execution accuracy 0.970（单表 0.975 / join 0.950 / 聚合 1.000，100 条三层评测集）；
> 攻击用例 20/20 拦截 + 只读账号纵深防御；Schema Linking token 降 53.3% 精度反升；
> 错误自修复救回率 0.933；多轮追问 9/10；洞察摘要数字可溯源（禁止编造双保险）；real P95 38.9ms。

## 十、W25 衔接

| W25 主题 | 本周输入 |
|---|---|
| eval_nightly | 100 条评测集 + 错例抓手（few-shot 优化方向） |
| daily_brief | NL2SQL 链路生成日报 SQL（GMV/延迟率/TOP5） |
| feedback 回流 | `POST /api/data/query/{id}/feedback` 预置（fb_type=sql） |
| scheduler_job_runs | W23 建表就绪，W25 落调度六任务 |

> W24 的结论一句话：**NL2SQL 的公信力不在"准确率高"，而在每条 SQL 可见、每个错误可解释、每次攻击被拦截。**
> 0.970 + 20/20 + P95 38.9ms + 3 条可解释错例——三类证据齐全，进入 W25。
