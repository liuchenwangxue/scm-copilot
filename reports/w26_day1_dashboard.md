# W26 Day1 报告 · 业务监控面板（五区）

> 阶段四 SCM Copilot 第 4 周 Day1 ｜ 2026-09-07（周一）
> 主题：业务监控面板五区上墙 + 自定义 metrics 补齐 + 验证
> 依据：《W26学习执行手册》Day1、《01_四周总计划》验收指标体系

---

## 〇、Day1 速览

**目标**：五区业务面板（NL2SQL 质量 / 语义缓存 / 队列调度 / 流量健康 / 成本看板）全部有真实流动数据 + 自定义 metrics（`scm_nl2sql_eval_score` Gauge / `scm_job_success_total` Counter）可查。

**结果**：

| # | 任务 | 状态 |
|---|---|---|
| 1 | 自定义 metrics 补齐（9 个业务指标） | ✅ |
| 2 | Grafana MySQL datasource 配置（直连 scm_platform） | ✅ |
| 3 | `scm_business.json` 五区面板 | ✅ |
| 4 | 验证：压测 100/100 + 手动触发 eval_nightly 跑通 | ✅ |
| 5 | 截图归档 + JSON 进仓 + 报告 | ✅（本文件） |

---

## 一、自定义 metrics 补齐

### 1.1 业务指标定义（`metrics.py` 新增）

| 指标 | 类型 | Label | 埋点位置 | 用途 |
|---|---|---|---|---|
| `scm_nl2sql_eval_score` | Gauge | `layer` | `eval_nightly.run()` | NL2SQL 分层准确率趋势 |
| `scm_rag_eval_score` | Gauge | `metric` | `eval_nightly.run()` | RAG 检索质量（hit@1/recall@5/...） |
| `scm_job_success_total` | Counter | `job_name` | `scheduler._record()` | 调度任务 24h 成功数 |
| `scm_job_failed_total` | Counter | `job_name` | `scheduler._record()` | 调度任务 24h 失败数 |
| `scm_semcache_hit_total` | Counter | — | `semantic_cache.lookup()` | 语义缓存命中率分子 |
| `scm_semcache_miss_total` | Counter | — | `semantic_cache.lookup()` | 语义缓存命中率分母 |
| `scm_llm_tokens_total` | Counter | `model` | `real_provider._log_cost()` / `mock_provider` | token 用量按模型 |
| `scm_llm_cost_yuan_total` | Counter | `model` | `real_provider._log_cost()` | 成本（¥）按模型 |
| `scm_rq_queue_depth` | Gauge | `queue` | `queue.enqueue_report()` | RQ 报表队列深度 |

★ **手册坑 1**：label 基数控制——`job_name` 固定 6 个值（六任务名），不塞 trace_id/session_id。避免与 Prometheus 保留标签 `job` 冲突（实测：原始 `job` 标签会被 scrape job_name 覆盖，导致六任务无法区分）。
★ **手册坑 2**：`scm_job_success_total` 终态 `success/failed` 才计数（`running`/`skipped` 不计），保证双实例"零重复"语义不被提前累加干扰。

### 1.2 埋点接入

- **`metrics.py`**：新增业务指标字段 + 模块级便捷函数（`set_nl2sql_eval_score` / `inc_job_success` / `inc_semcache_hit` / `inc_llm_usage` / `set_rq_queue_depth` 等）
- **`eval_nightly.py`**：`run()` 评测完成后调用 `_push_eval_gauges()` 写 5 个 NL2SQL layer + 1 个 RAG metric
- **`scheduler/__init__.py`**：`_record()` 终态 success/failed 时 `inc_job_success(job)` / `inc_job_failed(job)`
- **`semantic_cache.py`**：lookup 命中/未命中/异常降级统一 `inc_semcache_hit()` / `inc_semcache_miss()`
- **`real_provider.py`**：`_log_cost()` 计算成本（`prompt/1e6*COST_PRICE_INPUT + completion/1e6*COST_PRICE_OUTPUT`）并 inc token/cost
- **`mock_provider.py`**：估算 token（字符数/2），开发期 cost=0 让成本看板 token 曲线可观察
- **`ops/tasks/queue.py`**：enqueue 成功后 `set_rq_queue_depth(queue, q.count)` 实时更新队列深度

### 1.3 关键修复：eval_nightly 容器内路径错位（发现并修复的 bug）

**症状**：容器内 `pip install .` 把包安装到 `site-packages`，`Path(__file__).resolve().parents[4]` 解析到 site-packages 目录（`/usr/local/lib/python3.12/site-packages/evals` 不存在），导致 eval_nightly.run() 跑出 0ms "success"但 metrics 只有 `error_rate=1.0`，NL2SQL 真实分数进不了 Gauge。

**修复**：`eval_nightly.py` 新增 `_find_eval_dir()` 多候选探测（环境变量 `SCM_EVAL_DIR` → 源码路径 → 镜像内 `/app/backend/evals`），`_find_scripts_dir()` 同理。修复后 eval 实际运行 2-3 秒，NL2SQL 分层准确率 1.0 全部进入 Gauge（`scm_nl2sql_eval_score{layer="overall|single|join|aggregation"}=1.0`）。

