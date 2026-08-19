# SCM Copilot 部署手册（30 分钟新成员版）

> 定稿：W26 Day4（2026-09-10）｜ 目标：**新成员 30 分钟从裸机到可用**
> 覆盖：前置依赖 → 一键起栈 → 初始化 → 冒烟验证清单 → HTTPS/监控 → 故障排查
> 配套：README 快速开始 · architecture 架构文档

---

## 〇、30 分钟时间预算

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 前置依赖检查 | 5min | Docker Desktop / Python 3.12 / git / mkcert |
| 起全家桶 | 10min | `make tls && make up`（首次拉镜像 + 构建） |
| 初始化 | 8min | migrate / seed / init-biz-db / seed-biz |
| 冒烟验证 | 7min | `make smoke` 六域 14 项全过 |

> 已有镜像/数据卷时整体可压缩到 **15 分钟**。

---

## 一、前置依赖

| 依赖 | 版本要求 | 验证命令 |
|---|---|---|
| Docker Desktop（WSL2 backend） | ≥ 24 | `docker version`（Client+Server 都在） |
| Python | 3.12 | `python --version` |
| git | 任意 | `git --version` |
| mkcert | 任意（本地 TLS 用） | `mkcert -version` |

> Windows 注意：Docker 必须跑 **Linux 容器**；本机 3306/8000/16380/3001 等端口可能被其他项目占用，本项目全部走自定义端口（见 §三）。

## 二、一键起栈

```bash
# 0) 取代码 + 进目录
git clone <repo-url> scm-copilot
cd scm-copilot

# 1) 虚拟环境 + 依赖
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
pip install -e ./sdk

# 2) 本地 TLS 证书（nginx 443 必需；无证书 nginx 起不来）
make tls        # mkcert -install + 生成 deploy/nginx/certs/

# 3) 起全家桶（10 容器：mysql/redis/mock-biz/backend-a1/a2/nginx + 监控 4）
make up         # docker compose -f deploy/docker-compose.yml up -d --build

# 4) 确认全部 healthy/running
docker compose -f deploy/docker-compose.yml ps
```

**启动失败排查**：nginx 起不来 → 90% 是 TLS 证书缺失 → `make tls` 后 `docker compose up -d nginx`。

## 三、初始化（幂等，可重跑）

```bash
# 平台库：12 表迁移 + 种子（4 角色 / 13 权限 / 3 租户 × 4 角色测试用户）
make migrate && make seed

# 业务库：六表迁移 + 万级固定 seed（suppliers 40 / orders 10000 / order_items ~35000）
make init-biz-db && make migrate-biz && make seed-biz
make check-biz      # 行数 + 校验和（重放一致性）

# （可选）监控栈：prometheus/grafana/node-exporter/cadvisor
make monitor
```

> 所有 make 目标幂等：重复执行结果一致（seed 连跑两遍校验和不变）。

### 访问入口

| 入口 | 地址 | 凭证 |
|---|---|---|
| Swagger / 平台 API | https://localhost:18443/docs | 测试账号见下 |
| Prometheus | http://localhost:19090/targets | — |
| Grafana | http://localhost:13001 | `admin` / `admin123` |
| nginx HTTP（301→HTTPS） | http://localhost:18000 | — |

测试账号（密码统一 `Passw0rd!`）：

```text
admin_t_huadong    operator_t_huabei    analyst_t_huanan    viewer_t_huadong
```

## 四、冒烟验证清单（`make smoke`）

`make smoke` 执行 `deploy/verify_e2e_day3.py`，六域 14 项端到端冒烟（真实 HTTPS 平台 + nginx LB），**全部 PASS 才算部署成功**：

| # | 域 | 冒烟项 | 判定标准 |
|---|---|---|---|
| 1 | 认证 | 正确登录 200 | status=200 |
| 2 | 认证 | 错误密码 401 | status=401 |
| 3 | 认证 | 无 token 401 | status=401 |
| 4 | 认证 | viewer 调 data 端点 403（RBAC） | status=403 |
| 5 | kb | 多轮问答（首轮 SSE done） | `"type": "done"` |
| 6 | kb | 同会话第二轮 SSE done | `"type": "done"` |
| 7 | ops | 查单 SSE done | `"type": "done"` |
| 8 | ops | 高危改单 → approval_request（HITL） | `"type": "approval_request"` |
| 9 | data | NL2SQL 表格 + SQL 透出 | table 且 sql 非空 |
| 10 | data | 攻击 SQL（堆叠注入）未穿透 | 返回 SQL 不含 DROP/堆叠 |
| 11 | 调度 | 六任务面板可查 | jobs ≥ 6 |
| 12 | SDK | chat_stream SSE done | events 含 done |
| 13 | SDK | nl2sql 表格 + SQL | table 且 sql |
| 14 | SDK | approvals.list_pending | list 类型 |

