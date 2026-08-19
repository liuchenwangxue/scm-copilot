# W26 Day2 报告 · 故障演练五连（杀穿全家桶不雪崩）

> 阶段四 SCM Copilot 第 4 周 Day2 ｜ 2026-09-08（周二）
> 主题：杀 MySQL / Redis / Qdrant / LLM 全超时 / 实例半瘫——验证降级链五连不雪崩
> 依据：《W26学习执行手册》Day2、《03_核心技术方案》第 6 节降级链验收
> **结果：5/5 场景自动降级不雪崩 + 恢复自动回归 + 当天修复 3 处演练暴露的问题 + 记录完整**

---

## 〇、Day2 速览

**目标**：杀穿全家桶，验证降级链五连不雪崩——"敢上线"的实证日。

**判定标准（演练前先写死，手册坑）**：
- 探活 5xx < 5%
- 无级联超时（故障依赖不拖垮非故障依赖）
- 恢复 <2min 自动回归（无需重启进程）

**结果总表**：

| # | 故障 | 预期降级 | 实际行为 | 判定 |
|---|---|---|---|---|
| 1 | 杀 MySQL | 审批暂停（503 明确提示）/ chat 缓存路径可用 / 写操作全拒不雪崩；恢复后 HITL 续跑 | ✅ 修复后：login 503 明确提示、已有 token 200（fail-open 认证）、审批 503、SSE error 明确提示；恢复自动回归 | ✅ |
| 2 | 杀 Redis | fail-open 降 SQLite/内存（幂等/缓存/锁走降级路径）；恢复自动切回 | ✅ 幂等降 SQLite 只执行一次、缓存降内存命中、锁放行、调度 leader 锁 fail-open；恢复 `available=True` 无缝切回 | ✅ |
| 3 | 杀 Qdrant | 检索降级 BM25-only（degraded 标记进响应/日志）；恢复混合检索自动回 | ✅ 修复后：BM25-only 4.1s 返回（原 32.5s 抛异常），degraded 标记正确；恢复 source 含 vec/both | ✅ |
| 4 | LLM 全超时 | 模型池切换全失败 → mock 兜底话术（明确告知降级）；usage 记账不重复 | ✅ 修复后：generate 返回 `[WARNING]` mock 文本、generate_json 返回 dict 且 `degraded=True`；失败模型不累计 usage | ✅ |
| 5 | 实例半瘫 | least_conn 摘除、5xx=0、流量集中 a2；恢复后自动回来 | ✅ 压测中段杀 a1：210/210 成功、5xx=0、a2 计数集中；恢复后 healthcheck 自动回归 | ✅ |

**当天修复 3 处演练暴露的问题**（详见 §七）：
1. 认证层：MySQL 挂时 `get_current_user` 查库失败 → 全部受保护端点 500 → **fail-open 信任 JWT claims**
2. 检索层：Qdrant 挂时 32.5s 重试后抛异常 → **BM25-only 降级 + 快速失败 + check_compatibility=False**
3. LLM 层：`generate_json` 降级返回 str 破坏 JSON 契约 → **按 tag 返回 mock dict 并带 degraded 标记**

---

## 一、环境与准备

### 1.1 环境确认

```bash
cd F:\code\agent\learning-outputs\scm-copilot
docker ps   # 全家桶 10 容器全 healthy（mysql/redis/mock-biz/backend-a1/a2/nginx/prometheus/grafana/node-exporter/cadvisor）
```

### 1.2 演练前零件盘点

| 零件 | 来源 | 用法 |
|---|---|---|
| `deploy/chaos/` 五脚本 + probe + 截图 | **本日新建** | 故障注入 / 探活 / 证据归档 |
| `deploy/load_test.py --kill-instance` | W23 改版 | 演练五：压测中段杀实例 |
| 降级链实现（fail-open 语义） | W19–W22 铺就 | 演练一~五的验证对象 |
| Grafana 业务面板 | W26 Day1 | 流量健康区曲线证据 |

