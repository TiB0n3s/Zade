param(
    [string]$BaseUrl = "http://127.0.0.1:8787",
    [string]$Token = $env:COFOUNDER_LOCAL_TOKEN,
    [switch]$SkipModel,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$LogDir = Join-Path $Root "run-logs"
$BaseUrl = $BaseUrl.TrimEnd("/")

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $NoStart) {
    & $StartScript -NoOpen -TimeoutSec 45 | Write-Output
}

$Headers = @{}
if ($Token) {
    $Headers["X-Zade-Token"] = $Token
}

$Results = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    base_url = $BaseUrl
    checks = @{}
}

function Add-Check {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    try {
        $Value = & $Action
        $Results.checks[$Name] = @{ ok = $true; value = $Value }
        Write-Output "$Name`: ok"
    } catch {
        $Results.checks[$Name] = @{ ok = $false; error = $_.Exception.Message }
        Write-Output "$Name`: failed - $($_.Exception.Message)"
        throw
    }
}

Add-Check "health" {
    Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 10
}

Add-Check "ui" {
    $Response = Invoke-WebRequest -Uri "$BaseUrl/ui" -UseBasicParsing -TimeoutSec 10
    if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
        throw "UI returned HTTP $($Response.StatusCode)."
    }
    @{ status_code = $Response.StatusCode; contains_zade = $Response.Content.Contains("Zade") }
}

Add-Check "skills-ui" {
    $Response = Invoke-WebRequest -Uri "$BaseUrl/ui/skills.html" -UseBasicParsing -TimeoutSec 10
    if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
        throw "Skills UI returned HTTP $($Response.StatusCode)."
    }
    if (-not $Response.Content.Contains("Zade Skill Registry")) {
        throw "Skills UI did not render the registry shell."
    }
    @{ status_code = $Response.StatusCode; bytes = $Response.Content.Length }
}

Add-Check "founder-ops-ui" {
    $Response = Invoke-WebRequest -Uri "$BaseUrl/ui/founder.html" -UseBasicParsing -TimeoutSec 10
    if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
        throw "Founder Ops UI returned HTTP $($Response.StatusCode)."
    }
    if (-not $Response.Content.Contains("Zade Founder Ops")) {
        throw "Founder Ops UI did not render the shell."
    }
    @{ status_code = $Response.StatusCode; bytes = $Response.Content.Length }
}

Add-Check "inventory" {
    $Inventory = Invoke-RestMethod -Uri "$BaseUrl/self-inventory" -TimeoutSec 10
    if (-not ($Inventory.work_queue.routes -contains "POST /work/items/{item_id}/dispatch")) {
        throw "Dispatch route missing from self-inventory."
    }
    if (-not ($Inventory.skill_layer.routes -contains "POST /skills/scan")) {
        throw "Skill scan route missing from self-inventory."
    }
    if (-not ($Inventory.founder_operating_layer.routes -contains "POST /founder/decision-engine/recommend")) {
        throw "Decision engine route missing from self-inventory."
    }
    $Inventory
}

Add-Check "skills-registry" {
    $Summary = Invoke-RestMethod -Uri "$BaseUrl/skills/summary" -TimeoutSec 10
    if ($Summary.total -lt 1) {
        $Scan = Invoke-RestMethod -Uri "$BaseUrl/skills/scan" -Method Post -Headers $Headers -Body "{}" -ContentType "application/json" -TimeoutSec 45
        $Summary = $Scan.summary
    }
    if ($Summary.total -lt 1) {
        throw "No skills registered."
    }
    $Summary
}

Add-Check "skills-route" {
    $Body = @{
        query = "Debug this bug and verify the fix before claiming complete"
        task_type = "coding"
        limit = 3
    }
    $Route = Invoke-RestMethod -Uri "$BaseUrl/skills/route" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 20
    if ($Route.selected_count -lt 1) {
        throw "No skills selected for debugging route."
    }
    $Route
}

