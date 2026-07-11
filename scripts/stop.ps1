$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root ".server.pid"

if (Test-Path -LiteralPath $PidFile) {
    $PidValue = Get-Content -LiteralPath $PidFile -Raw
    if ($PidValue) {
        $Process = Get-Process -Id ([int]$PidValue) -ErrorAction SilentlyContinue
        if ($Process) {
            Stop-Process -Id $Process.Id -Force
            Write-Output "Stopped server PID $($Process.Id)"
        }
    }
    Remove-Item -LiteralPath $PidFile -Force
} else {
    Write-Output "No PID file found."
}

$Listeners = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
foreach ($Listener in $Listeners) {
    $Process = Get-Process -Id $Listener.OwningProcess -ErrorAction SilentlyContinue
    if ($Process) {
        Stop-Process -Id $Process.Id -Force
        Write-Output "Stopped listener PID $($Process.Id)"
    }
}
