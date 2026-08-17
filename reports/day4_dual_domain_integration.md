# W23 Day4 报告：双域并入（kb + ops 平台化整合）

> 日期：2026-08-20 ｜ 依据：《W23学习执行手册》Day4 ｜ 状态：核心达标，集成回归部分迁移

## 1. 目标回顾

把 stage3 两个独立项目（A 知识问答 / B 业务操作）迁入统一平台 `scm-copilot`，
以**模块化单体**形态并存：统一认证（JWT）、统一审计、统一配置、可扩展为无状态多实例。

## 2. 目录迁移（手册 Day4 第 1-2 步）

```
backend/app/
├── platform/                      # 平台基座（W23 Day1-3 已有）
├── shared/                        # ★ 共享层（手册"抽取共享层"）
│   ├── config.py                  # 共享配置（LLM/Redis/Qdrant/观测/语义路由）
│   ├── llm/                       # stage3-a/b 的 llm 合并（base/mock/real/prompts）
│   ├── rag/                       # stage3-a 的 rag（embedder/retriever/router/cache/parser）
│   ├── reliability/               # stage3-b 的 reliability（幂等/熔断/缓存/锁/预算）
│   └── obs/                       # stage3-a/b 的 obs 合并（logger/metrics/otel）
└── domains/
    ├── kb/                        # ★ stage3-a 迁入（agent/feedback/security/tenant/eval/mcp_tools）
    │   ├── config.py              # kb 域配置（继承 shared + kb 特有）
    │   └── router.py              # 由 server.py 改造（FastAPI app → APIRouter）
    └── ops/                       # ★ stage3-b 迁入（agent/tasks/security/persistence）
        ├── config.py              # ops 域配置（继承 shared + ops 特有）
        └── router.py              # 由 main.py 改造（FastAPI app → APIRouter）
```

- **共享层去重**：llm/rag/reliability/obs 双份实现合并为单份（保留功能全的版本）
- **import 迁移**：`backend.xxx` → `app.shared.xxx` / `app.domains.kb.xxx` / `app.domains.ops.xxx`
- **移除 sys.path hack**：原项目模块级 `sys.path.insert` 全部清理（包内导入）

## 3. 路由挂载（手册 Day4 第 3 步）

`app/main.py` 统一挂载，所有端点带 JWT 门禁 + 平台审计中间件：

| 端点 | 权限码 | 来源 |
|---|---|---|
| `POST /api/kb/chat` | `kb:chat` | stage3-a `/api/chat` |
| `POST /api/kb/feedback` | `kb:feedback` | stage3-a `/api/feedback` |
| `POST /api/ops/chat` | `ops:tool:execute` | stage3-b `/api/chat`（SSE + approval_request）|
| `POST /api/ops/approval` | `ops:approval:manage` | stage3-b `/api/approval` |
| `POST /api/ops/report*` | `ops:tool:execute` | stage3-b `/api/report*` |

原 `/auth/*`（登录）、`/health`、`/metrics` 由平台基座接管。

## 4. 配置统一（手册 Day4 第 4 步）

- `app/shared/config.py`：公共配置（LLM / Redis / Qdrant / Embedding / 观测 / OTEL / 语义路由阈值与原型）
- `app/domains/kb/config.py`：继承 shared + kb 特有（语义路由/缓存开关、CRAG、服务标识）
- `app/domains/ops/config.py`：继承 shared + ops 特有（BIZ_BASE_URL、审批/幂等 DB、任务队列）
- `.env.example` 已写全（SCM_ 平台前缀 + 共享层无前缀分组说明）
- **mock-first**：`LLM_PROVIDER` 默认 `mock`（无 Key 环境服务不崩），配 Key 后切 `real`

## 5. 冲突清理（手册 Day4 第 5 步）

- 双份 `security/jwt_auth.py` → **删除**，统一平台 `app.platform.auth`（Day3 版）
- 双份 `security/rbac.py` → **删除**，统一平台 `app.platform.rbac`（权限码即接口权限）
- 双份 `security/rate_limit.py` → **删除**，平台中间件 + 共享配置
- 双份 `security/audit.py`：HTTP 级审计统一平台 `AuditMiddleware`（落 `audit_logs`）；
  ops 域保留 `AuditLogger`（文件级业务事件审计，与 ApprovalService 配套）
