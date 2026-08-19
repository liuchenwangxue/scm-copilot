# W26 Day4 报告 · 文档与演示（一键起全栈 + 10 分钟 demo 讲稿）

> 阶段四 SCM Copilot 第 4 周 Day4 ｜ 2026-09-10（周四）
> 主题：README 定稿 / 架构文档 / 30 分钟部署手册 / 一键起验证 / demo 录屏讲稿
> 依据：《W26学习执行手册》Day4、《05_面试叙事与自测题》第 5 节
> **结果：三文档定稿 + 一键起 from-scratch 验证 14/14 + 修复 Makefile migrate 路径 bug + demo 讲稿出**

---

## 〇、Day4 速览

| # | 任务 | 状态 |
|---|---|---|
| 1 | `README.md` 定稿（定位/mermaid 架构图/快速开始/指标表/非目标） | ✅ |
| 2 | `docs/architecture.md`（三域+基座/数据流图/ADR 索引） | ✅ |
| 3 | `docs/deploy.md` 升级 30 分钟新成员部署手册（含冒烟清单） | ✅ |
| 4 | 一键起验证：down -v → up → migrate/seed/biz → smoke **14/14** | ✅（并修复 Makefile 路径 bug） |
| 5 | `reports/demo_10min.md` 录屏讲稿（五场景 + 追问预案） | ✅ |
| 6 | tag `v1.0.0` 项目冻结 | ✅ |

---

## 一、上午：三文档定稿

### 1.1 README.md（面向面试官/新成员的门面）

- **一句话定位**：一站式供应链智能助手——问知识（RAG）· 查数据（NL2SQL）· 办业务（工具+审批）
- **5 条核心亮点**全部带量化证据（0.970 / 20/20 / 100% / 30/30 / 344 passed / ≈¥20）
- **mermaid 架构图**：用户接入层 → nginx → 三域+基座 → 数据层/模型/观测
- **快速开始三命令**：`make tls && make up` → `make migrate && make seed && make init-biz-db && make migrate-biz && make seed-biz` → `make smoke`
- **指标表**：与 `acceptance_final.md` 18 项完全一致（未达项如实：coverage 56% / 40 并发 P95 / 夜间回归积累中）
- **非目标清单**：等保/OCR/IM/LoRA/BI 图表引擎等，主动讲边界

### 1.2 docs/architecture.md

- 三域 + 基座逐组件设计（kb/ops/data/platform/scheduler）
- **数据流图**（mermaid sequenceDiagram）：NL2SQL 主链路（生成→四道闸→只读沙箱→洞察溯源）+ 审批 HITL 链路 + 语义路由
- **ADR 索引**：8 条决策表 + 4 条修订记录
- 演进路径（stage3→平台→NL2SQL→调度+开放能力→二期 backlog）

### 1.3 docs/deploy.md（30 分钟新成员版）

- 30 分钟时间预算表（前置 5min / 起栈 10min / 初始化 8min / 冒烟 7min）
- 前置依赖清单 + 验证命令
- **冒烟验证清单**：`make smoke` 六域 14 项判定标准表
- HTTPS/监控/卸载重建/常见故障排查（10+ 条，含本次新坑）

---

## 二、上午：一键起全栈验证（from-scratch）

### 2.1 验证流程（真实可复现，全记录）

```bash
docker compose -f deploy/docker-compose.yml down -v      # ① 全清数据卷
docker compose -f deploy/docker-compose.yml up -d        # ② 全栈 10 容器 healthy
mingw32-make migrate                                     # ③ 平台库 5 版本链从空建表
mingw32-make seed                                        # ④ 12 用户/4 角色/13 权限
mingw32-make init-biz-db && mingw32-make migrate-biz && mingw32-make seed-biz   # ⑤ 业务库
mingw32-make check-biz                                   # ⑥ 校验和一致
mingw32-make smoke                                       # ⑦ 六域 14/14 PASS
```

### 2.2 ★ 发现并修复的真实问题：Makefile migrate 路径 bug

**症状**：`make migrate` 报 `.venv is not recognized as an internal or external command`。

**根因**：Makefile 中 `PY := .venv/Scripts/python.exe` 是相对项目根的路径；`migrate` 目标先 `cd backend` 再执行 `$(PY)`，此时相对路径随工作目录切换解析为 `backend/.venv/...`（不存在）→ Windows PowerShell + mingw32-make 下从零流程 `make migrate` **跑不通**。

