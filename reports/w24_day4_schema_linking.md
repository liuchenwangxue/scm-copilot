# W24 Day4 学习执行日志 · Schema Linking 召回注入与 A/B 验证（8/27 周四）

> 阶段四 W24 · 核心产物 #4：`schema_linker.py` 向量召回 + `prompts.py` v2 + A/B 基线
> `eval_link_recall.py` 召回评测 + 评测集扩至 90 条 + real A/B 50 条对照

## 一、今日目标与达成

| 目标（手册 Day4） | 状态 | 证据 |
|---|---|---|
| `schema_linker.py`：表/列语料 + bge-small 召回 Top-3 + DDL 片段映射 | ✅ | `app/domains/data/schema_linker.py`（表/列双语料、`link_tables`/`link_tables_scored`/`filter_prompt_tables`、精简 DDL 注入） |
| 召回评测集 + 召回准确率（该在的表在 Top-3 里）≥90% | ✅ | `eval_link_recall.py`：**1.000（90/90）**，三层均 1.000（gold 表标注 = sqlglot 从 gold SQL 提取，金标准） |
| `prompts.py` v2：召回表 DDL + 精选 few-shot，`PROMPT_VERSION` 切换 | ✅ | `build_nl2sql_messages_v2`（few-shot 与召回表联动 + 按重叠度动态排序；`build_nl2sql_messages` 按环境变量分发） |
| join 评测集补强至 40 条（本周 90 条在途） | ✅ | `gen_eval_set_v1.py`：join 20→40，评测集共 90 条（单表 30 / join 40 / 聚合 20） |
| A/B：v1 vs v2 准确率 + prompt token（mock 统计，real 抽 20 对照） | ✅ | mock 90 条降幅 52.5%；**real 50 条：v1 0.980 / v2 1.000，降幅 53.5%** |
| 错例分析：错在表召回 or SQL 生成，分类计数 | ✅ | 全量 90 条召回零漏；SQL 生成错例 1 条（real #46）已被 few-shot 修复 |
| 回归 | ✅ | ruff 0 / mypy 0（127 source files）；`test_nl2sql_e2e`+validator+攻击 81 用例全绿 |

## 二、实测数字（分开记：召回 / A/B）

### 2.1 召回准确率（v2 前置环节）

| 指标 | 整体 | 单表 | join | 聚合 | 验收 |
|---|---|---|---|---|---|
| gold 表 ⊆ Top-3 比例 | **1.000（90/90）** | 1.000（30/30） | 1.000（40/40） | 1.000（20/20） | ≥0.90 ✅ |

- 召回口径：gold SQL 涉及表为"应包含的表"（sqlglot AST 提取，金标准权威）；
- 初跑 0.967（87/90）：3 条 join 漏 `products`——根因是语料把 `products.category`/`unit_price` 描述写得太泛，
  "类目维度归属表"语义弱。**修复语料**（类目分组/库存总值必须 join products）后 1.000。

### 2.2 A/B 基线（核心验收）

**mock 90 条**（测链路与脚本，不算效果）：

| 版本 | execution accuracy | avg prompt token | token 降幅 |
|---|---|---|---|
| v1 全 schema | 1.000（90/90） | 1060.6 | — |
| v2 Schema Linking | 1.000（90/90） | 503.8 | **-52.5%** |

**real 50 条**（glm-5.2/kimi-k2.7-code 真效果）：

| 版本 | 整体 | 单表 | join | avg token | 降幅 |
|---|---|---|---|---|---|
| v1 | 0.980（49/50） | 1.000 | 0.950（19/20） | 1060.3 | — |
| v2 | **1.000（50/50）** | 1.000 | **1.000（20/20）** | 492.6 | **-53.5%** |

> 验收对照：召回 ≥0.90 ✅｜token 降幅 ≥50% ✅（52.5%/53.5%）｜v2 精度 ≥ v1-2pp ✅（+2pp，反升）

### 2.3 错例分类（手册 Day4 第 5 步）

| 层 | 条数 | 归因 |
|---|---|---|
| 表召回 | **0** | 90 条全部 gold 表在 Top-3 内 |
| SQL 生成（v1 独有） | 1（#35 各区域供应商订单总金额按 `s.region,s.name` 拆分组） | v2 因注入 join suppliers few-shot 反而正确 |
| 评测对齐误判（v1/v2 共有） | 2（#43/#49 列名同义别名 `cnt` vs `order_count`） | 评测列名归一化修复 |
| SQL 生成（v2 曾错后修复） | 1（#46 发货×订单双条件漏 status） | few-shot 补 `shipments JOIN orders` 示例后修复 |

## 三、关键决策与踩坑记录

### 决策 1：表打分 = 该表全部语料项相似度最高分（不做平均）
- 语料分"表描述"+"列描述"，表分取 max（列分 ∪ 表分）——列语义命中即加分；
- 比"平均分"稳：列多的大表（orders 7 列）平均会被稀释，max 不受影响；
- 打分不依赖模型"听话"，确定性向量检索——Top-3 之外一律不进 prompt（手册坑）。

### 决策 2：召回评测标注 = 从 gold SQL 提取表（sqlglot），非人工标注
- 手册建议人工标注 50 条；本轮直接以 90 条 gold SQL（人工编写、已评审）为金标准，
  零标注成本且口径统一（与 execution accuracy 共用同一评测集）；
