# 故障注入 #4：LLM 全超时
# 预期：模型池三级切换全失败 → mock 兜底话术（明确告知降级）；usage 记账不重复
# 做法：把 LLM 配置改为"全指向失效 key + 短超时"，使模型池每个模型都连接失败/401，
#   观察 real provider 降级链（LLM_DEGRADE_TO_MOCK=1 默认）返回 [WARNING] mock 兜底。
# 注意：容器内 LLM_PROVIDER=mock（零成本），real 降级链用本地 venv 验证最直接。
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/llm_timeout.ps1 [-Recover]
param(
    [switch]$Recover
)

# 失效 key / 短超时配置（演练专用；不要与真实 Key 混用）
$env:LLM_PROVIDER = "real"
$env:LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:LLM_API_KEY  = "sk-invalid-key-for-chaos-drill-0000"
$env:LLM_TIMEOUT  = "2"                 # 2s 短超时：快速触发超时
$env:LLM_MODEL_POOL = "glm-5.2,deepseek-chat,invalid-model-x"   # 三级切换，全指向失效/慢模型
$env:LLM_DEGRADE_TO_MOCK = "1"          # 降级链开关（默认即开）

if ($Recover) {
    Write-Host "[chaos] 恢复：还原 LLM 配置为 mock（清空演练专用环境变量，进程退出即自动还原）" -ForegroundColor Green
    exit 0
}

Write-Host "[chaos] LLM 全超时演练（本地 venv）..." -ForegroundColor Yellow
Write-Host "  1. real provider 模型池 [glm→deepseek→invalid] 逐个连接失败/超时"
Write-Host "  2. 池内全失败 → 降级链返回 [WARNING] 前缀 mock 兜底（明确告知降级）"
Write-Host "  3. usage 记账不重复：失败模型不累计 usage，只有兜底成功才记一次"

# 直接跑一个小验证：三接口中 generate_json 的降级链（mock 兜底必须返回 dict 结构）
Push-Location "F:\code\agent\learning-outputs\scm-copilot"
try {
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/llm_deg_check.py
} finally {
    Pop-Location
}

Write-Host "[chaos] 完成。注意：若在容器内演练，需在 compose 中改 env 并重建镜像，" -ForegroundColor DarkYellow
Write-Host "[chaos] 演练后必须还原 LLM_PROVIDER=mock（别带着故障配置跑 Day3 验收）" -ForegroundColor DarkYellow
