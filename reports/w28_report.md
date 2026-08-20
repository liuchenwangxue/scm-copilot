# W28 Day1 报告 · 容器口径统一（阶段五 · 口径统一与二期功能 第 1 天）

> 阶段五 SCM Copilot 第 2 周 Day1 ｜ 2026-08-21 ｜ 依据《W28学习执行手册》Day1
> 主题：模型入容器 + 评测/压测/演示三口径合一 + `reports/w28_report.md` 第一节落账
> **Day1 验收：容器内 eval 分差 ≤2pp（实测 0pp）✓ / 两实例 health 可见模型状态 ✓ / 语义缓存容器内命中 ✓**

---

## 〇、Day1 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | 模型进容器（bge-small + bge-reranker，named volume 卷挂载） | ✅ 镜像 + `scm_model_cache` 卷就位 |
| 2 | 启动健壮性：加载失败 → mock embedder/RuleReranker 降级 + WARNING + /health 暴露 | ✅ 代码 + 单测（`test_embedder_mode.py` 8 用例） |
| 3 | reranker 分级降级：a1 挂 1.1GB bge、a2 保持 rule，/health 可见差异 | ✅ `a1=real/bge`、`a2=real/rule` |
| 4 | 容器内 eval（RAG 156 条）与本机分差 ≤2pp | ✅ **0pp**（0.9038 = 0.9038） |
| 5 | 容器内 30 并发压测 P95 不劣化超 W27 基线 ×1.3 | ✅ 892ms ≤ 928ms（714×1.3） |
| 6 | 语义缓存容器内命中（真实 bge） | ✅ sim=0.9868 命中 |
| 7 | 全量回归 + 覆盖率 + ruff/mypy | ✅ 545 passed（+8）/ 65% / 0 error |
| 8 | `w28_report.md` 第一节"容器内外评测对照表" | ✅ 本报告 |

> **过程性重大发现（面试素材）**：真 bge 装进容器后，压测暴露了两个 W27 时被"模型缺失→路由降级"掩盖的真 bug——
> ① 语义路由 bootstrap 聊天原型覆盖不足：完整寒暄句（"你好呀，你能做什么？"）被真实 embedding 误判 rag → 触发 RAG 检索 + reranker 5.7s，并发下排队到 **38~64s**；
> ② 语义缓存 `lookup` 对所有请求执行（含规则层可判定的 chat/tool），每次白做一次真 embedding 推理。**两处均已修复**（见 §五），修复后净环境 P95 38s→892ms。

---

## 一、结论速览（Day1 验收 Gate）

| 手册 Day1 验收项 | 判定 | 证据 |
|---|---|---|
| 容器内 eval 与本机分差 ≤2pp（hit@1 本机 0.9038，容器 ≥0.88） | ✅ **0pp**，hit@1=0.9038 / recall@5=0.9936 / citation=0.9754 | `deploy/verify_eval_container.py`（容器内 156 条实测） |
| 两实例 health 可见模型状态 | ✅ a1 `embedder=real, reranker=bge`；a2 `embedder=real, reranker=rule` | `GET /health` 双实例实测 |
| 语义缓存容器内开启并命中 | ✅ `semantic_cache=on`；相似问 sim=0.9868 命中、无关问 miss | `deploy/verify_semcache_container.py` |
| 容器内 30 并发压测不劣化（≤ W27 基线 ×1.3 = 928ms） | ✅ 净环境 P95=**892ms**，QPS=36.7，100% 成功率、5xx=0 | `deploy/reports/w28d1_load_30_clean_v3.json` |

---

## 二、模型进容器（上午 1）

### 2.1 改动清单

