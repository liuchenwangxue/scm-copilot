# 故障注入 #3：杀 Qdrant
# 预期：检索降级 BM25-only（召回降级标记进响应/日志）；恢复后混合检索自动回
# 注意：平台 QDRANT_URL 默认 http://localhost:6333 指向宿主 w5-qdrant（或 stage3-qdrant）。
#   先确认实际连接目标：docker ps --filter name=qdrant
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/kill_qdrant.ps1 [-Recover] [-Container w5-qdrant]
param(
    [switch]$Recover,      # 恢复模式
    [string]$Container = "w5-qdrant"
)

if ($Recover) {
    Write-Host "[chaos] 恢复 $Container ..." -ForegroundColor Green
    docker start $Container
    Start-Sleep -Seconds 6
    docker exec $Container sh -c "wget -q -O - http://127.0.0.1:6333/healthz || curl -s http://127.0.0.1:6333/healthz"
    exit 0
}

Write-Host "[chaos] 杀 $Container (docker stop) ..." -ForegroundColor Yellow
docker stop $Container
Write-Host "[chaos] 已停止。观察预期：" -ForegroundColor Yellow
Write-Host "  1. KB 域检索 → 向量路 Qdrant 超时 → 降级 BM25-only（source 标记进响应/日志）"
Write-Host "  2. 语义缓存 lookup → Redis 仍可用（缓存路径不依赖 Qdrant）"
Write-Host "  3. 恢复后混合检索（向量+BM25+RRF）自动回，无需重启 backend"
Write-Host "[chaos] 容器内 KB_DATA_DIR 无 chunks 时 KB 域可能因缺语料不可用（见 w26_day1 报告），" -ForegroundColor DarkYellow
Write-Host "[chaos] BM25-only 降级验证在本地 venv（带 chunks_title.json）执行：deploy/chaos/qdrant_deg_check.py" -ForegroundColor DarkYellow
