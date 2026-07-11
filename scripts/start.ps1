$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root ".server.pid"
$LogDir = Join-Path $Root "run-logs"
$OutLog = Join-Path $LogDir "server.out.log"
$ErrLog = Join-Path $LogDir "server.err.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing venv Python at $Python. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -Raw
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        Write-Output "Server already running with PID $ExistingPid"
        exit 0
    }
}

$Process = Start-Process -FilePath $Python -ArgumentList "-m", "cofounder_kernel" -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
Start-Sleep -Seconds 2
$Listener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Listener) {
    Set-Content -LiteralPath $PidFile -Value $Listener.OwningProcess
    Write-Output "Started Local AI Co-founder Kernel with PID $($Listener.OwningProcess)"
} else {
    Set-Content -LiteralPath $PidFile -Value $Process.Id
    Write-Output "Started Local AI Co-founder Kernel with launcher PID $($Process.Id); listener not ready yet"
}
Write-Output "URL: http://127.0.0.1:8787"