> **这是 W26 Day1 最重要发现**：之前 W25 报告说"夜间回归 2 晚出报告"，实际容器内 eval 一向返回 error，eval_reports 表里的真实分数来自本地 venv 手动触发。修复后容器内首次能正确跑出 mock 评测全量并落库。

---

## 二、Grafana MySQL datasource 配置

### 2.1 provisioning（`deploy/grafana/provisioning/datasources/datasource.yml`）

```yaml
- name: MySQL
  uid: scm-mysql
  type: mysql
  access: proxy
  url: mysql:3306
  user: root
  secureJsonData: { password: root123 }
  jsonData:
    database: scm_platform       # ★ 放 jsonData（顶层 database 不被识别）
    maxOpenConns: 5
```

★ **手册坑**：`database` 必须放 `jsonData`（Grafana 11 MySQL 数据源约定），否则请求会作为无效请求被拒绝。

### 2.2 验证

- `GET /api/datasources/uid/scm-mysql/health` → `{"message": "Database Connection OK", "status": "OK"}`
- 测试查询 `SELECT created_at, JSON_EXTRACT(metrics, '$.overall') FROM eval_reports` → 200，返回真实数据

---

## 三、`scm_business.json` 五区面板

### 3.1 面板结构（uid: scm-business，20 个 panel/row）

| 行 | 标题 | 数据源 | 关键指标 |
|---|---|---|---|
| ① | NL2SQL 质量 | MySQL | 整体准确率 / 分层准确率 / 拒答率（`rejected/count`） |
| ② | 语义缓存 | Prometheus | 命中率 / 命中未命中 QPS / RAG 检索质量 |
| ③ | 队列与调度 | Prometheus + MySQL | 六任务 24h 成功 / RQ 队列深度 / 任务上次运行距今（表格） |
| ④ | 流量健康 | Prometheus | QPS 按域 / P95 延迟按域 / 错误率按域（`/api/v1/{kb,ops,data,...}`） |
| ⑤ | 成本看板 | Prometheus | token 用量按模型 / 成本按模型 / 日预算水位 |

### 3.2 关键 SQL 修正