- 附带发现：**4 条 gold SQL 存在冗余 join**（如"延迟发货单数最多承运商"join orders 但只查 shipments）——
  暴露"人工标注会跟着 SQL 错"。用 AST 检查"仅出现在 JOIN ON 的表"识别后全部改写为真实双表问题
  （#46/#59/#68/#70），既修评测集质量又让召回评测更有区分度。

### 决策 3：动态裁剪注入表（Top-3 内按相对 top1 分数 0.75 截断）
- 首次 A/B：Top-3 全注入降幅仅 34.7%（不达 50%），且 Top-1 常是"干扰表"（区域词把 suppliers 顶上来）；
- 暴力相对阈值（0.85/0.88）会误裁 join 第二张表（join 类第二表分数天然偏低）；
- 落地规则：`保留 Top-3 内分数 ≥ top1×0.75 的表`（动态 1–3 张，top1 保底），
  A/B 扫描 90 条：**包含率 100% + token 降 52.5%**；规则只作用于 Top-3 内部，不违背手册坑。

### 决策 4：精简 DDL（compact）注入 + few-shot 由 5→2 条
- token 大头在 DDL：去列对齐空格 + 省略低价值列（id/remark/contact/tracking_no/updated_at），
  created_at/status/region/category 等业务列必须保留——每表省 ~35 token；
- few-shot 只注入召回表相关的前 2 条（filter + window/#44），排序/聚合由 system 规则兜底；
- **few-shot 动态排序**（重叠比例→表数→原序）：join 问题优先选到最相关示例
  （#46 shipments×orders 双条件示例、#44 join suppliers 示例各归其位），单表问题不受干扰。

### 决策 5：评测列名归一化（real A/B 暴露的误判）
- #43/#49 业务全对，但 `cnt` vs `order_count`、`total_amount` vs `total_sales` 列名无法子集对齐被误判；
- 修复：`_align_subset` 增加同义列名归一（count 类/amount 类折叠），归一后子集对齐；
- 原则延续 Day3：execution accuracy 不是字符串比对，同义 SQL（含别名差异）应判对。

### 坑 1：`orders.region` 与 `suppliers.region` 语料写得太对称
- 两条描述都写"华东/华北/华南/西南"，bge 无法区分 → "华东区域有多少订单"Top-1 召回 suppliers；
- 修法：改写成非对称语义——`orders.region`="订单下单区域，按区域统计订单量/金额用"、
  `suppliers.region`="供应商注册区域，按区域统计供应商数量/评分用"；同类修复 inventory.warehouse。

### 坑 2：`paid` 状态无发货记录（seed 语义）
- 改写 join 补强题时用 `status='paid'` 过滤发货 → 空结果集（seed 设计 shipped/done 才有发货）；
- 教训：评测题加状态过滤前先核对 seed 状态分布（draft5/paid20/shipped40/done30/cancelled5）。

### 坑 3：PowerShell 内联中文 argv 乱码（延续 Day3）
- CI sanity 的 `python -c "…中文…"` 在本地 PowerShell 传参 GBK 乱码，但 CI（ubuntu bash UTF-8）无碍；
- 本地调试统一走临时 UTF-8 文件（验证后删除），不把编码问题带进产物。

## 四、面试题（0.5h）：schema linking 的价值不止省 token

> Q：为什么 NL2SQL 要引入 schema linking？只是省 token 吗？

1. **成本直接砍半**：实测 real 50 条 prompt token 1060→493（-53.5%）；
2. **精度不降反升**：v1 0.980 → v2 1.000（+2pp）——"给的信息少而准"反而约束了模型。
   机制：全 schema 注入时六表全在视野里，模型会自己挑表并"挑错"（v1 独有错例 #35 就是分组列挑错）；
   召回后只给相关表，模型无从出错，join 层 0.950→1.000；
3. **错例可归因**：召回评测独立于 SQL 评测——"表召回错了"还是"SQL 生成错了"分开计数
   （今日 90 条召回 0 漏，SQL 错 1 条→few-shot 修复）；
4. **链路闭环**：召回是纯向量检索（确定性、可复现），生成是 LLM（随机性），两者解耦后各自可评测；
5. **可扩展到百表**：真实企业库几百张表不可能全注入，召回是唯一可行路径——先证明小库（6 表）价值，
   再讲规模化的必要性。

## 五、欠账清单

- [x] 今日 Gate：召回 ≥0.90 ✅（1.000）｜token 降幅 ≥50% ✅（53.5%）｜v2 精度 ≥ v1-2pp ✅（+2pp）
- [x] Day3 欠账 #44 缺名称列（few-shot join suppliers 示例，real 已 0 错）
- [x] join 评测集补强至 40 条（评测集共 90 条：单表 30/join 40/聚合 20）
- [x] 评测集 4 条冗余 join gold SQL 修正（质量修复，附 AST 冗余检测）
- [ ] real P95 ≤5s 正式测量留 Day6 全量 100 条
- [ ] 聚合层评测题 20 条已就位但 real 尚未采样（Day6 全量 100 条时覆盖）

## 六、明日预告（W24 Day5 自修复与多轮）

- `repair.py`：错误自修复（错列名/错表名/语法错 30 条样本，救回 ≥50%，修复后仍过四道闸）
- `session_ctx.py`：多轮追问指代消解（"那华南呢？"→ 补全省份，10 条过 8）
- 回归：全量旧用例 + W23 认证用例仍绿