| 项 | 改动 | 文件 |
|---|---|---|
| 镜像依赖 | 新增 `torch --index-url cpu` + `sentence-transformers`（层缓存：COPY 之前安装，业务代码改动不触发重装） | `deploy/backend/Dockerfile` |
| 离线守卫 | `SENTENCE_TRANSFORMERS_OFFLINE=1` / `HF_HUB_OFFLINE=1`（手册坑：首次 import 联网查版本会卡死） | Dockerfile ENV |
| 模型卷 | `model-cache:/root/.cache`（named volume，非 bind mount——Windows Desktop 大模型 bind IO 慢坑） | `deploy/docker-compose.yml` |
| 卷内容 | 本机已下载的 `bge-small-zh-v1.5`（~100MB）+ `bge-reranker-base`（~1.1GB）+ 历史 bge-base 等 4 模型拷入 `scm_model_cache` 卷 | 卷初始化（docker run cp） |
| 环境变量 | `SCM_EMBEDDER=real`、`SCM_RERANKER=bge(a1)/rule(a2)`、`SEMANTIC_CACHE_ENABLED=1` | compose backend-a1/a2 |
| 模型探测 | 新增进程级模型状态注册表 `shared/rag/model_status.py`；/health 首次探活 `probe_if_pending()`（幂等）后缓存 | 新增模块 |
| Qdrant 通路 | 容器内 `QDRANT_URL=http://host.docker.internal:6333`——w5-qdrant（本机 6333，`scm_kb_v1` collection 所在），保证"容器内外同语料"；生产替换为 compose 内专用服务 | compose environment |

> 面试题素材：**镜像体积 vs 运行时下载模型**——学习项目卷挂载零成本（镜像不膨胀）；生产镜像内嵌保证不可变部署（`HF_ENDPOINT=https://hf-mirror.com` 中国网络源）；权衡轴 = 部署原子性 vs 体积/构建时长。

### 2.2 镜像体积与依赖实证

```
docker run --rm scm-backend:latest python -c "import torch, sentence_transformers"
→ torch 2.13.0+cpu ｜ sentence_transformers 6.0.0 ｜ HF_HUB_OFFLINE=1
```

模型文件不入镜像（走卷），镜像增量 ≈ 依赖包体积（torch CPU ~200MB + st/transformers），手册六问"镜像增量 ≤1.5GB"口径内。

---

## 三、启动健壮性：降级哲学贯穿（上午 2）

### 3.1 Embedder / reranker 状态机

| 组件 | pending → 状态 | 触发降级 |
|---|---|---|
| Embedder | `real`（真模型）→ `mock`（主动选择 SCM_EMBEDDER=mock）→ `mock_degraded`（real 加载失败自动回退） | 任何加载异常（卷未挂/下载不全/缺依赖）→ `_load` 抛错 → 大写 WARNING + mode=mock，**服务不崩** |
| Reranker | `pending` → `bge`（bge-reranker-base）→ `rule`（SCM_RERANKER=rule）→ `bge-failed→rule` | transformers 加载异常自动降 RuleReranker，`_load_error` 记录 |

- `/health` 新增字段：`embedder` / `reranker` / `semantic_cache`（schemas.HealthOut + main.health）
- 首次探活触发一次模型加载探测（bge-small ~3s + reranker ~5-10s）→ healthcheck `start_period` 放宽至 60s
- 探测幂等：非 pending 后不再重载（高频探活不烧 CPU）

### 3.2 单测覆盖（`test_embedder_mode.py` 8 用例）

| 用例 | 验证点 |
|---|---|
| mock 模式 4 项 | status=mock / 512 维确定性向量 / 同输入同输出 / L2 归一化 / batch 契约 |
| real 加载失败 1 项 | 注入 `_load` 抛错 → 自动降级 `mock_degraded` + load_error 记录 + 接口仍可用 |
| model_status 注册表 2 项 | record/snapshot；`probe_if_pending` 探测一次后幂等 |

---

## 四、容器内外评测对照表（★核心产物，下午 4）

### 4.1 RAG 156 条（`rag_eval_v2.json`，mock provider 同口径）

