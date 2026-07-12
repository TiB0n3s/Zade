param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$Label = "manual",
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
}

$Headers = @{}
if ($Token) {
    $Headers["X-Zade-Token"] = $Token
}

$Body = @{ label = $Label }
$Result = Invoke-RestMethod -Uri "$BaseUrl/ops/backup" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 4) -ContentType "application/json" -TimeoutSec 60
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "backup-$Stamp.json"
$Result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $LogPath -Encoding UTF8

Write-Output "Backup: ok"
Write-Output "Path: $($Result.path)"
Write-Output "Log: $LogPath"
