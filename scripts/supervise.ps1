param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$DataDir = "C:\AI Brain\memory-hot\cofounder-kernel",
    [switch]$CheckOnly,
    [int]$StartTimeoutSec = 45,
    [int]$MaxLogLines = 2000
)

$ErrorActionPreference = "Stop"

$StartScript = Join-Path $PSScriptRoot "start.ps1"
$BaseUrl = $BaseUrl.TrimEnd("/")
$HealthUrl = "$BaseUrl/health"
$LogDir = Join-Path $DataDir "supervision"
$LogPath = Join-Path $LogDir "supervisor-log.jsonl"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-SupervisionEvent {
    param(
        [string]$EventName,
        [bool]$Ok,
        [string]$Detail = ""
    )
    $Entry = [ordered]@{
        timestamp  = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss") + "+00:00"
        event      = $EventName
        ok         = $Ok
        detail     = $Detail
        base_url   = $BaseUrl
        supervisor = "supervise.ps1"
    }
    Add-Content -LiteralPath $LogPath -Value ($Entry | ConvertTo-Json -Compress) -Encoding UTF8
    $Lines = Get-Content -LiteralPath $LogPath
    if ($Lines.Count -gt $MaxLogLines) {
        $Lines | Select-Object -Last $MaxLogLines | Set-Content -LiteralPath $LogPath -Encoding UTF8
    }
}

function Test-KernelHealth {
    try {
        $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
        if ($Health.ok) {
            return $Health
        }
        return $null
    } catch {
        return $null
    }
}

$Health = Test-KernelHealth
if ($Health) {
    Write-SupervisionEvent -EventName "healthy" -Ok $true -Detail "uptime_seconds=$($Health.uptime_seconds)"
    Write-Output "Supervisor: kernel healthy (uptime $($Health.uptime_seconds)s)."
    exit 0
}

Write-SupervisionEvent -EventName "unreachable" -Ok $false -Detail "Health check failed at $HealthUrl"
Write-Output "Supervisor: kernel unreachable at $HealthUrl."

if ($CheckOnly) {
    Write-Output "Supervisor: check-only mode; not starting the kernel."
    exit 1
}

try {
    & $StartScript -NoOpen -TimeoutSec $StartTimeoutSec | Write-Output
} catch {
    Write-SupervisionEvent -EventName "start_failed" -Ok $false -Detail $_.Exception.Message
    Write-Output "Supervisor: start failed: $($_.Exception.Message)"
    exit 1
}

$Health = Test-KernelHealth
if ($Health) {
    Write-SupervisionEvent -EventName "started" -Ok $true -Detail "Kernel recovered by supervisor."
    Write-Output "Supervisor: kernel started and healthy."
    exit 0
}

Write-SupervisionEvent -EventName "start_failed" -Ok $false -Detail "Kernel did not pass health check after start."
Write-Output "Supervisor: kernel did not pass health check after start."
exit 1
