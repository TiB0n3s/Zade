param(
    [string]$TaskName = "Zade Desktop Tray"
)

$ErrorActionPreference = "Stop"

# Registers the resident Zade tray to start at logon. Unlike the kernel
# supervisor (a repeating health check), the tray is a single long-running
# process, so this uses a logon trigger only, no execution time limit, and
# IgnoreNew so a second logon never spawns a duplicate tray.
$Root = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot "run-tray.ps1"
$PowerShell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $PowerShell) {
    $PowerShell = (Get-Command powershell.exe).Source
}

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $Root

$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $LogonTrigger -Settings $Settings -Principal $Principal -Description "Runs the resident Zade desktop tray at logon: status icon and OS toasts, polling the local kernel." -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: at logon (single resident process)"
Write-Output "Script: $RunScript"
Write-Output "Prerequisite: install the tray extra -> .venv\Scripts\pip install `"local-ai-cofounder-kernel[tray]`""
