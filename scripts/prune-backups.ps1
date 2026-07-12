param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [int]$KeepLast = 10,
    [string]$Token = $env:COFOUNDER_LOCAL_TOKEN,
    [switch]$Commit,
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
    keep_last = $KeepLast
    dry_run = -not $Commit
}

$Result = Invoke-RestMethod -Uri "$BaseUrl/ops/backups/prune" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 60
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "backup-retention-$Stamp.json"
$Result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $LogPath -Encoding UTF8

if ($Result.dry_run) {
    Write-Output "Backup retention: dry run"
} else {
    Write-Output "Backup retention: committed"
}
Write-Output "Keep last: $($Result.keep_last)"
Write-Output "Delete candidates: $($Result.deleted_count)"
Write-Output "Log: $LogPath"
