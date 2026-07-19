param(
    [string]$TaskName = "Zade Project Intake Watcher"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$WatcherScript = Join-Path $PSScriptRoot "run-project-intake-watcher.ps1"
$PowerShell = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe"
if (-not (Test-Path -LiteralPath $PowerShell)) {
    $PowerShell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
}
if (-not $PowerShell) {
    $PowerShell = (Get-Command powershell.exe).Source
}

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WatcherScript`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Watches Zade's project-intake Vault folder and triggers governed project registration and builds." -Force -ErrorAction Stop | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Watcher: $WatcherScript"
