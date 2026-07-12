param(
    [string]$TaskName = "Zade Local Supervisor",
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$SuperviseScript = Join-Path $PSScriptRoot "supervise.ps1"
$PowerShell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $PowerShell) {
    $PowerShell = (Get-Command powershell.exe).Source
}

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$SuperviseScript`"" `
    -WorkingDirectory $Root

$Interval = New-TimeSpan -Minutes $IntervalMinutes
$RepeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval $Interval -RepetitionDuration (New-TimeSpan -Days 3650)
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($RepeatTrigger, $LogonTrigger) -Settings $Settings -Principal $Principal -Description "Keeps the Zade kernel resident: starts it at logon and restarts it if the health check fails." -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: at logon, then every $IntervalMinutes minute(s)"
Write-Output "Script: $SuperviseScript"