```bash
make smoke   # 期望：===== 汇总：14/14 PASS =====（退出码 0）
```

### 深度验证（可选，超出 30 分钟预算）

| 命令 | 内容 |
|---|---|
| `make test` | pytest 全量 344 passed + coverage |
| `make check` | ruff + mypy 0 error |
| `make loadtest` | 30 并发压测（正式 Gate，P95 ≈ 1.27s） |
| `make chaos-probe` | 故障演练探活（杀容器前先起观察） |
| `make drill` | 压测中段杀实例演练（5xx=0） |

## 五、HTTPS（本地 TLS）

- **为什么本地也上 HTTPS**：SSE（EventSource）只认 http(s)；nginx 80 → 301 统一入口，避免"一个平台两个协议"
- `make tls` 用 mkcert 生成 `localhost + scm.local` 证书到 `deploy/nginx/certs/`，nginx 443 挂载
- **生产替换**：证书与配置解耦，只换文件不换配置（Let's Encrypt：`certbot --nginx -d scm.example.com`，或公司 CA 证书覆盖同名文件）

## 六、监控（W25 Day6 三吸收项之一）

```
Prometheus (拉取 15s)
  ├── backend-a1/a2 /metrics      应用指标：QPS/P95/成功率 + 9 个业务指标
  ├── node-exporter :9100         宿主机（Docker 宿主 VM）
  └── cadvisor :8080              容器指标
          └──▶ Grafana :13001  →  SCM 业务五区面板（NL2SQL 质量/语义缓存/队列调度/流量健康/成本看板）
```

验证：`http://localhost:19090/targets` 4 个 job 全部 `UP` → Grafana → Dashboards → `SCM Business` / `SCM Platform 核心指标`。

> Windows 坑：node-exporter 监控的是 Docker 宿主 VM（Linux）而非 Windows 真机；cAdvisor 对 Windows 容器无效（本项目全部是 linux 容器，不受影响）。

## 七、卸载 / 重建（从零验证）

```bash
docker compose -f deploy/docker-compose.yml down -v   # 清空数据卷（-v = 连数据一起删）
# 然后重新执行 §二 §三 §四 → 从零到可用的完整闭环
```

> W26 Day4 一键起验证记录见 [reports/w26_day4_doc.md](../reports/w26_day4_doc.md)。

## 八、常见故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| nginx 起不来 | TLS 证书缺失 | `make tls` 后 `docker compose up -d nginx` |
| backend 反复重启 | MySQL 未 healthy（backend depends_on healthy） | `docker compose ps` 看 mysql；首次拉镜像慢等 start_period 30s |
| 登录 503 | MySQL 挂了（认证 fail-open 设计：已有 token 可用，login 拒绝） | `make up-mysql` 恢复 |
| Grafana 面板无数据 | Prometheus targets 未 UP | 检查 `http://localhost:19090/targets` |
| data 查询报 Connection refused | 容器内 `SCM_BIZ_RO_DSN` 指向 127.0.0.1（应为 mysql 服务名） | 检查 compose 环境变量 `SCM_BIZ_RO_DSN` |
| daily_brief 归属日少一天 | 容器缺 TZ | backend 已配 `TZ: Asia/Shanghai`，勿删 |
| 双实例 job_runs 全是 local | 缺 `SCM_INSTANCE_ID` | compose 已配 a1/a2；本地 `set SCM_INSTANCE_ID=local-dev` |
| SDK 集成 429 未出现 | 无 Redis（配额 fail-open） | 本地部署环境必须起 Redis（`make up-mysql`） |
| 压测 P95 长尾 | 容器内 STRUCT_LOG_ENABLED=1 写盘慢 | compose 已默认关（`STRUCT_LOG_ENABLED: "0"`） |
| `pip install -e ".[dev]"` 失败 | Windows 缺 build 工具（bcrypt 编译） | 升级 pip + setuptools 后重试；或用预编译 wheel |

---

> 部署完成，下一步：看 [reports/demo_10min.md](../reports/demo_10min.md) 跟随 10 分钟 demo 走一遍五场景。
