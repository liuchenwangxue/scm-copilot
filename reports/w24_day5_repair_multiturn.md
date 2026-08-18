# W24 Day5 学习执行日志 · 错误自修复与多轮追问（8/28 周五）

> 阶段四 W24 · 核心产物 #5：`repair.py` 错误自修复 + `session_ctx.py` 多轮指代消解
> 评测：`eval_repair.py` 30 条坏 SQL + `eval_multiturn.py` 10 条对话（共 22 轮）

## 一、今日目标与达成

| 目标（手册 Day5） | 状态 | 证据 |
|---|---|---|
| `repair.py` 修复 prompt（带报错/坏 SQL/不改语义硬性规则） | ✅ | `app/domains/data/repair.py` `build_repair_messages` + `repair_sql`（mock/real 双路径） |
| `repair.py` mock 确定性修复（评测集 gold / fail 模式） | ✅ | `app/domains/data/mock_repair.py` `MockRepairGenerator` |
| 图接入 repair/degrade 节点（≤2 次循环 + 安全不豁免） | ✅ | `app/domains/data/graph.py` `repair_node` / `degrade_node` / `route_after_validate` 分流 |
| `session_ctx.py` 多轮指代消解（mock 规则 + real LLM） | ✅ | `app/domains/data/session_ctx.py` `_mock_resolve`（12 模式全过）/ `build_resolve_messages` |
| `POST /api/data/query` 接 session_id（消解→入图→上下文记录） | ✅ | `app/domains/data/router.py` `_audit_sink` 增 `repaired_sql` 字段 + session 闭环 |
| 30 条坏 SQL 救回率 ≥50% | ✅ | **real 28/30 = 0.933**（错列名 10/10 / 错表名 8/10 / 语法错 10/10；平均 1 次修复） |
| 多轮 10 条过 8 | ✅ | **real 9/10**（整体 0.955 / 首轮 1.000 / 追问轮 0.917） |
| 修复产物仍必须过四道闸（安全不豁免） | ✅ | 修复循环内出现安全类闸拒 → 直接降级（`route_after_validate` 分流 + `test_graph_repair_degrade_after_max_attempts`） |
| 回归：全量旧用例 + W23 认证用例仍绿 | ✅ | **200 passed**（纯单测 + integration）；ruff 0 / mypy 0（135 source files） |
| 错例可解释（不是黑盒） | ✅ | 每条 `repair_log` 含 `[{attempt, failed_sql, failure, repaired_sql}]` → `audit_logs` 落 `repaired_sql` 取证可回放 |
| ★ 模型池持久化 + 全量配置（Day5 顺带） | ✅ | `reports/llm_model_state.json` 持久化当前模型；10 个模型全配置 |

## 二、实测数字（real，kimi-k2.7-code 活跃）

### 2.1 错误自修复救回率（30 条：错列名 10 / 错表名 10 / 语法错 10）

| 指标 | 整体 | 错列名 | 错表名 | 语法错 | W24 Gate |
|---|---|---|---|---|---|
| 救回率 | **0.933（28/30）** | **1.000（10/10）** | **0.800（8/10）** | **1.000（10/10）** | 整体 ≥0.50 ✅ |
| 平均修复次数 | 1.0 | 1.0 | 1.0 | 1.0 | — |
| 降级样本 | 0 | 0 | 0 | 0 | — |

### 2.2 多轮指代消解 + 全链路（10 条对话 × 2–3 轮）

| 指标 | 对话通过 | 轮次整体 | 首轮 | 追问轮 | W24 Gate |
|---|---|---|---|---|---|
| 准确率 | **0.900（9/10）** | **0.955（21/22）** | 1.000（10/10） | 0.917（11/12） | 10 条过 8 ✅ |
| 消解一致率 | — | 0.773 | 1.000 | 0.583（追问轮的 LLM 消解） | — |

> mock 链路（消解规则 + 注册 SQL）：**10/10 / 22/22 = 1.000**（测链路与脚本正确性，不算效果）。

