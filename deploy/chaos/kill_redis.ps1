# 故障注入 #2：杀 Redis
# 预期：fail-open 降 SQLite/内存（幂等/缓存/锁全走降级路径，响应变慢但可用）；恢复后自动切回（无缝）
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/kill_redis.ps1 [-Recover]
param(
    [switch]$Recover,      # 恢复模式
    [string]$Container = "scm-redis"
)

if ($Recover) {
    Write-Host "[chaos] 恢复 $Container ..." -ForegroundColor Green
    docker start $Container
    Start-Sleep -Seconds 4
    docker exec $Container redis-cli ping
    exit 0
}

Write-Host "[chaos] 杀 $Container (docker stop) ..." -ForegroundColor Yellow
docker stop $Container
Write-Host "[chaos] 已停止。观察预期：" -ForegroundColor Yellow
Write-Host "  1. GET /health 仍 200（db=up；Redis 非 health 判定项）"
Write-Host "  2. 幂等 claim/complete → fail-open 降 sqlite（/data/idempotency.db，幂等语义仍成立）"
Write-Host "  3. 查询缓存 → 降级内存 dict（响应可用，TTL 内仍命中）"
Write-Host "  4. 分布式锁 / API Key 令牌桶 → 放行打 WARNING（配额是软约束）"
Write-Host "  5. 调度 leader 锁 → fail-open 放行（任务幂等兜底，零重复语义不破）"
Write-Host "[chaos] 恢复后自动切回 Redis，无需重启 backend（redis_client 懒连接 + 冷却探测）" -ForegroundColor DarkYellow
