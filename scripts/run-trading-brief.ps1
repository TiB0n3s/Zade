param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$TargetDate = (Get-Date -Format "yyyy-MM-dd"),
    [string[]]$Symbols = @(),
    [string[]]$SnapshotTables = @(),
    [int]$LimitPerTable = 25,
    [int]$MaxRecommendations = 10,
    [string[]]$IncludeOpsChecks = @(),
    [string]$Token = $env:COFOUNDER_LOCAL_TOKEN,
    [switch]$NoStart,
    [switch]$NoEvidence,
    [switch]$NoJudgments,
    [switch]$NoScore,
    [switch]$ExportVault
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

. (Join-Path $PSScriptRoot "zade-token.ps1")
$Token = Resolve-ZadeToken -BaseUrl $BaseUrl -Token $Token
$Headers = @{}
if ($Token) {
    $Headers["X-Zade-Token"] = $Token
}

$Body = @{
    target_date = $TargetDate
    symbols = @($Symbols)
    snapshot_tables = @($SnapshotTables)
    limit_per_table = $LimitPerTable
    max_recommendations = $MaxRecommendations
    include_ops_checks = @($IncludeOpsChecks)
    store_evidence = -not $NoEvidence
    create_judgments = -not $NoJudgments
    score_outcomes = -not $NoScore
    export_vault = [bool]$ExportVault
}

$Result = Invoke-RestMethod `
    -Uri "$BaseUrl/trading-bot/daily-brief" `
    -Method Post `
    -Headers $Headers `
    -Body ($Body | ConvertTo-Json -Depth 8) `
    -ContentType "application/json" `
    -TimeoutSec 180

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "trading-brief-$TargetDate-$Stamp.json"
$Result | ConvertTo-Json -Depth 60 | Set-Content -LiteralPath $LogPath -Encoding UTF8

$Counts = $Result.brief.counts
Write-Output "Trading brief: ok"
Write-Output "Target date: $TargetDate"
Write-Output "Audit ID: $($Result.audit_id)"
Write-Output "Evidence ID: $($Result.evidence.evidence_id)"
Write-Output "Judgments: $($Result.judgments.Count)"
Write-Output "Score updates: $($Result.score_updates.Count)"
Write-Output "Missed calls: $($Result.missed_calls.Count)"
Write-Output "Counts: strong=$($Counts.strong) watch=$($Counts.watch) blocked=$($Counts.blocked) noise=$($Counts.noise)"
if ($Result.vault_export.path) {
    Write-Output "Vault export: $($Result.vault_export.path)"
}
Write-Output "Log: $LogPath"
