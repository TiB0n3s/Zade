param(
    [string]$LegacyDarkIndex = "C:\BookCatalogingApp",
    [string]$IntakeRoot = "C:\AI Brain\project-intake",
    [string]$QuarantineRoot = "C:\AI Brain\.trash\dark-index-legacy",
    [string]$Downloads = "$env:USERPROFILE\Downloads",
    [string]$Inbox = "C:\AI Brain\inbox",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$DarkIndexDestination = Join-Path $IntakeRoot "The Dark Index"
$SameGroundDestination = Join-Path $IntakeRoot "Same Ground"
$RunLogs = Join-Path (Split-Path -Parent $PSScriptRoot) "run-logs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$QuarantineDestination = Join-Path $QuarantineRoot $Timestamp
$ReceiptPath = Join-Path $RunLogs "project-intake-migration-receipt-$Timestamp.json"

function Resolve-AbsolutePath {
    param([string]$Path)
    return [IO.Path]::GetFullPath($Path)
}

function Assert-ChildPath {
    param([string]$Parent, [string]$Candidate)
    $ParentPath = (Resolve-AbsolutePath $Parent).TrimEnd('\')
    $CandidatePath = Resolve-AbsolutePath $Candidate
    if (-not $CandidatePath.StartsWith($ParentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes intended parent: $CandidatePath"
    }
    return $CandidatePath
}

function Select-SourceFile {
    param([string]$Name)
    $DownloadPath = Join-Path $Downloads $Name
    $InboxPath = Join-Path $Inbox $Name
    $Candidates = @(@($DownloadPath, $InboxPath) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if (-not $Candidates) { throw "Required founder source material is missing: $Name" }
    $Hashes = @(@($Candidates | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }) | Select-Object -Unique)
    if ($Hashes.Count -gt 1) { throw "Conflicting copies found for $Name; refusing to choose." }
    return [pscustomobject]@{ Path = $Candidates[0]; Hash = $Hashes[0]; Duplicates = @($Candidates | Select-Object -Skip 1) }
}

$IntakeRoot = Resolve-AbsolutePath $IntakeRoot
$QuarantineRoot = Resolve-AbsolutePath $QuarantineRoot
$LegacyDarkIndex = Resolve-AbsolutePath $LegacyDarkIndex
$DarkIndexDestination = Assert-ChildPath -Parent $IntakeRoot -Candidate $DarkIndexDestination
$SameGroundDestination = Assert-ChildPath -Parent $IntakeRoot -Candidate $SameGroundDestination
$QuarantineDestination = Assert-ChildPath -Parent $QuarantineRoot -Candidate $QuarantineDestination

if (-not (Test-Path -LiteralPath $LegacyDarkIndex -PathType Container)) {
    throw "Legacy Dark Index project does not exist: $LegacyDarkIndex"
}
foreach ($Destination in @($DarkIndexDestination, $SameGroundDestination, $QuarantineDestination)) {
    if (Test-Path -LiteralPath $Destination) { throw "Destination already exists; refusing to overwrite: $Destination" }
}

$Sources = @{
    dark_index_zade_context_pack = Select-SourceFile -Name "dark_index_zade_context_pack.md"
    dark_index_project_workbook = Select-SourceFile -Name "dark_index_project_workbook.xlsx"
    Same_Ground_Zade_Handoff = Select-SourceFile -Name "Same_Ground_Zade_Handoff.md"
    Same_Ground_Project_Workbook = Select-SourceFile -Name "Same_Ground_Project_Workbook.xlsx"
    Same_Ground_CSV_Bundle = Select-SourceFile -Name "Same_Ground_CSV_Bundle.zip"
}

$Receipt = [ordered]@{
    timestamp = (Get-Date).ToString("o")
    dry_run = [bool]$WhatIf
    legacy_source = $LegacyDarkIndex
    legacy_quarantine = $QuarantineDestination
    dark_index_destination = $DarkIndexDestination
    same_ground_destination = $SameGroundDestination
    sources = @($Sources.GetEnumerator() | ForEach-Object { @{ name = $_.Key; path = $_.Value.Path; sha256 = $_.Value.Hash } })
}

if ($WhatIf) {
    $Receipt | ConvertTo-Json -Depth 5
    return
}

New-Item -ItemType Directory -Force -Path $IntakeRoot, $QuarantineRoot, $RunLogs | Out-Null
Move-Item -LiteralPath $LegacyDarkIndex -Destination $QuarantineDestination
if (Test-Path -LiteralPath $LegacyDarkIndex) { throw "Legacy source still exists after quarantine move." }

New-Item -ItemType Directory -Path $DarkIndexDestination | Out-Null
New-Item -ItemType Directory -Path $SameGroundDestination | Out-Null

Move-Item -LiteralPath $Sources.dark_index_zade_context_pack.Path -Destination (Join-Path $DarkIndexDestination "dark_index_zade_context_pack.md")
Move-Item -LiteralPath $Sources.dark_index_project_workbook.Path -Destination (Join-Path $DarkIndexDestination "dark_index_project_workbook.xlsx")
Move-Item -LiteralPath $Sources.Same_Ground_Zade_Handoff.Path -Destination (Join-Path $SameGroundDestination "Same_Ground_Zade_Handoff.md")
Move-Item -LiteralPath $Sources.Same_Ground_Project_Workbook.Path -Destination (Join-Path $SameGroundDestination "Same_Ground_Project_Workbook.xlsx")
Move-Item -LiteralPath $Sources.Same_Ground_CSV_Bundle.Path -Destination (Join-Path $SameGroundDestination "Same_Ground_CSV_Bundle.zip")

$DarkManifest = @"
---
name: The Dark Index
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: true
---

# The Dark Index

A mobile application for cataloguing and understanding a physical book collection. It is not a reading platform. This is a clean implementation with no legacy build material.
"@
$SameManifest = @"
---
name: Same Ground
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: true
---

# Same Ground

A resource, support, and community mobile application for veterans, EMTs, and law-enforcement personnel, with optional service verification as a trust layer.
"@
Set-Content -LiteralPath (Join-Path $DarkIndexDestination "project.md") -Value $DarkManifest -Encoding utf8
Set-Content -LiteralPath (Join-Path $SameGroundDestination "project.md") -Value $SameManifest -Encoding utf8
$Receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReceiptPath -Encoding utf8

[pscustomobject]@{ ok = $true; receipt = $ReceiptPath; dark_index = $DarkIndexDestination; same_ground = $SameGroundDestination }
