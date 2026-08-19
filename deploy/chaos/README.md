# 故障演练脚本（W26 Day2）

> 依据：《W26学习执行手册》Day2 故障演练五连 +《03_核心技术方案》第 6 节降级链验收。
> 环境：Windows + Docker Desktop（脚本为 PowerShell/.py，可在 Git Bash 等价执行）。
> 原则：**演练顺序从轻到重（Qdrant → Redis → MySQL），出问题好定位**；"不雪崩"判定标准先写死：
> 探活 5xx < 5%、无级联超时、恢复 <2min 自动回归。演练结束后必须恢复服务并 reseed（演示数据一致性）。

## 五个故障注入脚本

| # | 脚本 | 故障 | 预期降级行为 |
|---|---|---|---|
| 1 | `kill_mysql.ps1` | `docker stop scm-mysql` | 审批暂停（明确提示）、chat 缓存路径可用、写操作全拒；恢复后 HITL 断点续跑 |
| 2 | `kill_redis.ps1` | `docker stop scm-redis` | fail-open 降 SQLite（幂等/缓存/锁走降级路径）；恢复后自动切回 |
| 3 | `kill_qdrant.ps1` | `docker stop w5-qdrant`（宿主 Qdrant 容器） | 检索降级 BM25-only（召回降级标记进响应/日志）；恢复后混合检索自动回 |
| 4 | `llm_timeout.ps1` | 改 provider 配置全指向失效 key + 短超时 | 模型池三级切换全失败 → mock 兜底话术（明确告知降级）；usage 记账不重复 |
| 5 | `kill_instance.ps1` | `docker stop scm-backend-a1` | least_conn 摘除、5xx=0、流量集中 a2；恢复后自动回来 |

## 辅助脚本

- `probe.ps1`：持续 curl 探活观察脚本（每 1s 打 /health + 业务端点，输出时间戳/状态码，超过"不雪崩"阈值标红）
- `grafana_snapshot.py`：Grafana 面板截图（五连记录证据，需 Grafana API 已启用匿名访问/使用 admin 凭据）
- `drill_report.ps1`：一键串跑全部五连（默认顺序 Qdrant→Redis→MySQL→LLM→实例），生成 `deploy/reports/chaos_drill_raw.log`

## 执行约定

```powershell
# 单连演练（示例：杀 Redis）
powershell -ExecutionPolicy Bypass -File deploy/chaos/kill_redis.ps1
# 演练期间持续探活（另开窗口）
powershell -ExecutionPolicy Bypass -File deploy/chaos/probe.ps1 -DurationSec 300
```

演练结束后：

```powershell
# 恢复所有被杀容器 + reseed（演示数据一致性）
docker start scm-mysql scm-redis w5-qdrant scm-backend-a1
cd F:\code\agent\learning-outputs\scm-copilot
make seed-biz        # scm_biz 固定 seed 幂等
make check-biz       # 校验和一致
```

> 注意：`kill_qdrant.ps1` 默认操作宿主 `w5-qdrant` 容器（QDRANT_URL 指向 localhost:6333）；
> 若使用其他 Qdrant 容器，用参数 `-Container <name>` 覆盖。
