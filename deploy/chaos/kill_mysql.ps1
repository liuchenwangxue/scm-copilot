# 故障注入 #1：杀 MySQL
# 预期：审批暂停（明确提示）、chat 缓存路径可用、写操作全拒不雪崩；恢复后 HITL 断点续跑
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/kill_mysql.ps1 [-Recover]
param(
    [switch]$Recover,      # 恢复模式：docker start 被杀的容器
    [string]$Container = "scm-mysql"
)

if ($Recover) {
    Write-Host "[chaos] 恢复 $Container ..." -ForegroundColor Green
    docker start $Container
    Write-Host "[chaos] 等待 MySQL 健康 ..." -ForegroundColor Green
    Start-Sleep -Seconds 8
    docker exec $Container mysqladmin ping -uroot -proot123
    exit 0
}

Write-Host "[chaos] 杀 $Container (docker stop) ..." -ForegroundColor Yellow
docker stop $Container
Write-Host "[chaos] 已停止。观察预期：" -ForegroundColor Yellow
Write-Host "  1. GET /health → db=down / status=degraded（backend 自身存活）"
Write-Host "  2. POST /api/v1/ops/chat 高危改单 → 审批创建失败 → SSE error 明确提示（不雪崩）"
Write-Host "  3. 登录（依赖 MySQL）→ 502/503 但 nginx 不级联超时"
Write-Host "  4. 恢复后 HITL 断点可 resume（approvals 表数据在 MySQL，恢复后读回）"
Write-Host "[chaos] 注意：MySQL 停后 login 不可用，探活主要打 /health 与 kb/chat（缓存/路由路径）" -ForegroundColor DarkYellow