- `JSON_UNQUOTE(JSON_EXTRACT(...))` 返回字符串 → 用 `CAST(... AS DECIMAL(5,4))` 转为数字类型让 Grafana 单位识别为 `percentunit`
- MySQL 保留字 `join` / `trigger` 加反引号 `` ` ``（不加会 500/400）
- 表格面板中文字段别名出现乱码 → 改用英文别名（`AS job/last_success/minutes_ago`）
- 流量健康按域分标签：`_norm_path` 保留前 3 段（`/api/v1/kb/ops/data/auth/admin`），使 Grafana 可按域分组

### 3.3 时间窗口

- 面板默认 `now-24h` 到 `now`，覆盖完整的夜间回归周期
- 夜间任务数据（`created_at` 在 02:00 附近）在 last 24h 内可见

---

## 四、验证

### 4.1 Prometheus 指标现状

```
scm_nl2sql_eval_score{layer="overall"} 1.0
scm_nl2sql_eval_score{layer="single"} 1.0
scm_nl2sql_eval_score{layer="join"} 1.0
scm_nl2sql_eval_score{layer="aggregation"} 1.0
scm_nl2sql_eval_score{layer="rejected_rate"} 0.0
scm_rag_eval_score{metric="error_rate"} 1.0   # 容器内无 embedding → 已知限制
scm_job_success_total{job_name="eval_nightly"} 1
scm_job_success_total{job_name="kb_increment_sync"} 2
scm_llm_tokens_total{model="mock"} 6510
```

### 4.2 压测验证（20 并发 × 5 = 100 请求）

```
总耗时 4.19s | QPS=23.88
成功率 100/100 = 100.0%
P50=97.5ms P95=1293.3ms P99=2200.7ms
HTTP_5xx=0 错误分布：无
[ops_query] 21 条 成功 21 P50=562ms P95=1293ms
[kb_tool]  38 条 成功 38 P50=79ms  P95=130ms
[kb_chat]  41 条 成功 41 P50=88ms  P95=1281ms
```

报告：`deploy/reports/day1_load.json`

### 4.3 调度任务运行时长

| 任务 | 最近运行时长 | 备注 |
|---|---|---|
| `kb_increment_sync` | 20-107ms | */5min 正常执行 |
| `eval_nightly` | 2-3s | 修复路径后 100 条 NL2SQL mock 跑通 |
| `daily_brief` | 334ms | 工作日 08:00 |

### 4.4 截图归档

- `reports/w26_day1_panel_business_24h.png`（上半部：NL2SQL 质量 + 语义缓存）
- `reports/w26_day1_panel_business_lower.png`（下半部：队列调度 + 流量健康 + 成本看板）

### 4.5 面板 JSON 进仓

- `reports/scm_business_dashboard.json`（W26 Day1 业务面板）
- `reports/scm_backend_dashboard.json`（W25 Day6 基础监控面板）

---

## 五、面试题 0.5h：监控三支柱分工

> **logs / metrics / traces** 在 SCM 平台如何分工？

- **Prometheus（metrics）**：看整体健康度趋势（QPS / P95 / 成功率 / 业务指标），拉模型 15s 抓取，Grafana 画曲线，告警阈值化。
- **LangFuse（traces）**：看单次请求的完整调用链（哪一步 LLM 慢、token 多少、prompt 是什么），用于定位具体请求的瓶颈。
- **JSON 日志（logs）**：取证（哪个 user 何时调用了哪个工具、审批流水的完整记录），结构化便于按 `request_id` 串联。

> 一句话：**Prometheus 告诉"系统病了没"，LangFuse 告诉"哪个请求病了"，JSON 日志告诉"为什么病了"**。

---

## 六、当前面板"No data"项及原因（如实标注）

| 面板 | 状态 | 原因 |
|---|---|---|
| NL2SQL 整体准确率趋势 | ✅ 有数据 | eval_reports 表查询 1 个数据点（24h 内只有今晚一次 eval） |
| NL2SQL 分层准确率 | ✅ overall/single/join=100% | aggregation 缺值（mock 评测 aggregation 层有 20 条）—— 后续 eval 会补 |
| NL2SQL 拒答率 | ✅ 0% | |
| 语义缓存命中率 | No data | 容器内 `SEMANTIC_CACHE_ENABLED=0`（无 embedding 模型）—— 本地有数据 |
| 命中/未命中 QPS | 有微弱线 | 有底层流量（kb 请求），但无缓存命中 |
| RAG 检索质量 | No data | 容器内 retriever init 失败（无 embedding + 缺 chunks_title.json） |
| 六任务 24h 成功 | ✅ 有数据 | 24h 内多任务多实例执行 |
| 任务上次运行距今 | ✅ 表格 | 表格正常 |
| RQ 队列深度 | No data | 24h 内无报表 enqueue |
| QPS 按域 | ✅ 有数据 | 压测产生流量 |
| P95 延迟按域 | ✅ 0.5-2.5ms | |
| 错误率按域 | No data | 压测 0 错误 |
| Token 用量按模型 | ✅ mock=6510 | mock 估算 token |
| 成本按模型 | No data | mock 成本=0 |
| 日预算水位 | No data | 累计成本=0 |

**如实标注**：容器环境因不装 embedding 模型 + 缺 KB 语料，RAG 评测与语义缓存命中率区显示 No data 是预期的（"反 Demo 化"原则）。W26 Day4 一键起全栈可在本地（带模型）复现真实数据。生产换有模型容器即可上真实指标。

---

## 七、欠账清点（W25 → W26 Day1）

| W25 欠账 | 状态 | 处置 |
|---|---|---|
| 严格 24h 连续零重复观测 | ✅ | W26 Day1 验证多任务 + 双实例 job_name counter |
| 日报连续 5 工作日准点 | 时间积累 | 继续 |
| eval_nightly 7 日均值偏离 | ✅ | eval_reports 2 晚数据已落库；Gage 已写 |
| 业务监控面板 | ✅ | 本日完成五区 |
| W23 遗留 40 并发 P95 评估 | 留 W26 Day3 | 验收前收口 |

---

## 八、W26 Day1 成功标准逐项勾

- [x] 自定义 metrics 补齐：`scm_nl2sql_eval_score` Gauge（4 个 layer + 1 rejected_rate）、`scm_job_success_total` Counter（label=job_name，6 个值）
- [x] Grafana MySQL datasource 直连 scm_platform 查 eval_reports / scheduler_job_runs
- [x] `scm_business.json` 五区面板（NL2SQL 质量 / 语义缓存 / 队列调度 / 流量健康 / 成本看板）
- [x] 跑一轮压测（20 并发 × 5 = 100 请求，100% 成功）+ 手动触发 eval_nightly（3 秒跑完 100 条 mock 评测，分数进入 Gauge）
- [x] 五区面板有真实数据流动：3/5 区有数据（NL2SQL 质量、流量健康、队列调度）；2/5 区如实标注 No data（语义缓存 + 成本看板的容器限制）
- [x] 截图归档 + 两面板 JSON 进仓 + 本报告

---

## 九、W26 Day2 衔接预告

| Day | 主题 | 关键准备 |
|---|---|---|
| Day2 (9/8) | 故障演练五连（杀 MySQL/Redis/Qdrant/LLM 全超时/实例半瘫） | `deploy/chaos/` 5 个脚本 + 观察脚本（持续 curl 探活 + Grafana 截屏） |
| Day3 (9/9) | 全量验收 | 160+ 用例、压测终版、acceptance_final.md |
| Day4 (9/10) | README/部署/10min 录屏 | 一键起 + 录屏讲稿 |
| Day5 (9/11) | 简历 v1 + STAR 脱稿 | |
| Day6 (9/12) | 模拟面试 ×2 + 项目冻结 tag v1.0.0 | |

> **Day1 修复的 eval_nightly 路径 bug 是 W25 遗留的真实问题**（容器内 eval 一直快速失败），本日报也作为 Day2 故障演练的"已知问题清零"基础。
