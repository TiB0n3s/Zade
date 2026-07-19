param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$ReviewType = "daily",
    [int]$MaxRun = 5,
    [int]$MaxImport = 5,
    [string]$ExperimentReviewType = "weekly",
    [string]$ExperimentPeriod = "",
    [int]$MaxExperimentReviews = 10,
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

. (Join-Path $PSScriptRoot "zade-token.ps1")
$Token = Resolve-ZadeToken -BaseUrl $BaseUrl -Token $Token
$Headers = @{}
if ($Token) {
    $Headers["X-Zade-Token"] = $Token
}

$Body = @{
    run_autonomous = $true
    max_run = $MaxRun
    review_type = $ReviewType
    import_candidates = $true
    max_import = $MaxImport
    link_goals = $true
    clear_resolved_warnings = $true
    experiment_review_type = $ExperimentReviewType
    experiment_period = if ($ExperimentPeriod) { $ExperimentPeriod } else { $null }
    max_experiment_reviews = $MaxExperimentReviews
}

$Result = Invoke-RestMethod -Uri "$BaseUrl/runtime/cadence" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 120
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "cadence-$Stamp.json"
$Result | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $LogPath -Encoding UTF8

Write-Output "Cadence: ok"
Write-Output "Audit ID: $($Result.audit_id)"
Write-Output "Next action: $($Result.next_action)"
Write-Output "Log: $LogPath"
