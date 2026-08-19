# 覆盖率「文档化接受」清单（W27-D5 初稿）

> 依据：W27 执行手册 D5 第 8 条（评审风险4采纳）——parser/store/otel 逐个判定
> 「补测 or 接受并写明理由」；W28-D6 只做终审，避免收官日为最后 1pp 硬凑加班。
> 口径：与 W26 一致 `pytest --cov=backend`（source=backend，branch=True）。

## 一、判定原则（面试口径）

1. **不值得追覆盖**：网络 IO 薄壳、依赖外部行为的适配层——mock 到没有信息量；
   覆盖率是手段不是 KPI。
2. **值得测**：纯逻辑与降级分支（本周 D5 已把 reranker/query_rewriter/real 错误分类/
   lock/idempotency/cost_budget 全部补齐）。
3. 本清单只解决「低覆盖但合理」的洼地，**每一项都必须给出证据链**（谁在测它、断点是什么）。

## 二、逐项判定

### 1. `shared/obs/otel.py`（0%）

- **判定**：接受（适配层，mock 无信息量）
- **理由**：
  - OTEL 是观测旁路（fail-open），`OTEL_ENABLED=0` 或 exporter 不可达时 noop；
    断言它「不炸」的信息量 ≈ 0。
  - 真实价值在 Traces 数据是否进到后端，属部署/容器验证而非单测；
    已有 `deploy/` 下的探活脚本 + W26 混沌演练覆盖「OTEL 故障不阻塞业务」。
  - 补测方案（已否决）：mock 一个 OTLP exporter 断言 span 内容——等于把第三方 SDK
    的行为搬进测试，维护成本高、信息量低。
- **终审建议（W28-D6）**：维持接受。

### 2. `shared/rag/store.py`（12%）

- **判定**：接受（Qdrant 适配层，逻辑已在别的文件覆盖）
- **理由**：
  - store.py 是 Qdrant 客户端的薄封装（upsert/search/delete + payload 过滤），
    依赖真实 Qdrant 容器；本地单测 mock 它 = 测 mock 自己。
  - 核心检索逻辑（混合检索融合、降级链）在 `hybrid_retriever.py`，
    已有 `test_hybrid_retriever_degrade.py` 覆盖，且有集成测试走真实 Qdrant。
  - 断点：`deploy/` eval_nightly / kb_sync 冒烟脚本 + 容器集成测试。
- **终审建议（W28-D6）**：维持接受；若 W28-D4 多租户分片改造动了 store 的过滤逻辑，
  再补该文件的租户隔离专项用例（与 C4 一起）。

### 3. `shared/rag/parser/`（pdf_parser 10% / word_parser 6% / registry 50%）

- **判定**：接受（外部解析库适配层，依赖真实文件）
- **理由**：
  - 解析依赖 pdfplumber / python-docx 且需要真实制度文档文件（.pdf/.docx）；
    mock 二进制解析流程没有信息量。
  - 已有真实覆盖：`kb_increment_sync` 增量同步 + eval_nightly 每晚对 57 篇真实
    制度文档做全量解析（B17 数据闭环），解析失败会在任务里显式报错——这是
    「真实文件 + 真实解析」的回归，比 mock 单测更有说服力。
  - registry 50% 已过半（分发逻辑），低覆盖集中在两个具体解析器。
- **终审建议（W28-D6）**：维持接受。

### 4. 其他低覆盖文件（顺带记录，不单独追）

| 文件 | 覆盖 | 判定 |
|---|---|---|
| `domains/kb/feedback/feedback_store.py` | 22% | 接受：存储适配层，被 feedback 集成链路 + 夜间回归覆盖 |
| `shared/obs/otel.py` | 0% | 见上 |
| `shared/reliability/retry_policy.py` | 58% | 接受：已被 real provider `_post_chat` 重试逻辑实测覆盖（W27-D4 归位复用） |
| `scripts/eval_*.py` | ~50% | 不进产品覆盖口径（脚本，非 app 包） |

## 三、验收对照（W27-D5 达成）

| 目标 | 实际 | 状态 |
|---|---|---|
| 总覆盖率 ≥66% | 66%（56% → +10pp） | ✅ |
| reranker ≥85% | 97% | ✅ |
| real 系列 ≥60% | errors 98% / provider 88% / cost 88% / model_pool 94% / obs 62%（合计 ~89%） | ✅ |
| query_rewriter 新测 | 96% | ✅ |
| distributed_lock 新测 | 99% | ✅ |
| idempotency 新测 | 93% | ✅ |
| cost_budget 新测 | 95% | ✅ |
| 新增测试文件 | 8 个（537 用例全绿） | ✅ |

> 本周追覆盖的取舍叙事：**把「逻辑复杂但纯函数、易测」的洼地全部填平**（reranker/
> query_rewriter/错误分类/四组件），把「网络 IO 薄壳、依赖外部行为」的适配层文档化接受——
> 这正是手册面试题的标准答案。
