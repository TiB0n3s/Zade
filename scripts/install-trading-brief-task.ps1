param(
    [string]$TaskName = "Zade Trading Intelligence Brief",
    [string]$At = "5:30PM"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BriefScript = Join-Path $PSScriptRoot "run-trading-brief.ps1"
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
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BriefScript`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Runs Zade's local read-only trading intelligence brief, judgment ledger, and outcome scoring loop." `
    -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"
Write-Output "Schedule: daily at $At"
Write-Output "Script: $BriefScript"
