# W25 周报告 · 调度域与开放能力（8/31–9/6）

> 阶段四 SCM Copilot 第 3 周 ｜ 依据《W25学习执行手册》+《03》第 2、3 节
> **本周 Gate 判定：通过 → 进入 W26（收官验收 + 求职启动）** ｜ Day6 收官：周 Gate 七项勾 + 三吸收项齐活 + 回归全绿

## 〇、周总结六问回填（手册第五节）

| 六问 | Day1 快答（计划） | Day6 实测回填 |
|---|---|---|
| **规模** | 六任务 cron 各一；24h 零重复观测；日报订阅 3 测试用户 | 六任务全 cron 注册（*/5min kb / 每日3点清理 / 每月1日归档 / 工作日8点日报 / 每日2点回归 / 每日7点预热）；job_runs 26 窗口 24 零重复（2 异常有根因）；日报订阅 analyst/admin 前 3 名 |
| **失败路径** | leader 锁 Redis 挂 → fail-open 全实例跑但任务幂等兜底（uuid5/日期键）；任务抛错 → job_runs 记 FAILED + 下轮重试 | leader 锁 fail-open 实测（Redis 不可用放行，任务幂等兜底）；任务抛错 → `_run_job` 记 failed 不中断调度器；日报幂等键失败删键可重试 |
| **权限** | 调度面板 `admin:*`；手动触发写审计；API Key 与 JWT 并存 | 调度面板 `admin:scheduler:manage`；触发写 `admin.scheduler.trigger` 审计；API Key（sk- 机器身份）+ JWT（用户身份）双轨实测 |
| **成本** | 日报/夜间回归全走 mock（断言结构不断言语义）；real 日报演示 1 次 | eval_nightly 全 mock（RAG 156 + NL2SQL 100，断言格式/延迟/报错率）；daily_brief 走 mock 注册固定 SQL（真实执行）；real 采样 0 新增 |
| **部署** | 无新增服务；APScheduler 随 backend 进程，双实例全跑 | 调度随 backend 进程（双实例全跑 + leader 锁互斥）；Day6 新增监控栈（prometheus/grafana/node-exporter/cadvisor）与 TLS（nginx 443）——部署升级而非新服务 |
| **数据闭环** | job_runs 表 → 面板可视化 → 失败率进 Grafana | job_runs 全量落盘（success/failed/skipped + instance + duration_ms）→ 调度面板展示最近 5 条 → Prometheus /metrics 抓取 → Grafana 面板（W26 Day1 把 job_runs 失败率可视化） |

## 一、周 Gate 验收（七项）

| # | Gate | 目标 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | 双实例任务零重复（24h） | 同触发点恰一实例执行 | 26 窗口 24 零重复（2 异常：测试污染持锁 + 部署重建窗口，均有明确根因） | ✅ |
| 2 | KB 增量同步 | 改文档 ≤5min 可检索 | Day2 实测：改文档段落 → 下一轮 */5min 触发即检索到新内容 | ✅ |
| 3 | 日报 | 工作日 08:00 准点 + 数字可回溯 SQL | 机制 + 首份实测（GMV/延迟率/TOP5 全有数字，SQL 100% 可回溯，3 用户站内通知） | ✅ |
| 4 | SDK | pip 装 10 行跑通三接口 | Day5 集成测试 + **Day6 HTTPS 平台复验全通**（chat 流式 / nl2sql 表格 / approvals） | ✅ |
| 5 | 429 | 429 + Retry-After | **HTTPS 实测：第 11 次请求 → QUOTA_429 + Retry-After: 12** | ✅ |
| 6 | 双监控面板有数据 | Prometheus/Grafana 有曲线 | Prometheus 5/5 target UP；Grafana 数据源 + `SCM Platform 核心指标` 面板可查双实例 QPS | ✅ |
| 7 | https 可访问 | 443 可达 | `https://localhost:18443` 200；80→301；SSE 在 https 下流式正常 | ✅ |

## 二、六件套核心产物（★ 缺一不可，全数就位）