Add-Check "active-objective" {
    $Active = Invoke-RestMethod -Uri "$BaseUrl/founder/active-objective" -TimeoutSec 10
    if (-not $Active.item -or -not $Active.item.id) {
        $Body = @{
            objective = "Smoke: keep Zade operationally useful"
            desired_outcome = "Smoke contract proves founder operating endpoints."
            metric = "smoke_contract"
            target = "ok"
            next_action = "Keep the local founder operating loop healthy."
            risks = @("Founder layer drifts into passive chat.")
            activate = $true
            metadata = @{ smoke = $true }
        }
        $Created = Invoke-RestMethod -Uri "$BaseUrl/founder/active-objectives" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 20
        $Active = @{ item = $Created.item }
    }
    if (-not $Active.item.objective) {
        throw "No active objective available."
    }
    $Active
}

Add-Check "decision-engine" {
    $Body = @{
        problem = "Smoke: what should Zade verify next?"
        context = "Automated smoke contract for the local founder operating layer."
        options = @(
            @{ name = "Verify the local founder operating layer"; recommended = $true },
            @{ name = "Skip verification"; priority = 0 }
        )
        create_decision_memo = $false
        create_next_task = $false
        metadata = @{ smoke = $true }
    }
    $Result = Invoke-RestMethod -Uri "$BaseUrl/founder/decision-engine/recommend" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 20
    if (-not $Result.operating_contract.recommendation) {
        throw "Decision engine did not return an operating contract."
    }
    $Result.operating_contract
}

Add-Check "ops-health" {
    Invoke-RestMethod -Uri "$BaseUrl/ops/health-check" -TimeoutSec 10
}

if (-not $SkipModel) {
    Add-Check "runtime-respond" {
        $Body = @{
            message = "Smoke check. State the next local action in one sentence."
            use_semantic_memory = $false
            think = $false
        }
        Invoke-RestMethod -Uri "$BaseUrl/runtime/respond" -Method Post -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 180
    }
}

Add-Check "approved-handler-dispatch" {
    $Key = "smoke.local.noop:$([guid]::NewGuid().ToString())"
    $CreateBody = @{
        kind = "smoke"
        title = "Smoke approval dispatch"
        detail = "Verify approval-required local handler dispatch."
        action = "local.noop"
        target = "smoke"
        permission_tier = "L3_EXTERNAL_ACTION"
        source = "smoke-contract"
        unique_key = $Key
    }
    $Created = Invoke-RestMethod -Uri "$BaseUrl/work/items" -Method Post -Headers $Headers -Body ($CreateBody | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 30
    if ($Created.status -ne "approval_required") {
        throw "Expected approval_required, got $($Created.status)."
    }
    $ApproveBody = @{
        resolved_by = "smoke-contract"
        note = "Automated local smoke dispatch."
        dispatch = $true
        typed_confirmation = "make the jump to hyperspace"
    }
    Invoke-RestMethod -Uri "$BaseUrl/work/items/$($Created.item_id)/approve" -Method Post -Headers $Headers -Body ($ApproveBody | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 30
}

Add-Check "experiment-evidence" {
    $Experiments = Invoke-RestMethod -Uri "$BaseUrl/experiments?limit=100" -TimeoutSec 20
    $Experiment = $Experiments.items | Where-Object { $_.title -like "EXP-001*" } | Select-Object -First 1
    if (-not $Experiment) {
        $Experiment = Invoke-RestMethod -Uri "$BaseUrl/experiments" -Method Post -Headers $Headers -Body (@{
            title = "EXP-001: Smoke evidence intake"
            experiment_type = "validation"
            hypothesis = "Evidence can be logged from the local UI and scripts."
            owner = "Zade"
            minimum_evidence = 1
        } | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 20
        $Experiment = $Experiment.item
    }
    $EvidenceBody = @{
        evidence_type = "smoke_contract"
        source = "scripts/smoke-contract.ps1"
        title = "Smoke evidence intake"
        content = "The smoke contract logged evidence through the experiment endpoint."
        reliability = "B"
        strength = 60
        notes = "Generated by local smoke automation."
        metadata = @{ smoke = $true }
    }
    Invoke-RestMethod -Uri "$BaseUrl/experiments/$($Experiment.id)/evidence" -Method Post -Headers $Headers -Body ($EvidenceBody | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 30
}

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "smoke-$Stamp.json"
$Results | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath $LogPath -Encoding UTF8

Write-Output "Smoke: ok"
Write-Output "Log: $LogPath"
