# SCM Copilot 部署手册

> 覆盖：本地一键起栈 / HTTPS（TLS）/ 监控 / SDK 发布 / 故障排查
> 更新：W25 Day6（三吸收项：Hooks + 基础监控 + TLS）

---

## 一、快速启动

```bash
cd F:\code\agent\learning-outputs\scm-copilot

# 1) 只起 MySQL + Redis（本地迁移/seed/单测用）
make up-mysql

# 2) 迁移 + 种子（平台库 + 业务库）
make migrate && make seed
make init-biz-db && make migrate-biz && make seed-biz

# 3) 生成本地 TLS 证书（★ W25 Day6：nginx 443 需要；无证书 nginx 起不来）
make tls

# 4) 起全栈（mysql/redis/mock-biz/backend-a1/a2/nginx）
make up

# 5)（可选）起监控栈（node-exporter/cadvisor/prometheus/grafana）
make monitor
```

访问：
- HTTP → https://localhost:18443（80 端口自动 301 跳转）
- Prometheus：http://localhost:19090/targets（看抓取状态）
- Grafana：http://localhost:13001（admin / admin123）

---

## 二、HTTPS（本地 TLS，W25 Day6）

### 为什么本地也上 HTTPS

- SSE（EventSource）只认 http(s)——浏览器对 `http://` 的流式有混用限制，未来接前端联调必须 https
- nginx 80 → 301 统一入口，避免"一个平台两个协议"的混合内容问题

### 证书：mkcert（本地根 CA）

```bash
make tls    # 内部执行：
#   mkcert -install                          # 本地根 CA 装进系统信任库（首次）
#   mkcert localhost scm.local               # 生成 CN 含两个 hostname 的证书
```

产物：`deploy/nginx/certs/`（`localhost+2.pem` + `localhost+2-key.pem`），
nginx 443 挂载该目录（compose `./nginx/certs:/etc/nginx/certs:ro`）。

**坑（手册 Day6）**：
1. 证书 CN 要含你实际访问的 hostname——`localhost + scm.local` 都生成（否则浏览器告警）
2. 证书文件缺失时 nginx 启动失败 → 先 `make tls` 再 `make up`

### 生产证书替换（重要）

**本证书只用于本地开发**。生产环境必须换正式证书（nginx 配置不用改，只换文件）：

```bash
# Let's Encrypt（推荐）：
certbot --nginx -d scm.example.com
# 或公司 CA：把正式证书/私钥放到 deploy/nginx/certs/，改名覆盖 localhost+2.pem/-key.pem
```

> 面试话术：TLS 配置与证书解耦——nginx.conf 只引用路径，换正式证书是"换文件"不是"改配置"。

---

## 三、监控（W25 Day6）

### 架构

```
                    ┌─────────────────────────────┐
   Prometheus 拉取  │  backend-a1/a2  /metrics    │  应用指标（QPS/P95/成功率）
   (15s)            │  node-exporter :9100         │  宿主机（Docker 宿主 VM）
                    │  cadvisor :8080              │  容器（backend/mysql/redis）
                    └─────────────────────────────┘
                                   │
                                   ▼
                              Grafana :13001（预置 Prometheus 数据源 + SCM 面板）
```

### 验证

1. `make up && make monitor`
2. Prometheus Targets：http://localhost:19090/targets → 4 个 job 全部 `UP`
3. Grafana：http://localhost:13001（admin/admin123）→ 左侧 Dashboards → `SCM Platform 核心指标`
   - 有数据 = 周 Gate "双监控面板有数据" ✓
4. 官方仪表盘导入（手册 Day6）：grafana.com 下载 Node Exporter Full（id=1860）/
   cAdvisor（id=14282）JSON → 放入 `deploy/grafana/dashboards/` → 30s 内自动加载

### 坑（手册 Day6）

- **cAdvisor 在 Windows Docker Desktop 下对 Windows 容器无效**，只监控 linux 容器
  （backend/mysql/redis 都是 linux，没问题）
- node-exporter 在 Windows 下监控的是 Docker 宿主 VM（Linux），不是 Windows 真机；
  容器资源占用看 cAdvisor 更直接

---

## 四、工具调用 Hooks（W25 Day6）

`backend/app/platform/hooks.py`：learn-claude-code s04 机制的实物落点。

- **PreToolUse**：参数校验（ToolSpec 契约 required）+ 审计埋点（before 状态）
- **PostToolUse**：结果审计（after + 耗时 + 熔断状态）+ 语义缓存失效（写类工具）
- ops 域 4 个工具（query_order/update_order/cancel_order/generate_report）全接入
  `execute_node`；`approval_gate` 复用 `make_after_state` 的 before/after diff

验证：审计文件（`data/audit.log`）出现 `tool_pre_use` / `tool_post_use` 事件；
`make test-hooks`（17 用例）。

---

## 五、SDK 发布（W25 Day5）

```bash
cd sdk
python -m build
twine upload --repository testpypi dist/*          # TestPyPI（备选名 scm-copilot-client-dev）
pip install --index-url https://test.pypi.org/simple scm-copilot-client
```

十行接入示例见 `reports/w25_day5_sdk.md` §2.1。

---

## 六、常见故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| nginx 起不来 | TLS 证书缺失 | `make tls` 后 `make up` |
| Grafana 面板无数据 | Prometheus targets 未 UP | 检查 `http://localhost:19090/targets` |
| 双实例 job_runs instance 全是 local | 缺 `SCM_INSTANCE_ID` | compose 已配 a1/a2；本地 `export SCM_INSTANCE_ID=local-dev` |
| daily_brief 归属日少一天 | 容器缺 TZ | backend 已配 `TZ: Asia/Shanghai` |
| 429 用例 fail-open | Redis 端口连错 | 默认 16381（本机 16380 被 stage3 占用） |
| /metrics 空 | METRICS_ENABLED=0 | 默认 1；容器内关（写盘慢）保留关闭 |