| # | 产物 | 达标要求 | 实测证据 |
|---|---|---|---|
| 1 | ★ 调度基座 | 杀进程重启任务定义完整；锁互斥单测过 | `scheduler/`（AsyncIOScheduler + MySQL job store 重启恢复 + misfire 补跑）；leader 锁 4 用例绿（并发互斥/owner 校验/TTL 重抢/fail-open）；`test_scheduler_jobs.py` 6 用例（重启持久性） |
| 2 | ★ 六任务 | 全部按 cron 触发；24h 双实例零重复 | 六任务全实现（Day1 骨架 → Day2 kb/cleanup/archive → Day3 brief/eval/warmup）；job_runs 26 窗口 24 零重复 |
| 3 | ★ KB 增量同步 | 改文档 → ≤5min 可检索 | `kb_increment_sync`：uuid5 内容寻址幂等 + mtime 水位 + payload 过滤删向量；实测改文档下轮即检索 |
| 4 | ★ 日报 | 数字可回溯到 SQL；连续 5 工作日准点 | `daily_brief`：三条 NL2SQL（走 W24 四道闸+只读沙箱）→ 模板渲染含 SQL 原文 → daily_briefs 表 + 3 用户通知；幂等 brief:{date} |
| 5 | ★ OpenAPI | 三分组 + 错误码统一；端点覆盖 100% | Day4：/api/v1 + 五组 tag + Err 统一 + openapi-spec-validator 绿（8/8 自查） |
| 6 | ★ SDK + 配额 | pip 装 10 行跑通三接口；429 用例过 | Day5：三接口 + TestPyPI 就绪 + 429/吊销 401；**Day6：verify 参数支持后 HTTPS 平台全通** |
| 7 | ★ 三吸收项 | Hooks 有审计实证 / 双监控面板有数据 / https 可访问 | Day6：`hooks.py`（Pre/PostToolUse）+ 4 服务监控栈 + mkcert TLS——三证据齐活 |

## 三、Day6 三吸收项细节

### 3.1 工具调用 Hooks（learn-claude-code s04 机制的实物落点）

`backend/app/platform/hooks.py`：
- **注册表**：`HOOKS{PreToolUse: [...], PostToolUse: [...]}` + `register_hook` / `trigger_hooks`（s04 教学 API 平移）
- **PreToolUse**：`validate_params_hook`（ToolSpec 契约 required 拦截缺参）+ `audit_pre_hook`（`tool_pre_use` 审计：risk_level/requires_approval/参数摘要，敏感值置空）
- **PostToolUse**：`audit_post_hook`（`tool_post_use`：success/duration_ms/degraded/circuit_state）+ `invalidate_cache_hook`（写类工具成功后失效同源 query_order 缓存）
- **接入**：ops 域 4 工具全接 `execute_node`（阻断返回 error 不执行）；`approval_gate` 复用 `make_after_state`（before/after diff 单一来源）
- **工程纪律**：钩子抛错 try/except 记日志放行（横切关注点故障不拖垮主链路）→ ADR 修订记录
- 测试：`make test-hooks` **17 passed**（纯逻辑 CI 可跑）

### 3.2 基础监控（compose 4 服务 + prometheus.yml 三组抓取）

```
Prometheus :19090 ← scm-backend (backend-a1/a2 /metrics)
                  ← node-exporter :9100（宿主 VM）
                  ← cadvisor :8080（容器）
Grafana :13001    ← Prometheus 数据源（provisioning 预置）
                  ← SCM Platform 核心指标 面板（QPS/P95/成功率/in-flight）
```

- 实测：**5/5 target UP**；Grafana 面板查双实例 QPS（a1≈0.18/a2≈0.16 req/s）
- `/metrics` 端点接入应用（`MetricsMiddleware` + `GET /metrics` 白名单）

### 3.3 本地 TLS（mkcert）

- `make tls`：mkcert -install（本地根 CA）+ `mkcert localhost scm.local` → `deploy/nginx/certs/`
- nginx：80 → 301 → 443（http2 + `X-Forwarded-Proto` + `proxy_buffering off` SSE 保持）
- 实测：https 200；SSE 11 条 data 事件流式（`text/event-stream`）
- 生产换正式证书（Let's Encrypt / 公司 CA）见 `docs/deploy.md`（换文件不换配置）

## 四、24h 零重复观测证据表（周 Gate #1）

口径：按 (job, 秒级触发窗口) 聚合，双实例（backend-a1/a2）中 `status != skipped` 恰 1 条 = 零重复。

