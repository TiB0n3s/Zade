param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$Inbox = "C:\AI Brain\inbox",
    [int]$DebounceSeconds = 3,
    [int]$MaxRun = 10,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "run-logs"
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$TokenScript = Join-Path $PSScriptRoot "zade-token.ps1"
$BaseUrl = $BaseUrl.TrimEnd("/")
$SupportedExtensions = @(".csv", ".json", ".jsonl", ".log", ".md", ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml")

New-Item -ItemType Directory -Force -Path $Inbox, $LogDir | Out-Null

if (-not $NoStart) {
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
} else {
    Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 5 | Out-Null
}

. $TokenScript

function Write-WatcherLog {
    param([hashtable]$Entry)

    $Entry["timestamp"] = (Get-Date).ToString("o")
    $Entry | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $LogDir "inbox-watcher.jsonl") -Encoding utf8
}

function Test-SupportedInboxFile {
    param([string]$Path)

    return (Test-Path -LiteralPath $Path -PathType Leaf) -and ($SupportedExtensions -contains [IO.Path]::GetExtension($Path).ToLowerInvariant())
}

function Invoke-InboxScan {
    $Token = Resolve-ZadeToken -BaseUrl $BaseUrl
    $Headers = @{ "X-Zade-Token" = $Token }
    $Body = @{ run_autonomous = $true; max_run = $MaxRun } | ConvertTo-Json
    $Result = Invoke-RestMethod -Uri "$BaseUrl/work/scan" -Method Post -Headers $Headers -Body $Body -ContentType "application/json" -TimeoutSec 120
    Write-WatcherLog @{ event = "scan"; created_count = $Result.created_count; existing_count = $Result.existing_count; run_count = @($Result.run).Count }
    Write-Output "Inbox scan: queued $($Result.created_count), already known $($Result.existing_count), ran $(@($Result.run).Count)."
}

$Watcher = [System.IO.FileSystemWatcher]::new($Inbox, "*")
$Watcher.IncludeSubdirectories = $true
$Watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::Size
$SourceIds = @("ZadeInboxCreated", "ZadeInboxChanged", "ZadeInboxRenamed")

Register-ObjectEvent -InputObject $Watcher -EventName Created -SourceIdentifier $SourceIds[0] | Out-Null
Register-ObjectEvent -InputObject $Watcher -EventName Changed -SourceIdentifier $SourceIds[1] | Out-Null
Register-ObjectEvent -InputObject $Watcher -EventName Renamed -SourceIdentifier $SourceIds[2] | Out-Null
$Watcher.EnableRaisingEvents = $true

Write-WatcherLog @{ event = "started"; inbox = $Inbox }
Write-Output "Watching $Inbox for supported files. Press Ctrl+C to stop."

try {
    while ($true) {
        $Event = $null
        while (-not $Event) {
            foreach ($SourceId in $SourceIds) {
                $Event = Wait-Event -SourceIdentifier $SourceId -Timeout 1
                if ($Event) {
                    break
                }
            }
        }
        $Path = $Event.SourceEventArgs.FullPath
        Remove-Event -EventIdentifier $Event.EventIdentifier
        if (-not (Test-SupportedInboxFile -Path $Path)) {
            continue
        }

        Start-Sleep -Seconds $DebounceSeconds
        while ($Pending = Get-Event -ErrorAction SilentlyContinue | Where-Object { $_.SourceIdentifier -in $SourceIds } | Select-Object -First 1) {
            Remove-Event -EventIdentifier $Pending.EventIdentifier
        }

        try {
            Invoke-InboxScan
        } catch {
            Write-WatcherLog @{ event = "scan_error"; error = $_.Exception.Message }
            Write-Warning "Inbox scan failed: $($_.Exception.Message)"
        }
    }
} finally {
    $Watcher.EnableRaisingEvents = $false
    foreach ($SourceId in $SourceIds) {
        Unregister-Event -SourceIdentifier $SourceId -ErrorAction SilentlyContinue
    }
    $Watcher.Dispose()
    Write-WatcherLog @{ event = "stopped" }
}