### 1.3 演练脚本清单（`deploy/chaos/`）

| 脚本 | 作用 |
|---|---|
| `kill_mysql.ps1` / `kill_redis.ps1` / `kill_qdrant.ps1` / `llm_timeout.ps1` / `kill_instance.ps1` | 五连故障注入（-Recover 恢复） |
| `probe.ps1` | 持续探活（5xx 占比判定，<5% 不雪崩） |
| `grafana_snapshot.py` | Grafana 面板截图（证据归档） |
| `baseline_check.py` / `mysql_down_check.py` / `redis_down_check.py` / `redis_idem_failopen_check.py` / `qdrant_deg_check.py` / `qdrant_warm_check.py` / `qdrant_recover_check.py` / `llm_deg_check.py` / `prom_check.py` | 各演练的行为观测/降级验证 |

---

## 二、演练一：杀 MySQL（8/9 上午第一连）

### 2.1 故障注入

```powershell
docker stop scm-mysql
```

### 2.2 预期 vs 实际

| 观测点 | 预期 | 修复前实际 | 修复后实际 |
|---|---|---|---|
| `GET /health` | degraded/db=down，backend 存活 | 200 `{status:degraded, db:down, scheduler:running}` | 同左 ✅ |
| `POST /auth/login` | 503 明确提示 | **500 INTERNAL_500** ✗ | **503 SERVICE_UNAVAILABLE "认证服务暂不可用"** ✅ |
| 已有 JWT 访问受保护端点 | 仍可用（fail-open） | **500** ✗ | **200**（`/api/v1/auth/me` 权限完整）✅ |
| `GET /api/v1/ops/approvals` | 503 明确提示 | 500 ✗ | **503 "审批服务暂不可用（审批存储依赖故障）"** ✅ |
| `POST /ops/chat` 高危改单 | SSE error 明确提示不雪崩 | SSE error（连接错误） | SSE error ✅ |
| 恢复后 | HITL 断点续跑 + 自动回归 | — | health ok/db=up、登录 200、高危改单正常返回 approval_request ✅ |

### 2.3 根因与修复（本次演练最大收获）

**根因**：`global_auth` → `auth.get_current_user` 在签名校验后**必须查库**（吊销名单 + 用户存活），MySQL 挂时抛异常 → 全局 500。这违背"已签发 token 权限来自签名 claims（零查库）"的设计初衷——认证成了 MySQL 的单点依赖。

**修复**（`backend/app/platform/auth.py`）：
- `get_current_user`：吊销名单/用户存活查询包 try/except，DB 异常 → **fail-open 信任 JWT claims**（构造轻量 User），打 WARNING 日志；签名校验（本地 HS256）仍严格——篡改 token 照常 401，安全边界不被故障削掉
- `login`：DB 异常 → **503 SERVICE_UNAVAILABLE**（明确"认证服务暂不可用"）
- `ops/router.py`：审批列表/审批决策 DB 异常 → **503 明确提示**（审批暂停不雪崩）

**新增测试**：`backend/tests/test_chaos_degrades.py`（5 用例：fail-open 通过、坏签名仍 401、refresh 当 access 仍 401、login 503、错误码契约）

---

## 三、演练二：杀 Redis（上午第二连）

### 3.1 故障注入

```powershell
docker stop scm-redis
```

### 3.2 预期 vs 实际

