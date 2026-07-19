param(
    [string]$TaskName = "Zade Local Cadence",
    [string]$At = "8:00AM"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$CadenceScript = Join-Path $PSScriptRoot "run-cadence.ps1"
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
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$CadenceScript`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Runs Zade's local operating, evidence, and experiment cadence loops." -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: daily at $At"
Write-Output "Script: $CadenceScript"