| 指标 | 本机基线（W25 首份报告） | 容器内（W28 D1 实测） | 分差 | 判定 |
|---|---|---|---|---|
| **hit@1** | **0.9038** | **0.9038** | **0pp** | ✅ ≤2pp |
| recall@5 | 0.9936 | 0.9936 | 0pp | ✅ |
| citation_accuracy | 0.9754 | 0.9754 | 0pp | ✅ |
| 检索 P50/P95 | 12.4s（W25 夜跑，reranker 交叉编码） | 5.39s / 6.25s | — | 容器 CPU 交叉编码固有成本 |

> 实测命令：`docker exec scm-backend-a1 python /app/verify_eval_container.py`（同 eval_nightly 链路：`HybridRetriever(reranker=get_reranker())` + EvalRunner top_k=5）。
> **分差 0pp 的意义**：容器内外用同一 Qdrant collection（`scm_kb_v1`）、同一批模型权重、同一检索链路——"本机好用容器缩水"的暗坑关闭，压测/评测/演示三口径合一。

### 4.2 语义缓存容器内命中（真实 bge embedding）

| 场景 | 结果 |
|---|---|
| put "采购申请需要经过哪几级审批" → lookup 相似问 | ✅ **sim=0.9868, char_overlap=0.9231**，双闸门命中 |
| lookup "今天天气怎么样？"（无关） | ✅ miss |
| Redis 权威 | ✅ available=True，key 写入共享 Redis |

---

## 五、★ 压测暴露的两个真 bug 及修复（下午 5 过程中）

### 5.1 现象

W27 时容器内**无 embedding 模型**，语义路由 embedding 路径异常降级 → 压测"虚快"（kb_chat 走 chat 规则层）。装真 bge 后"假死变真活"，真实分类暴露两个问题：

| 轮次 | 总 P95 | kb_chat P95 | 根因 |
|---|---|---|---|
| 修复前（混合环境） | 35.9~38.1s | 33~58s | ① 语义路由误判 rag → RAG+reranker 5.7s，并发排队 |
| 单发复现 | 6.8s/次 | — | "你好呀，你能做什么？"→ route=rag, sim=0.5257 < chat 阈值 0.85 |
| 修复后（混合环境） | 1.61s | 906ms | 两处修复生效 |
| 修复后（净环境 v3 warm） | **892ms** | 700ms | ✅ 达标 |

### 5.2 根因与修复

**① 语义路由 bootstrap 聊天原型覆盖不足**（`semantic_router.py`）
- 根因：聊天类手打原型只有极短精确词（你好/再见…），完整寒暄句与 chat 原型相似度仅 ~0.52 → 被"宽容阈值"兜进 rag 默认域 → 白烧 RAG 检索 + reranker。
- 修复：规则层扩充 `_CHAT_PHRASES`（10 条长聊天表述子串：你能做什么/你是做什么的/很高兴认识你/你好呀…）——**规则优先层零 embedding 拦截，chat 是零检索零 token 分支**（设计哲学：精确到高置信模式，不用裸关键词）。
- 新增测试 `test_router_chat_long_phrase_rules`（4 句命中 rule + 制度问不进 chat）。

**② 语义缓存 lookup 对所有请求执行**（`domains/kb/router.py`）
- 根因：`SEMANTIC_CACHE_ENABLED` 时缓存查询在路由**前**执行，chat/tool/data 请求也各做一次真 embedding（~100ms/次，30 并发排队）。
- 修复：缓存查询移到语义路由**后**，仅 rag 分支查缓存（put 本来只在 rag 分支落库，查询也随之只服务 rag——语义一致）。
- 现有 `test_kb_semantic_router` 缓存用例回归绿。

> 叙事价值：这恰是手册 C1 的目的——**容器内外同口径后，靠真实评测暴露了"环境依赖掩盖的逻辑缺陷"**，修复后数字（38s→892ms）有前后对比、根因可解释、测试有回归。

---

## 六、容器内 30 并发压测（下午 5）

口径与 W27 完全一致（`deploy/load_test.py --concurrency 30 --per 7`，nginx 双实例、LLM_PROVIDER=mock、热身后跑）：

