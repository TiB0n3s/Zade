param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$ProjectIntake = "C:\AI Brain\project-intake",
    [int]$DebounceSeconds = 3,
    [int]$ScanTimeoutSeconds = 3900,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "run-logs"
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$TokenScript = Join-Path $PSScriptRoot "zade-token.ps1"
$BaseUrl = $BaseUrl.TrimEnd("/")

New-Item -ItemType Directory -Force -Path $ProjectIntake, $LogDir | Out-Null

if (-not $NoStart) {
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
} else {
    Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 5 | Out-Null
}

. $TokenScript

function Write-ProjectIntakeWatcherLog {
    param([hashtable]$Entry)

    $Entry["timestamp"] = (Get-Date).ToString("o")
    $Entry | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $LogDir "project-intake-watcher.jsonl") -Encoding utf8
}

function Invoke-ProjectIntakeScan {
    $Token = Resolve-ZadeToken -BaseUrl $BaseUrl
    $Headers = @{ "X-Zade-Token" = $Token }
    $Result = Invoke-RestMethod -Uri "$BaseUrl/project-intake/scan" -Method Post -Headers $Headers -ContentType "application/json" -TimeoutSec $ScanTimeoutSeconds
    Write-ProjectIntakeWatcherLog @{
        event = "scan"
        created_count = $Result.created_count
        existing_count = $Result.existing_count
        error_count = @($Result.errors).Count
    }
    Write-Output "Project intake scan: registered $($Result.created_count), already known $($Result.existing_count), errors $(@($Result.errors).Count)."
}

$Watcher = [System.IO.FileSystemWatcher]::new($ProjectIntake, "*")
$Watcher.IncludeSubdirectories = $true
$Watcher.NotifyFilter = [System.IO.NotifyFilters]::DirectoryName -bor [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite
$SourceIds = @("ZadeProjectIntakeCreated", "ZadeProjectIntakeChanged", "ZadeProjectIntakeRenamed")

Register-ObjectEvent -InputObject $Watcher -EventName Created -SourceIdentifier $SourceIds[0] | Out-Null
Register-ObjectEvent -InputObject $Watcher -EventName Changed -SourceIdentifier $SourceIds[1] | Out-Null
Register-ObjectEvent -InputObject $Watcher -EventName Renamed -SourceIdentifier $SourceIds[2] | Out-Null
$Watcher.EnableRaisingEvents = $true

Write-ProjectIntakeWatcherLog @{ event = "started"; project_intake = $ProjectIntake }
Invoke-ProjectIntakeScan

try {
    while ($true) {
        $Event = $null
        while (-not $Event) {
            foreach ($SourceId in $SourceIds) {
                $Event = Wait-Event -SourceIdentifier $SourceId -Timeout 1
                if ($Event) { break }
            }
        }
        Remove-Event -EventIdentifier $Event.EventIdentifier
        Start-Sleep -Seconds $DebounceSeconds
        while ($Pending = Get-Event -ErrorAction SilentlyContinue | Where-Object { $_.SourceIdentifier -in $SourceIds } | Select-Object -First 1) {
            Remove-Event -EventIdentifier $Pending.EventIdentifier
        }
        try {
            Invoke-ProjectIntakeScan
        } catch {
            Write-ProjectIntakeWatcherLog @{ event = "scan_error"; error = $_.Exception.Message }
            Write-Warning "Project intake scan failed: $($_.Exception.Message)"
        }
    }
} finally {
    $Watcher.EnableRaisingEvents = $false
    foreach ($SourceId in $SourceIds) {
        Unregister-Event -SourceIdentifier $SourceId -ErrorAction SilentlyContinue
    }
    $Watcher.Dispose()
    Write-ProjectIntakeWatcherLog @{ event = "stopped" }
}