| 观测点 | 预期 | 实际 | 判定 |
|---|---|---|---|
| `GET /health` | 仍 200（Redis 非 health 项） | 200 ok/db=up | ✅ |
| 幂等（ops 改单审批链） | fail-open 降 SQLite，幂等语义成立 | `exec_count=1, hit1=False, hit2=True`（同 key 只执行一次） | ✅ |
| 查询缓存 | 降内存兜底，TTL 内仍命中 | `query_cache hit=True val={'v':1}` | ✅ |
| 分布式锁 | fail-open 放行（不卡死） | `acquired=True` + 日志 `[LOCK] Redis 不可用 → fail-open 放行` | ✅ |
| 调度 leader 锁 | fail-open 放行（任务幂等兜底） | 日志 `[SCHED] leader 锁 Redis 不可用 → fail-open 放行` | ✅ |
| API Key 令牌桶 | fail-open 放行（配额软约束） | 业务端点全 200 | ✅ |
| **恢复** | **自动切回无缝（无需重启 backend）** | `redis available: False → True`（懒连接 + 冷却探测） | ✅ |

### 3.3 结论

Redis 挂时全链路 fail-open 生效，**零服务中断**；恢复后 redis_client 懒连接 + 冷却探测自动切回，无需重启 backend。这是 W21 Day3 就铺好的 fail-open 语义（幂等→sqlite、缓存→内存、锁→放行）的**全量实证**。

---

## 四、演练三：杀 Qdrant（下午第一连）

### 4.1 故障注入

```powershell
docker stop w5-qdrant   # QDRANT_URL 指向宿主 6333
```

### 4.2 预期 vs 实际

| 观测点 | 预期 | 修复前实际 | 修复后实际 |
|---|---|---|---|
| KB 域检索 | 降级 BM25-only，degraded 标记 | **32.5s 重试后抛异常** ✗ | **4.1s 返回 BM25-only**（预热后）✅ |
| 结果标记 | `source=bm25-degraded` + `degraded=True` | 无（抛异常） | ✅ `bm25-degraded True` |
| 恢复后 | 混合检索自动回 | — | `source∈{vec,both,bm25}`、`degraded=False`（无需重启）✅ |

### 4.3 根因与修复

**根因 1**：`HybridRetriever.retrieve()` 向量路 `store.query` 抛异常直接上抛，无降级。**修复**：捕获向量路异常 → 只用 BM25 结果，每条带 `source=bm25-degraded` + `degraded=True`（召回降级标记进响应/日志）。

**根因 2**：`store.query` 固定 4 次重试（1+3）指数退避 → Qdrant 挂时 30s 级才失败，拖垮响应（"降级不雪崩"要求快速失败）。**修复**：`retries` 参数可配，HybridRetriever 传 `retries=0` 快速失败。

**根因 3**：qdrant_client 每次查询前做 server version 检查（挂时先等待超时）。**修复**：`check_compatibility=False`。

**新增测试**：`backend/tests/test_hybrid_retriever_degrade.py`（3 用例：retries 参数签名、向量失败→BM25-only+标记、恢复→混合路）

---

## 五、演练四：LLM 全超时（下午第二连）

### 5.1 故障注入（本地 venv 验证 real 降级链）

```powershell
$env:LLM_PROVIDER="real"; $env:LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:LLM_API_KEY="sk-invalid-key-for-chaos-drill-0000"; $env:LLM_TIMEOUT="2"
$env:LLM_MODEL_POOL="glm-5.2,deepseek-chat,invalid-model-x"; $env:LLM_DEGRADE_TO_MOCK="1"
python deploy/chaos/llm_deg_check.py
```

### 5.2 预期 vs 实际

| 观测点 | 预期 | 修复前实际 | 修复后实际 |
|---|---|---|---|
| 模型池三级切换全失败 | 全失败后降级 | 逐模型 401 快速失败 ✅（`_post_chat` 池内循环） | 同左 ✅ |
| `generate` | `[WARNING]` 前缀 mock 兜底（明确告知降级） | ✅ `[WARNING] real 失败降级: ...` | 同左 ✅ |
| `generate_json` | mock dict 兜底（守 JSON 契约） | **返回 str** ✗ | **dict：`{"answer","citations","degraded":True,"degrade_reason"}`** ✅ |
| usage 记账不重复 | 失败模型不累计，仅兜底记一次 | — | ✅ 降级路径只调一次 mock，cost_usage 无失败模型堆积 |