| 环境 | 轮次 | 总 P95 | ops P95 | kb_tool P95 | kb_chat P95 | QPS | 成功率 |
|---|---|---|---|---|---|---|---|
| W27 净环境基线 | — | **714.1ms** | 832ms | 398ms | 398ms | 36.32 | 100% |
| W28 混合环境（修复后） | v3 | 1612ms | 2422ms | 895ms | 906ms | 28.21 | 100% |
| **W28 净环境（达标轮）** | **v3 warm** | **892.0ms** | 1197ms | 662ms | 700ms | **36.7** | **100%** |

> 判定：净环境 P95=892ms ≤ 928ms（714×1.3），**达标**；QPS=36.7 与基线持平（36.32），无模型劣化。
> 注：v2 轮（重启后首次）出现 5 个 502（97.6%）——模型 warm 前的瞬时冷启动窗口，v3 复测 100% 无 5xx（W27 手册"热身轮排除冷启动"同口径）。

---

## 七、质量门

| 项 | 结果 | 说明 |
|---|---|---|
| 全量回归 | **545 passed**（W27 537 → +8） | 新增 `test_embedder_mode.py` 8 用例 |
| 覆盖率（完整口径，含 integration） | **65%** | W27 66%，-1pp：新增 model_status.py / embedder real 分支等未全额覆盖，D6 冲 75% 时补 |
| ruff | **0 error** | `ruff check backend/app backend/tests` |
| mypy | **0 error** | `mypy backend/app` 122 文件 |
| 镜像/卷 | 依赖实证 + 卷初始化成功 | 见 §2.2 |

---

## 八、欠账核对清单（Day1）

| 项 | 状态 | 说明 |
|---|---|---|
| C1 容器口径统一 | ✅ 清 | 分差 0pp；语义缓存容器内命中；/health 两实例模型状态可见 |
| 压测暴露 2 bug | ✅ 清 | 语义路由聊天规则层扩充 + 缓存查询位置修正，均带单测回归 |
| 覆盖率 65%（-1pp） | ⚠️ 挂 D6 | 新增模块未全额覆盖，D6 冲 75% 一并补 |
| 生产 Qdrant 通路 | ⚠️ 文档化 | 当前走 `host.docker.internal` 复用本机 w5-qdrant（同语料同口径）；生产替换为 compose 内专用 qdrant 服务（ADR/三期） |
| TestPyPI 上传 | ⚠️ 挂起 | W27 fallback 口径，W28 若注册成功补上传 |

---

## 九、Day1 成功标准逐项勾（手册 Day1 验收）

- [x] 模型进容器（卷挂载，named volume；SCM_EMBEDDER=real；SCM_RERANKER a1=bge / a2=rule）
- [x] 启动健壮性：加载失败自动回退 mock/RuleReranker + 大写 WARNING + /health 暴露（`test_embedder_mode.py` 覆盖）
- [x] 容器内 eval 分差 ≤2pp（实测 0pp，hit@1=0.9038，recall@5=0.9936，citation=0.9754）
- [x] 语义缓存容器内命中（sim=0.9868 命中 / 无关 miss / Redis 权威）
- [x] 容器内 30 并发压测 P95 ≤ W27 基线 ×1.3（净环境 892ms ≤ 928ms）
- [x] `w28_report.md` 第一节"容器内外评测对照表"落账

**→ Day1 通过，进入 W28 Day2（Gradio 前端三页）**

---

## 十、Day1 结语

> **把模型装进容器，数字才第一次说了真话。**
>
> W27 压测的"干净"其实是环境缺模型的"假干净"——装真 bge 后第一轮压测就爆出 38s 长尾，根因不在模型推理（embedding 单次 ~110ms），而在语义路由 bootstrap 原型覆盖不足 + 语义缓存对所有请求空转。两处都是"环境依赖掩盖的逻辑缺陷"，被同口径评测暴露、用规则优先层哲学修复（零 embedding 拦截寒暄、缓存只服务 rag 分支），净环境 P95 回到 892ms、QPS 与 W27 基线持平。
>
> 这正是指南里 C1 的完整叙事：**口径统一不是"数字更好看"，而是"数字第一次可信"。**
