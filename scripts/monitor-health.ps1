param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [switch]$StartIfDown,
    [switch]$RequireRecentCadence,
    [int]$MaxCadenceAgeHours = 30
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$LogDir = Join-Path $Root "run-logs"
$BaseUrl = $BaseUrl.TrimEnd("/")

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Invoke-Health {
    $Require = $RequireRecentCadence.IsPresent.ToString().ToLowerInvariant()
    Invoke-RestMethod -Uri "$BaseUrl/ops/health-check?max_cadence_age_hours=$MaxCadenceAgeHours&require_recent_cadence=$Require" -TimeoutSec 15
}

try {
    $Health = Invoke-Health
} catch {
    if (-not $StartIfDown) {
        throw
    }
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
    $Health = Invoke-Health
}

$Ui = Invoke-WebRequest -Uri "$BaseUrl/ui" -UseBasicParsing -TimeoutSec 10
if ($Ui.StatusCode -lt 200 -or $Ui.StatusCode -ge 400) {
    throw "UI returned HTTP $($Ui.StatusCode)."
}

$Task = Get-ScheduledTask -TaskName "Zade Local Cadence" -ErrorAction SilentlyContinue
$Payload = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    base_url = $BaseUrl
    health = $Health
    ui_status_code = $Ui.StatusCode
    scheduled_task = if ($Task) {
        @{
            task_name = $Task.TaskName
            state = [string]$Task.State
        }
    } else {
        $null
    }
}

$Latest = Join-Path $LogDir "health-latest.json"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Stamped = Join-Path $LogDir "health-$Stamp.json"
$Payload | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath $Latest -Encoding UTF8
$Payload | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath $Stamped -Encoding UTF8

if (-not $Health.ok) {
    throw "Health check failed. See $Latest"
}

Write-Output "Health monitor: ok"
Write-Output "Log: $Latest"
