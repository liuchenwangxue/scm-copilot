# 故障注入 #5：实例半瘫（杀 backend-a1）
# 预期：least_conn 摘除、5xx=0、流量集中 a2（Grafana 曲线可见）；恢复后自动回来
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/kill_instance.ps1 [-Recover] [-Container scm-backend-a1]
param(
    [switch]$Recover,      # 恢复模式
    [string]$Container = "scm-backend-a1"
)

if ($Recover) {
    Write-Host "[chaos] 恢复 $Container ..." -ForegroundColor Green
    docker start $Container
    Start-Sleep -Seconds 12   # 等应用启动 + healthcheck 通过
    docker ps --filter name=scm-backend-a1 --format "{{.Names}} {{.Status}}"
    exit 0
}

Write-Host "[chaos] 杀 $Container (docker stop) ..." -ForegroundColor Yellow
docker stop $Container
Write-Host "[chaos] 已停止。观察预期：" -ForegroundColor Yellow
Write-Host "  1. 压测中段 stop → nginx least_conn 自动摘除（upstream 只剩 a2）"
Write-Host "  2. 探活 5xx=0（proxy_next_upstream 切换 + healthcheck 摘除）"
Write-Host "  3. Grafana 双实例 QPS 曲线：a1 归零、a2 翻倍"
Write-Host "  4. 恢复 docker start 后自动回归，流量重新均分"
Write-Host "[chaos] 组合演练：deploy/load_test.py --kill-instance a1 可在压测中段自动杀+恢复" -ForegroundColor DarkYellow
