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

. (Join-Path $PSScriptRoot "zade-token.ps1")
$Token = Resolve-ZadeToken -BaseUrl $BaseUrl -Token $Token
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

Add-Check "approval-console-ui" {
    $Response = Invoke-WebRequest -Uri "$BaseUrl/ui/approvals.html" -UseBasicParsing -TimeoutSec 10
    if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
        throw "Approval console UI returned HTTP $($Response.StatusCode)."
    }
    if (-not $Response.Content.Contains("Zade Action Approval Console")) {
        throw "Approval console UI did not render the shell."
    }
    @{ status_code = $Response.StatusCode; bytes = $Response.Content.Length }
}

Add-Check "inventory" {
    $Inventory = Invoke-RestMethod -Uri "$BaseUrl/self-inventory" -TimeoutSec 10
    if (-not ($Inventory.work_queue.routes -contains "POST /work/items/{item_id}/dispatch")) {
        throw "Dispatch route missing from self-inventory."
    }
    if (-not ($Inventory.work_queue.routes -contains "GET /approval-console")) {
        throw "Approval console route missing from self-inventory."
    }
    if (-not ($Inventory.work_queue.routes -contains "POST /approval-requests/{request_id}/defer")) {
        throw "Approval defer route missing from self-inventory."
    }
    if (-not ($Inventory.work_queue.routes -contains "POST /approval-requests/{request_id}/edit")) {
        throw "Approval edit route missing from self-inventory."
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

Add-Check "approval-console" {
    $Key = "smoke.approval.console:$([guid]::NewGuid().ToString())"
    $CreateBody = @{
        kind = "smoke"
        title = "Smoke approval console"
        detail = "Verify action approval console edit and defer."
        action = "browser.open"
        target = "https://example.com/smoke"
        permission_tier = "L3_EXTERNAL_ACTION"
        source = "smoke-contract"
        unique_key = $Key
        metadata = @{
            evidence = @("Smoke contract created this request.")
            risks = @("External browser action should stay approval-gated.")
        }
    }
    $Created = Invoke-RestMethod -Uri "$BaseUrl/work/items" -Method Post -Headers $Headers -Body ($CreateBody | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 30
    if ($Created.status -ne "approval_required") {
        throw "Expected approval_required for console smoke, got $($Created.status)."
    }
    $Console = Invoke-RestMethod -Uri "$BaseUrl/approval-console?status=pending&limit=100" -TimeoutSec 20
    $Request = $Console.items | Where-Object { $_.work_item.id -eq $Created.item_id } | Select-Object -First 1
    if (-not $Request) {
        throw "Console did not expose the smoke approval request."
    }
    if (-not $Request.evidence.items -or $Request.evidence.items.Count -lt 1) {
        throw "Console did not expose approval evidence."
    }
    if (-not $Request.risk.items -or $Request.risk.items.Count -lt 1) {
        throw "Console did not expose approval risk."
    }
    $EditBody = @{
        edited_by = "smoke-contract"
        note = "Keep smoke local."
        title = "Smoke approval console edited"
        action = "local.browser.open"
        target = "http://127.0.0.1:8787/ui/approvals.html"
        permission_tier = "L3_EXTERNAL_ACTION"
        evidence = @("Edited by smoke contract.")
        risks = @("Still requires approval and typed dispatch.")
    }
    $Edited = Invoke-RestMethod -Uri "$BaseUrl/approval-requests/$($Request.id)/edit" -Method Post -Headers $Headers -Body ($EditBody | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 30
    if ($Edited.request.action -ne "local.browser.open") {
        throw "Approval edit did not update the action."
    }
    $DeferBody = @{
        resolved_by = "smoke-contract"
        note = "Deferred by smoke contract."
        defer_until = (Get-Date).AddDays(1).ToString("o")
    }
    $Deferred = Invoke-RestMethod -Uri "$BaseUrl/approval-requests/$($Request.id)/defer" -Method Post -Headers $Headers -Body ($DeferBody | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 30
    if ($Deferred.request.status -ne "deferred") {
        throw "Approval defer did not set request deferred."
    }
    $Training = Invoke-RestMethod -Uri "$BaseUrl/approval-training-events?approval_request_id=$($Request.id)&limit=10" -TimeoutSec 20
    $Outcomes = @($Training.items | ForEach-Object { $_.outcome })
    if (-not ($Outcomes -contains "edited") -or -not ($Outcomes -contains "deferred")) {
        throw "Approval training events did not record edit and defer."
    }
    @{ request_id = $Request.id; outcomes = $Outcomes }
}

Add-Check "cadence-approval-pressure" {
    $Key = "smoke.cadence.approval:$([guid]::NewGuid().ToString())"
    $CreateBody = @{
        kind = "smoke"
        title = "Smoke cadence approval pressure"
        detail = "Verify cadence sees approval-console blockers."
        action = "local.browser.open"
        target = "http://127.0.0.1:8787/ui/approvals.html"
        permission_tier = "L3_EXTERNAL_ACTION"
        priority = 96
        source = "smoke-contract"
        unique_key = $Key
        metadata = @{
            evidence = @("Smoke contract created cadence approval pressure.")
            risks = @("Still requires approval and typed dispatch.")
        }
    }
    $Created = Invoke-RestMethod -Uri "$BaseUrl/work/items" -Method Post -Headers $Headers -Body ($CreateBody | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 30
    if ($Created.status -ne "approval_required") {
        throw "Expected cadence approval item to require approval, got $($Created.status)."
    }
    $Console = Invoke-RestMethod -Uri "$BaseUrl/approval-console?status=pending&limit=100" -TimeoutSec 20
    $Request = $Console.items | Where-Object { $_.work_item.id -eq $Created.item_id } | Select-Object -First 1
    if (-not $Request) {
        throw "Cadence smoke approval was not visible in approval console."
    }
    $CadenceBody = @{
        run_autonomous = $false
        max_run = 0
        review_type = "daily"
        import_candidates = $false
        max_import = 0
        max_experiment_reviews = 1
    }
    $Cadence = Invoke-RestMethod -Uri "$BaseUrl/runtime/cadence" -Method Post -Headers $Headers -Body ($CadenceBody | ConvertTo-Json -Depth 10) -ContentType "application/json" -TimeoutSec 45
    $Pressure = $Cadence.operating.cadence.findings.approval_pressure
    if (-not $Pressure -or $Pressure.pending -lt 1) {
        throw "Cadence did not report pending approval pressure."
    }
    $TopIds = @($Pressure.items | ForEach-Object { $_.id })
    if (-not ($TopIds -contains $Request.id)) {
        throw "Cadence approval pressure did not include the smoke approval request."
    }
    if (-not $Cadence.operating.cadence.highest_leverage_action.Contains("/ui/approvals.html")) {
        throw "Cadence highest leverage action does not point to the approval console."
    }
    $DenyBody = @{
        resolved_by = "smoke-contract"
        note = "Cadence approval pressure smoke complete."
    }
    Invoke-RestMethod -Uri "$BaseUrl/approval-requests/$($Request.id)/deny" -Method Post -Headers $Headers -Body ($DenyBody | ConvertTo-Json -Depth 8) -ContentType "application/json" -TimeoutSec 30 | Out-Null
    @{ request_id = $Request.id; pending = $Pressure.pending; highest = $Cadence.operating.cadence.highest_leverage_action }
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