**放大影响**：因 migrate 失败，`make seed` 的 `Base.metadata.create_all(checkfirst=True)` 兜底建表（**不含 alembic_version 表**）→ 手动 alembic 从空版本跑 → CREATE TABLE 冲突。**这就是"一键起"链条在真实新机器上会断的位置**。

**修复**：migrate/migrate-biz 改用 make 内置 `$(CURDIR)` 绝对路径调用 python（先试 `../$(PY)` 又被 make 当路径转换，改绝对路径最稳）：

```makefile
migrate:
	cd $(BACKEND) && $(CURDIR)/.venv/Scripts/python.exe -m alembic upgrade head
```

**修复后验证**：from-scratch 全流程 migrate 从空库成功建 5 个版本链 + seed 正常 + smoke 14/14。

### 2.3 smoke 冒烟结果（真实 HTTPS 平台）

```
1. 认证三态 + RBAC 抽样     [PASS]×4（200/401/401/403）
2. kb 域多轮问答+反馈       [PASS]×2（SSE done 两轮）
3. ops 查单+高危审批        [PASS]×2（approval_request）
4. data NL2SQL+攻击拦截     [PASS]×2（table+sql / 注入未穿透）
5. 调度六任务面板           [PASS]（jobs=6）
6. SDK 三接口               [PASS]×3（chat_stream/nl2sql/approvals）
===== 汇总：14/14 PASS =====
```

---

## 三、下午：demo 录屏讲稿（reports/demo_10min.md）

- **开场定位**（15s）→ **问知识**（2min，引用溯源+多轮）→ **查数据+攻击拦截**（3min，SQL 透出+四道闸讲解）→ **办业务**（2min，审批 before/after diff + HITL）→ **控制台调度**（1.5min，六任务+零重复）→ **Grafana**（1.5min，业务五区+基础监控）→ 收尾总结
- 每场景含：画面 / 操作 / 口播词 / **金句**（面试记忆点）
- 附：录前检查清单（reseed 演示数据/关通知/1080p）、4 条面试追问预案

> ⏺ **录屏本体**（`demo/demo_10min.mp4`）需 OBS 按讲稿录制（AI 结对无法生成带人声视频）。讲稿已覆盖五场景无卡壳脚本 + 操作步骤，空跑两遍后即可录制。

---

## 四、面试题 0.5h：demo 讲稿自听自查

- 按五场景把讲稿过一遍，找卡壳点：数据/安全/审批三处讲得最密，**单句 30 秒上限**约束已写入讲稿（金句化）
- 追问预案四连：为什么不用 Dify/Coze / 数据量涨 100 倍 / 语义缓存错答 / 四道闸绕过——答案锚定 ADR 决策记录，与《05》高频追问清单对齐

---

## 五、欠账清点（Day4 → Day5）

| 项 | 状态 | 处置 |
|---|---|---|
| README / architecture / deploy 三文档 | ✅ | 定稿 |
| 一键起全栈 from-scratch | ✅ | 14/14 + Makefile 修复 |
| demo_10min.md 讲稿 | ✅ | 录屏本体待 OBS 录制（讲稿已备） |
| tag v1.0.0 项目冻结 | ✅ | 本日打 tag |
| Day5：简历 v1 + STAR 三主线脱稿 | 明日 | 依据《05》第 4 节 + 简历素材库 |

---

## 六、Day4 成功标准逐项勾

- [x] 一键起全栈：down -v → up → migrate/seed/biz → smoke 14/14（修复 Makefile migrate 路径 bug）
- [x] README 定稿（定位/架构图/快速开始/指标表/非目标，指标与 acceptance_final 一致）
- [x] docs/architecture.md（三域+基座/数据流图/ADR 索引）
- [x] docs/deploy.md（30 分钟新成员部署手册 + 冒烟验证清单）
- [x] demo_10min.md 录屏讲稿（五场景无卡壳脚本 + 追问预案）
- [x] tag v1.0.0 项目冻结

> **Day4 完成：三文档定稿 + 从零到可用 14/14 + 修复真实一键起 bug + demo 讲稿就绪。** 明天 Day5 简历 v1 + 三主线脱稿。