### 5.3 根因与修复

**根因**：`RealLLMProvider._degrade_or_raise` 一律返回 `[WARNING]` 前缀 str——但 `generate_json` 调用方契约要求 dict，降级后下游 JSON 解析必炸。

**修复**：按 `tag` 分派——`generate_json` 走 mock 的 `generate_json`（dict）+ `degraded=True` 标记；`generate`/`stream` 保持 `[WARNING]` str。**降级也必须守住接口契约**。

**新增测试**：`backend/tests/test_llm_degrades.py`（3 用例：generate_json 降级 dict、generate 降级 str、usage 不重复）

---

## 六、演练五：实例半瘫（下午第三连）

### 6.1 故障注入（压测中段杀 backend-a1）

```powershell
python deploy/load_test.py --concurrency 30 --per 7 --kill-instance a1 --kill-at-pct 0.4 --out deploy/reports/day2_drill_kill_a1.json
```

### 6.2 预期 vs 实际

| 观测点 | 预期 | 实际 | 判定 |
|---|---|---|---|
| least_conn 摘除 | a1 停止后自动摘除 | nginx upstream 自动切换（`proxy_next_upstream`） | ✅ |
| 5xx = 0 | 压测全程无 5xx | `HTTP_5xx=0` | ✅ |
| 成功率 | 100% | **210/210 = 100.0%** | ✅ |
| 流量集中 a2 | Grafana/Prometheus 曲线可见 | Prometheus `http_requests_total` a2 计数远高于 a1（a1 停止窗口 0 增量） | ✅ |
| 恢复后自动回归 | docker start 后 healthcheck 恢复 | `scm-backend-a1 Up (healthy)` + Prometheus `up=1` | ✅ |

### 6.3 证据

- 压测报告：`deploy/reports/day2_drill_kill_a1.json`（210/210 / 5xx=0 / P95=2928ms）
- Prometheus targets：杀时 a1 `down` → 恢复 `up`（`deploy/chaos/prom_check.py`）
- Grafana 截图：`deploy/reports/w26_day2_chaos_kill_instance.png`

---

## 七、当天修复汇总（预案外行为逐条修）

| # | 暴露问题 | 修复文件 | 修复内容 | 新增测试 |
|---|---|---|---|---|
| 1 | MySQL 挂 → 全部受保护端点 500（认证查库单点依赖） | `app/platform/auth.py` + `app/domains/ops/router.py` | get_current_user fail-open 信任 claims；login/审批 503 明确提示 | `test_chaos_degrades.py` 5 用例 |
| 2 | Qdrant 挂 → 32.5s 重试后抛异常（无 BM25-only 降级） | `app/shared/rag/hybrid_retriever.py` + `store.py` | 向量路异常 → BM25-only + degraded 标记；retries 可配快速失败；check_compatibility=False | `test_hybrid_retriever_degrade.py` 3 用例 |
| 3 | LLM 全失败 → generate_json 降级返回 str 破坏 JSON 契约 | `app/shared/llm/real_provider.py` | 按 tag 分派：generate_json 返回 mock dict + degraded 标记 | `test_llm_degrades.py` 3 用例 |

**ADR 修订记录**（追加到《04》）：
- **ADR 修订-3（W26 Day2）**：认证的存储依赖从"强依赖"调整为"fail-open"——JWT 签名本地校验是安全边界（篡改仍 401），吊销名单/用户存活是查库增强项（DB 挂时降级信任 claims，恢复自动回查）。安全收益不降（签名校验不妥协），可用性大幅提升（存储故障不拖垮全部请求）。
- **ADR 修订-4（W26 Day2）**：LLM 降级链必须守住接口契约——`generate_json` 降级返回结构化 dict（mock 引用结构 + degraded 标记），而非通用 `[WARNING]` 文本；"降级是响应语义，不是类型破坏"。

