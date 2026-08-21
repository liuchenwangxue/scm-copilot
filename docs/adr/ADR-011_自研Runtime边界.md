# ADR-011：自研 Runtime 的边界——何时不依赖框架

- 状态：已采纳（W28 Day6 落地 PoC，B5 项）
- 日期：2026-09-05（W28-D6）
- 相关：`00_问题总清单` C8/B5、ADR-009/ADR-010
- 影响代码：`backend/app/shared/runtime/loop.py`（双形态内核）、
  `backend/app/domains/data/runtime_graph.py`（data 图迁移）、`backend/tests/test_runtime_loop.py`

---

## 一、背景与问题

项目当前全部 agent 图跑在 LangGraph 上（kb/ops/data 三域）。框架带来的收益与**税**并存：

| 税 | 表现 |
|---|---|
| 隐式行为黑盒 | checkpointer/interrupt 的 resume 语义（W28-D2 观察项：approval_gate
  resume 时节点整体重跑、审批单重复 create）——不读源码/不压测几乎无法预判 |
| 版本迁移风险 | langgraph-checkpoint-mysql 等配套包随主版本演进，升级需要回归全部图 |
| 能力被框架上限锁死 | 无法在节点间插自定义调度（如按租户路由）、无法做 mock-first 的
  无框架单测 |

C8 的诉求是"框架依赖要有退出路径"——不是推翻 LangGraph，而是**证明随时能退出**。

## 二、决策：自研最小内核，只迁 data 域，保留 ops/kb 在 LangGraph

`shared/runtime/loop.py`（~120 行）提供双形态内核：

1. **图节点循环 `run_graph`**：`(start, nodes, edges, router_map, state, max_steps)`
   三件事——按拓扑走到节点、执行节点函数、按路由函数决定下一步；END 用空串表示。
2. **原生 tool-calling 循环 `run_tool_loop`**：`tools schema → tool_calls →
   registry 执行 → tool_result 回填 → 终答`——w11/w12 的 SDK 标准循环形态
   （D2 资产回归：与平台"结构化 JSON 选工具"形成双路径对照）。

**迁移边界**：只迁 **data 域**（`domains/data/runtime_graph.py` 复用 graph.py 的
7 节点 + 3 路由函数，同输入同输出对照测试通过）。ops 域**不迁**——它的
interrupt/checkpointer 生态（HITL 审批断点续跑）是 LangGraph 主场；kb 域检索
图简单但依赖 Qdrant 重试等既有封装，保持现状零风险。

## 三、为什么 data 域适合自研

| 判据 | data 域 | ops 域 |
|---|---|---|
| 状态持久化 | 图上无 checkpointer（多轮上下文在图上之外的 session_ctx/Redis） | 需要 checkpointer（HITL） |
| interrupt | 无 | 有（approval_gate） |
| 节点数 | 7 节点 + 3 路由，纯 DAG | 含条件重跑/外部工具副作用 |
| 单测友好度 | mock 生成器可全链路无框架测 | 依赖 mock-biz + 审批服务 |

data 图单轮无状态、无中断点——正是"框架税 > 收益"的场景；自研内核 100 行
完全覆盖其执行需求。

## 四、与框架的取舍（面试对照）

| 维度 | 自研内核 | LangGraph |
|---|---|---|
| 可控性 | 100 行、零依赖、行为全透明 | 隐式 checkpointer/interrupt 语义 |
| 功能 | 单轮无状态子图足够 | checkpointer/interrupt/持久化完整生态 |
| mock 友好 | 节点函数可脱离框架单测 | 需 compile + 依赖注入 |
| 运维面 | 无版本迁移风险 | 配套包随主版本演进 |
| 适用 | 简单 DAG、可重入、无中断 | 有状态、需中断/续跑、复杂编排 |

## 五、混合运行（本项目落地形态）

- data 域：`run_data_runtime(state)` 与 `data_graph.ainvoke(state)` 同签名——
  service.py 切换一行即可（本日仅对照测试，不切线上路径）；
- kb/ops 域：保留 LangGraph；
- 三期 backlog：Runtime 全量迁移（B5）——只有在"全项目图都无状态化"后才值得，
  当前 PoC 证据足够支撑"我有退出路径"的面试叙事。

## 六、验证与证据

- `test_runtime_loop.py` 18 用例：
  - tool-calling 内核单测：立即终答 / 单工具→回填→终答 / 多工具一轮 /
    async 工具 / max_steps 熔断 / 未知工具 KeyError / schema 协议形态；
  - 图内核单测：线性图 / 条件路由 / 未知路由键 RuntimeNodeError / 环熔断；
  - **同构对照**：data 图四路径（合法 / 安全拒答 / 未知问题 / 修复循环）
    LangGraph vs 自研引擎同输入同输出。
- `make check`（ruff/mypy）0 error；全量回归绿。

## 七、未做/边界

- 无 checkpointer/interrupt：自研内核刻意不做（有状态场景直接留 LangGraph）；
- 无持久化/重放：单轮无状态场景不需要；
- 未切线上：data 域 service.py 仍走 LangGraph（对照测试充分证明后，三期切）。
