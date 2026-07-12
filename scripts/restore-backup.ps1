param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,
    [string]$DatabasePath = "",
    [switch]$StartAfter
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root ".server.pid"
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$DefaultDatabase = "C:\AI Brain\memory-hot\cofounder-kernel\cofounder.sqlite"

$ResolvedBackup = (Resolve-Path -LiteralPath $BackupPath).Path
if (-not (Test-Path -LiteralPath $ResolvedBackup -PathType Leaf)) {
    throw "Backup not found: $BackupPath"
}

if (-not $DatabasePath) {
    $DatabasePath = $DefaultDatabase
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8787/health" -TimeoutSec 2
        if ($Health.database) {
            $DatabasePath = [string]$Health.database
        }
    } catch {
        $DatabasePath = $DefaultDatabase
    }
}

$TargetParent = Split-Path -Parent $DatabasePath
if (-not (Test-Path -LiteralPath $TargetParent -PathType Container)) {
    throw "Database parent does not exist: $TargetParent"
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -Raw
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        Stop-Process -Id ([int]$ExistingPid) -Force
        Start-Sleep -Seconds 1
    }
}

$Listener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Listener -and (Get-Process -Id $Listener.OwningProcess -ErrorAction SilentlyContinue)) {
    throw "Port 8787 is still in use by PID $($Listener.OwningProcess). Stop it before restoring."
}

if (Test-Path -LiteralPath $DatabasePath -PathType Leaf) {
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $PreRestore = Join-Path $TargetParent "cofounder-pre-restore-$Stamp.sqlite"
    Copy-Item -LiteralPath $DatabasePath -Destination $PreRestore -Force
    Write-Output "Current database copied to: $PreRestore"
}

Copy-Item -LiteralPath $ResolvedBackup -Destination $DatabasePath -Force
Write-Output "Restored database from: $ResolvedBackup"
Write-Output "Database: $DatabasePath"

if ($StartAfter) {
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
}
