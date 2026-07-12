param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$Prompt = "State local co-founder readiness in one sentence.",
    [string[]]$Roles = @("general", "reasoning", "coding"),
    [int]$NumPredict = 160,
    [string]$Token = $env:COFOUNDER_LOCAL_TOKEN,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$LogDir = Join-Path $Root "run-logs"
$BaseUrl = $BaseUrl.TrimEnd("/")

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $NoStart) {
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
} else {
    Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 5 | Out-Null
}

$Headers = @{}
if ($Token) {
    $Headers["X-Zade-Token"] = $Token
}

$Body = @{
    prompt = $Prompt
    roles = $Roles
    num_predict = $NumPredict
}

$Result = Invoke-RestMethod -Uri "$BaseUrl/models/benchmark" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 240
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "model-benchmark-$Stamp.json"
$Result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $LogPath -Encoding UTF8

Write-Output "Model benchmark: $($Result.status)"
foreach ($Item in $Result.results) {
    Write-Output "$($Item.role): $($Item.status) $($Item.model) $($Item.latency_ms)ms"
}
Write-Output "Log: $LogPath"