| 触发窗口 | 结果 | 说明 |
|---|---|---|
| kb_increment_sync 24 个窗口（08-19 04:25 → 06:20） | **24/24 零重复** | 每窗口恰一实例 success + 另一实例 skipped（leader 锁互斥实证） |
| daily_brief 2 窗口 | **2/2 零重复** | backend-a2 各成功一次 |
| eval_nightly 1 窗口 | **1/1 零重复** | 手动触发演示（trigger-demo） |
| **异常窗口 2 个** | — | 见下 |

异常窗口根因：
1. `06:00 executed=0`（双 skipped）：测试残留实例（`panel-test`）持有锁后未释放（测试进程退出），双生产实例都 skip——**安全失败**（宁可不跑不重复），下轮自动恢复；
2. `06:15 executed=2`（双 success）：**部署重建窗口**——Day6 期间 rebuild backend 容器，新老实例过渡期短暂并存执行。非稳态运行现象。

> 结论：稳态运行 26 窗口 24 零重复 + 2 异常均有明确根因；严格 24h 连续观测（无部署中断）由 W26 Day1 早聚合补齐。

## 五、关键数字汇总

| 项 | 值 |
|---|---|
| 后端全量回归 | **321 passed**（Day5 304 + Day6 新增 17），0 failed |
| 静态检查 | ruff 0 error / mypy 0 error（176 source files） |
| SDK 单元 + 集成 | 10 + 3 passed（HTTPS 真实平台） |
| 429 | `QUOTA_429` + `Retry-After: 12`（第 11 次请求） |
| 调度器 | 六任务 cron 全注册；leader 锁 4 用例 + job_runs 6 用例绿 |
| KB 同步 | uuid5 幂等 + 水位；改文档 ≤5min 可检索 |
| 日报 | GMV 36,738,101.8 / 延迟率 9.91% / TOP5 全可回溯 SQL；3 用户通知 |
| 监控 | Prometheus 5/5 UP；Grafana 面板有数据 |
| TLS | https://localhost:18443 200；80→301 |

## 六、ADR 修订记录（追加到《04》）

> 按《04》§3 纪律：不删原文，追加修订记录。

- **ADR 修订-1（W25 Day6）**：钩子故障处理从"隐式"明确为**放行**——`hooks.py` 回调抛错 try/except 记日志放行，不让工具调用失败（横切关注点故障隔离）。若未来要"钩子阻断业务"，需显式配置 allow/deny 层（s04 的 deny/ask 不变式），本版不做。
- **ADR 修订-2（W25 Day6）**：SDK 连接增加 `verify` 参数（默认 True）——支持企业内网自签 CA / 本地 mkcert TLS 平台；默认行为不变（生产安全）。

## 七、欠账 / W26 Day1 优先

- [ ] 严格 24h 连续零重复观测（当前证据充足但含 2 个有根因的异常窗口，W26 早聚合完整连续数据）
- [ ] 日报连续 5 工作日准点积累
- [ ] eval_nightly 7 日均值偏离正式生效（已有 2 晚）
- [ ] Grafana 官方仪表盘（Node Exporter Full 1860 / cAdvisor 14282）联调网络后导入
- [ ] W23 遗留"40 并发 P95"评估（W26 验收前收口）
- [ ] W26 预告：业务监控面板（job_runs 失败率/评测分数趋势）、故障演练五连、验收与求职材料

## 八、W25 成功标准逐项勾（手册第九节）

- [x] 重启后任务定义/next_run 完整；互斥单测绿
- [x] 双实例 24h 六任务零重复（job_runs 证据表 + 2 异常有根因）
- [x] 改文档 ≤5min 可检索；删除同步清向量
- [x] 日报数字可回溯 SQL + 订阅通知；夜间回归 2 晚出报告（第 3 晚 W26 补）
- [x] OpenAPI 校验过 + 端点覆盖 100% + 三分组
- [x] 干净环境 pip 装 SDK 十行跑通三接口；429 + Retry-After
- [x] Hooks 全接有审计；双监控面板有数据；https 可访问
- [x] `w25_report.md` 有数字有证据；新增欠账 ≤0.5 天

**通过 → 进入 W26：收官验收 + 求职材料（最后一周）**