## 三、关键决策与踩坑记录

### 决策 1：修复循环 ≤2 次 + 安全不豁免（双保险）
- 修复触发面只两类：①执行报错（state.error 非空）②可修复闸拒（`REPAIRABLE_REASONS = {parse-error, unknown-table}`）；
- 安全类闸拒（write-op/not-select/multi-statement/dangerous-func/for-update）→ **永不修复**：
  - 首次 → reject_node 拒答；
  - 修复循环中出现 → 直接 degrade（route_after_validate 按 `repair_attempts` 分流）；
- 修复产物**仍必须重新过四道闸**（图路径 `repair → validate → execute` 强制安全不豁免）。

### 决策 2：修复 prompt 注入相关表 DDL + 关联关系（Day5 关键迭代）
- **首版 prompt** 只给"报错原文 + 坏 SQL"，救回率 0.93：模型把 JOIN ON 关联键改了（如 `p.id`→`p.sku`、`o.supplier_id`→`s.supplier_code`、删 inventory JOIN）；
- **改进**：从坏 SQL AST 抽出涉及的六表 DDL（精简）+ RELATIONSHIPS_TEXT（关联键权威）；
- **强化**："严禁改动 JOIN 子句的任何部分（包括关联列 ON ... = ...）" + "只修复【报错信息明确指向】的错误"；
- **效果**：3 次 real 跑随机失败样本（不同但同根因）逐次被 prompt 修复；最终 0.933。

### 决策 3：mock 与 real 双路径——"mock 测链路、real 测效果"（手册坑）
- `MockRepairGenerator(mode="gold"|"fail")`：gold 按问题返回评测集 gold SQL（必然救回，30/30）；fail 原样返回（测"两次失败→降级"路径）；
- `MockSQLGenerator` 多轮评测扩展：每轮可注册 question→gold SQL（mock 链路 10/10 = 1.000）。

### 决策 4：mock 规则消解 vs LLM 消解——各司其职
- mock 规则消解（12 模式）：时间窗替换/补插、区域替换/补插、"各区域"聚合对比、状态替换/补插（修复了首版字符类匹配错的 bug——`[华东华北华南西南]区域` 会被单字匹配）；
- LLM 消解（real）：单独一次调用注入"上一轮 SQL + 上一轮问题"，不塞进 SQL 生成 prompt（手册坑"两个关注点分开"）；
- 追问轮消解一致率仅 0.583（real LLM 随机性），但 SQL 链路执行准确率 0.917——LLM 消解错时降级话术兜底（不硬答）。

### 决策 5：TokenError 也要按 parse-error 拒绝（★ Day5 实测踩坑）
- sqlglot 30.x 对未闭合括号等输入抛 `TokenError`（不是 `ParseError`），validator 漏接导致图整体崩溃（langgraph 异常冒泡到 ainvoke 调用方）；
- 修复：validator 闸1 try 块改为 `except (ParseError, TokenError)` 统一拒绝；
- 回归测试 `test_sql_validator.py::TestParseError::test_unparseable_rejected` 加 `SELECT ... region='华东'(` 覆盖。

### 决策 6：★ 模型池持久化与全量配置（Day5 顺手）
- **全量配置** 10 个模型（按控制台截图）：kimi-k2.7-code（首位，当前活跃）/ qwen3.7-max-2026-06-08（剩余 99%）/ qwen3.7-plus-2026-05-26 / qwen3.7-max-2026-05-20 / qwen3.7-max-2026-05-17 / qwen3.7-max-preview / qwen3.7-plus / qwen3.7-max / qwen3.8-2.4t-a95b / deepseek-v4-pro-0813；
  - 已剔除 glm-5.2（免费额度早已耗尽，每次先探它会白白浪费一次 HTTP 调用）；
- **持久化**：每次成功调用后写 `reports/llm_model_state.json`（model + updated_at），下次进程启动读取并把活跃模型挪到池首位；
- **效果**：本轮 real 评测无任何 `[REAL-QUOTA]` 切换日志（直接命中 kimi），节省 HTTP 调用；
- 持久化文件只含模型名/时间戳，无敏感信息（Key 仍在 .env 中受 .gitignore 保护）。