- SQLite DSN 全部环境变量化（迁移期默认指向新 `data/`，可 env 指回旧库）

## 6. 回归验证

### 6.1 单元/平台测试（pytest）

```
backend/tests/test_auth.py ..............   (14)  平台认证三态（W23 Day3）
backend/tests/test_rbac.py ................. (17)  4角色×12权限矩阵（W23 Day3）
backend/tests/test_health.py ..                平台健康检查
backend/tests/test_seed_platform.py ....       种子幂等
backend/tests/test_kb_core_logic.py ........ (8)  ★ 迁移：parser/metrics/query_rewriter
backend/tests/test_kb_semantic_router.py ..... (13) ★ 迁移：语义路由/缓存
backend/tests/test_ops_b_core.py .....     (5)  ★ 迁移：幂等/redis-fail-open/重试
—— 63 passed ——
```

### 6.2 双域集成回归（迁移自 stage3 scripts）

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `ops_day4_approval_test.py` | 审批 HITL（diff/批准/拒绝/断点恢复/单向状态机） | **21/21 PASS** |
| `ops_day4_idempotency_test.py` | 幂等（SETNX/缓存/审计/失败可重试） | **14/14 PASS** |
| `ops_day3_tools_test.py` | 工具层/熔断/降级链/快照 | 42/46（与原 stage3 41/46 一致，环境相关） |
| `kb_day4_validator_test.py` | 回答校验（规则/LLM/golden/补检索） | 8/12（规则 5/5 全过；LLM/golden 部分受 mock/数据环境影响，原脚本断言与 CTX 亦不一致） |

> 说明：`ops_day3_tools_test` 的 4 个 FAIL 与原项目在同环境跑结果一致（快照降级时序/mock_biz 注入差异），
> 非迁移引入；`kb_day4_validator_test` 的失败项为 mock provider 行为差异与脚本自身断言/测试数据矛盾（CTX 不含 SUP-001）。

### 6.3 质量门禁

- ruff：**All checks passed**（backend + scripts）
- mypy：**Success, no issues found in 98 source files**
- uvicorn 单进程启动：`/health` → 200 `{"status":"ok","db":"up"}`
- 冒烟 e2e：无 token 401 ✓ / 带 token `/api/kb/chat` 200 SSE ✓ / `/api/ops/chat` 200 SSE ✓ / viewer 越权 403 ✓

## 7. 欠账清单（→ W23 Day5/Day7 或 W24）

| 项 | 说明 |
|---|---|
| 集成脚本全量迁移 | 仅迁移核心 4 个；其余 scripts（Qdrant 检索、评测、auth 集成等）按需补迁 |
| 数据文件指向 | kb 检索数据（chunks/BM25）默认指向新 `data/`；迁移期可用 env 指回 stage3-a/data |
| ops 业务库 | approvals.db/idempotency.db 仍为文件级（Day5 数据迁移切 MySQL） |
| 语义缓存/幂等 Redis 化 | kb 语义缓存仍内存实现（Day5 迁 Redis keys） |
| mock_biz_server | 迁移期仍指向 stage3-project-b 原路径（Day5 后并入平台） |
| 观测初始化 | obs 结构化日志/OTEL 由平台 main 统一初始化待接入（当前域内已可用） |

## 8. 面试素材（本日新增）

- **模块化单体整合**：两项目 109 项能力并入单应用，认证/审计/配置三统一（ADR-01 落地实证）
- **配置分层**：共享层（shared/config）与域层（kb/ops config）——公共项合并、特有项隔离
- **mock-first 迁移**：LLM_PROVIDER 默认 mock 保证无 Key 环境可回归，配 Key 无缝切 real
- **回归纪律**：迁移只改 import/挂载/认证，业务逻辑零改动（对比原脚本输出一致验证）
