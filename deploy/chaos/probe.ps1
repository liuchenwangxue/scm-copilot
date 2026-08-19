# 探活观察脚本：演练期间持续打健康与业务端点，判定"不雪崩"
# 判定标准（手册坑）：探活 5xx < 5%、无级联超时、恢复 <2min 自动回归
# 用法：powershell -ExecutionPolicy Bypass -File deploy/chaos/probe.ps1 -DurationSec 120 [-IntervalSec 2]
param(
    [int]$DurationSec = 120,        # 观察时长（秒）
    [int]$IntervalSec = 2,          # 探活间隔（秒）
    [string]$Base = "https://localhost:18443",
    [string]$Out = "deploy/reports/chaos_probe_raw.log"
)

$ErrorActionPreference = "Continue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 简化 TLS 校验（mkcert 本地证书不在系统信任库；探活工具面向本地演练）
Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAll : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert,
        WebRequest req, int problem) { return true; }
}
"@
[Net.ServicePointManager]::CertificatePolicy = New-Object TrustAll

$logDir = Split-Path -Parent $Out
if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Probe($name, $url, $method = "GET", $body = $null) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Method = $method
        $req.Timeout = 8000
        if ($body) {
            $req.ContentType = "application/json"
            $bytes = [Text.Encoding]::UTF8.GetBytes($body)
            $req.ContentLength = $bytes.Length
            $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
        }
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($resp) { return [int]$resp.StatusCode }
        return 599   # 无响应（超时/连接失败）
    } catch {
        return 599
    }
}

# 登录拿 token（打 ops/kb 业务端点需要）
function Login {
    $body = '{"username":"admin_t_huadong","password":"Passw0rd!"}'
    try {
        $r = Invoke-RestMethod -Uri "$Base/api/v1/auth/login" -Method Post -Body $body `
            -ContentType "application/json" -SkipCertificateCheck -TimeoutSec 8
        return $r.access_token
    } catch { return $null }
}

$start = Get-Date
$end = $start.AddSeconds($DurationSec)
$rows = @()
$total = 0; $bad = 0

Write-Host "[probe] 开始 $DurationSec s 探活 @ $Base (每 ${IntervalSec}s)" -ForegroundColor Cyan
while ((Get-Date) -lt $end) {
    $ts = (Get-Date).ToString("HH:mm:ss")
    $c1 = Probe "health" "$Base/health"
    $token = Login
    $c2 = 0
    if ($token) {
        $hbody = '{"message":"你好","session_id":"chaos-probe-' + (Get-Random) + '"}'
        $c2 = Probe "kb_chat" "$Base/api/v1/kb/chat" "POST" $hbody
    }
    $total += 2
    if ($c1 -ge 500 -or $c2 -ge 500) { $bad++ }
    $line = "$ts health=$c1 kb_chat=$c2 login=$([bool]$token)"
    $rows += $line
    $flag = if ($c1 -ge 500 -or $c2 -ge 500) { "  <-- FAIL" } else { "" }
    Write-Host ("  " + $line + $flag)
    Start-Sleep -Seconds $IntervalSec
}

$rate = if ($total) { [math]::Round($bad / $total * 100, 2) } else { 0 }
Write-Host ""
Write-Host "[probe] 汇总：总探测 $total 次，5xx/无响应 $bad 次，5xx 占比 $rate%（判定线 <5%）" -ForegroundColor Cyan
if ($rate -lt 5) {
    Write-Host "[probe] 判定：不雪崩 ✓（5xx < 5%）" -ForegroundColor Green
} else {
    Write-Host "[probe] 判定：雪崩风险 ✗（5xx >= 5%），检查级联超时与降级链" -ForegroundColor Red
}
$rows | Set-Content -Path $Out -Encoding UTF8
Write-Host "[probe] 原始记录已写入 $Out" -ForegroundColor Cyan
