param(
    [string]$TaskName = "Zade Local Health Monitor",
    [int]$EveryMinutes = 60
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$MonitorScript = Join-Path $PSScriptRoot "monitor-health.ps1"
# Prefer the Store app-execution ALIAS over Get-Command: the latter resolves to
# a version-pinned WindowsApps path (...PowerShell_7.6.3.0...) that breaks the
# task the next time the Store updates PowerShell. The alias is stable.
$PowerShell = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe"
if (-not (Test-Path -LiteralPath $PowerShell)) {
    $PowerShell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
}
if (-not $PowerShell) {
    $PowerShell = (Get-Command powershell.exe).Source
}

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$MonitorScript`" -StartIfDown" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Checks Zade kernel, UI, Ollama, and cadence posture." -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Interval: every $EveryMinutes minutes"
Write-Output "Script: $MonitorScript"
