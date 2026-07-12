param(
    [switch]$NoOpen,
    [int]$TimeoutSec = 30
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root ".server.pid"
$LogDir = Join-Path $Root "run-logs"
$OutLog = Join-Path $LogDir "server.out.log"
$ErrLog = Join-Path $LogDir "server.err.log"
$BaseUrl = "http://127.0.0.1:8787"
$HealthUrl = "$BaseUrl/health"
$UiUrl = "$BaseUrl/ui"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing venv Python at $Python. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$RunningPid = $null
if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -Raw
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        $RunningPid = [int]$ExistingPid
    }
}

if (-not $RunningPid) {
    $Listener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Listener -and (Get-Process -Id $Listener.OwningProcess -ErrorAction SilentlyContinue)) {
        $RunningPid = [int]$Listener.OwningProcess
        Set-Content -LiteralPath $PidFile -Value $RunningPid
    }
}

if ($RunningPid) {
    Write-Output "Kernel already running with PID $RunningPid"
} else {
    $Process = Start-Process -FilePath $Python -ArgumentList "-m", "cofounder_kernel" -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
    Set-Content -LiteralPath $PidFile -Value $Process.Id
    Write-Output "Started Local AI Co-founder Kernel launcher PID $($Process.Id)"
}

$Deadline = (Get-Date).AddSeconds($TimeoutSec)
$Health = $null
$LastError = $null
while ((Get-Date) -lt $Deadline) {
    try {
        $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
        if ($Health.ok) {
            break
        }
    } catch {
        $LastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 500
}

if (-not $Health -or -not $Health.ok) {
    Write-Output "Kernel did not pass health check at $HealthUrl"
    if ($LastError) {
        Write-Output $LastError
    }
    if (Test-Path -LiteralPath $ErrLog) {
        Write-Output "Recent stderr:"
        Get-Content -LiteralPath $ErrLog -Tail 20
    }
    throw "Startup health check failed."
}

$Listener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Listener) {
    Set-Content -LiteralPath $PidFile -Value $Listener.OwningProcess
    Write-Output "Kernel PID: $($Listener.OwningProcess)"
}

$UiResponse = Invoke-WebRequest -Uri $UiUrl -UseBasicParsing -TimeoutSec 5
if ($UiResponse.StatusCode -lt 200 -or $UiResponse.StatusCode -ge 400) {
    throw "UI check failed at $UiUrl with HTTP $($UiResponse.StatusCode)."
}

Write-Output "Health: ok"
Write-Output "UI: $UiUrl"

if (-not $NoOpen) {
    Start-Process -FilePath $UiUrl | Out-Null
    Write-Output "Opened Zade UI."
}