---

## 八、回归验证（修复后全量）

```bash
pytest backend/tests -q          # 320 passed（原 312 + 新增 8）
ruff check backend scripts        # 0 error
mypy --explicit-package-bases --namespace-packages backend scripts  # 0 error（178 files）
```

容器验证：backend 镜像重建（含 3 处修复）→ 双实例重启 → `/health` 200 ok/db=up → 登录 200 → 高危改单 approval_request 正常 → Prometheus 双 target up。

---

## 九、面试题 0.5h：完整口述"故障演练五连"

> **叙事框架（W19–W22 铺的降级链，今天全部兑现——"纵深防御"最强素材）**：

- **S**：上线前最怕"依赖一挂全平台雪崩"。我把 MySQL/Redis/Qdrant/LLM/实例 五类故障逐一杀掉，验证每条降级链。
- **T**：五连演练 + 判定标准写死（探活 5xx<5%、无级联超时、恢复<2min 自动回归），演练当天修复暴露的问题。
- **A**：
  1. **杀 MySQL**：认证 fail-open（签名本地校验 + claims 权限，DB 挂不 500）、审批 503 明确暂停、恢复后 HITL 断点续跑；
  2. **杀 Redis**：幂等降 SQLite（同 key 只执行一次）、缓存降内存、锁放行——fail-open 全链路，恢复无缝切回；
  3. **杀 Qdrant**：向量路失败 → BM25-only 降级（4.1s，degraded 标记进响应），恢复混合检索自动回；
  4. **LLM 全超时**：模型池三级切换全失败 → mock 兜底话术（明确告知降级），usage 记账不重复；
  5. **实例半瘫**：压测中杀一个实例，least_conn 摘除、5xx=0、流量集中健康实例，恢复自动回归。
- **R**：5/5 不雪崩 + 当天修 3 处真 bug（认证 fail-open / BM25-only 降级 / generate_json 契约）+ 320 测试全绿 + 每项有 Prometheus/Grafana/压测报告证据。

**一句话**：降级链不是 PPT 上的箭头，是 W19–W22 一行行写出来的 fail-open 语义，今天用 docker stop 逐条验证——**每个故障都有日志、有指标、有自动恢复**。

---

## 十、Day2 成功标准逐项勾

- [x] `deploy/chaos/` 五脚本（kill_mysql/redis/qdrant/llm_timeout/kill_instance）+ 探活脚本 + Grafana 截图脚本
- [x] 演练一杀 MySQL：5/5 降级不雪崩 + 恢复 HITL 续跑 + **修复认证 500→fail-open/503**
- [x] 演练二杀 Redis：fail-open 降 SQLite/内存 + 恢复自动切回（零重启）
- [x] 演练三杀 Qdrant：BM25-only 降级（degraded 标记）+ 恢复混合检索自动回 + **修复 32.5s→4.1s**
- [x] 演练四 LLM 全超时：模型池切换全失败 → mock 兜底 + **修复 generate_json 契约**
- [x] 演练五实例半瘫：least_conn 摘除、5xx=0、210/210、恢复自动回归
- [x] 当天修复 3 处问题（8 个新测试）+ 全量回归 320 passed + ruff/mypy 0 error
- [x] `chaos_drill.md` 五连记录完整（故障/预期/实际/修复/证据）
- [x] 演练后全栈恢复健康（health ok、登录 200、双 target up、Grafana 截图归档）

---

## 十一、Day3 衔接

| 项 | 准备 |
|---|---|
| 全量验收（9/9） | 演练后全栈已重启干净；压测前 `make reseed-biz` 重置演示数据 |
| 压测终版 | 20/40 并发阶梯 × 混合路径（Day1 已跑 20 并发基线） |
| 夜间回归 | 连续 7 晚数据已积累，Day3 回填 |

> 演练暴露的三处问题全部闭环（修复 + 测试 + 镜像重建），无欠账进 Day3。