### 坑 1：PowerShell 中文字符终端管道乱码（延续 Day3）
- 评测/调试用内联 `python -c "中文"` 在 PowerShell GBK 乱码；
- 解决：统一写临时 UTF-8 文件调试（验证后立即删除）。

### 坑 2：模拟 SQL 注入 `_break_*` 构造列名/表名错（sqlglot AST 坑）
- 首版用 `col.set("name", ...)` 无效（正确键是 `this`），构造出来仍是原 SQL；
- 修：改用 `col.set("this", exp.to_identifier(...))`。

### 坑 3：mock 消解正则不识别全角问号"？"
- `^(?:那|那么)?(.+?)呢\??$` 末尾只匹配半角 `?`，中文全角问号（U+FF1F）"？"导致匹配失败；
- 修：改为 `?`（同时支持半角/全角问号）。

## 四、面试题（0.5h）：降级设计哲学

**Q：自修复和降级的话术如何设计？为什么要"两次失败就降级"？**

1. **错误答案的代价远高于拒答**：模型"诚实犯错"（错列名/表名/语法错）可救——给它两次机会；如果第二次仍救不回，**说明模型对业务语义理解不到位**，硬答只会更危险（"能跑但答非所问"）；
2. **修复 prompt 强约束"不改语义"**：错列名可救（`amountx`→`amount`），改 WHERE 条件/改 JOIN/删表不能救——会污染结果集；
3. **降级话术要点**：不重复报错原文（前端/用户关心的是"接下来怎么办"），给**改写方向建议**（如"可尝试改为：SELECT COUNT(*) FROM shipments WHERE delay_days > 0"）——把评测集里 gold 相似问题的方向提炼出来；
4. **可解释 ≠ 永远成功**：错例可归因（`repair_log` 记录每次尝试的 failed_sql/repaired_sql/failure），真实 LLM 救回率不可能 100%，**剩下的 5–10% 降级比硬答更可信**——这是面试讲"边界"的素材。

## 五、欠账清单

- [x] 今日 Gate：救回率 ≥0.50 ✅（0.933）/ 多轮 10 过 8 ✅（9/10）
- [x] mock 链路 100% 通过
- [x] 修复 prompt 强化迭代完成（JOIN/关联键保护 + DDL 注入）
- [x] TokenError 兼容性 + 单测覆盖
- [x] 完整回归：200 passed / ruff 0 / mypy 0
- [ ] real P95 ≤5s 正式测量留 W24 Day6 全量 100 条
- [ ] W23 遗留"40 并发 P95 达标"评估窗口延续
- [ ] 多实例会话持久化（MySQL `conversations` 表已有 touch 通路）→ W25 scheduler_job_runs 联动

## 六、W25 衔接预告

| W25 主题 | 与本日的关系 |
|---|---|
| eval_nightly 夜间回归 | 本周 100 条 + 多轮 10 条评测集成为夜间任务的数据源 |
| daily_brief 经营日报 | 本周 NL2SQL 链路生成日报 SQL（GMV/延迟率/TOP5） |
| feedback 回流 | `POST /api/data/query/{id}/feedback` 已预置（fb_type=sql），W25 进评测集增量 |
| 多实例会话持久化 | 进程内 LRU（`session_ctx.py`）临时方案 → W25 迁 MySQL `conversations`（已有 touch 通路） |
| SDK 客户化封装 | NL2SQL 是 SDK 三个核心契约之一（`ScmCopilot.nl2sql(question, as_dataframe=True)` → 透出 SQL 可审计） |

> NL2SQL 的可解释性不止"准确率高"：**每条 SQL 可见、每个修复可追溯、每次攻击被拦截、降级永远不硬答**。
> W24 结束时，所有 4 个产品维度（业务库 / 闸 + 沙箱 / Schema Linking / 自修复 + 多轮）就位。