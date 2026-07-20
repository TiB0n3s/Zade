# Project-Autonomy Planning Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Stop project autonomy from re-asking resolved decisions or endlessly retrying invalid MVP plans, then safely restart Same Ground and The Dark Index with their founder-authored inputs.

**Architecture:** Keep \`ProjectMvpPlanner\` as the strict structured-output validator and make its prompt explicitly treat historical founder answers as binding constraints. Make \`ProjectAutonomyOrchestrator\` convert planner validation failures and duplicate decision requests into evidence-backed blocked results instead of leaking worker errors. Keep the Dark Index ZIP immutable; extract its README into its repository as the planner-eligible written UI source.

**Tech Stack:** Python 3.14, FastAPI, SQLite/WAL, local Ollama structured output, pytest, PowerShell, Flutter documentation.

## Global Constraints

- Preserve local-only provider policy and founder-boundary controls.
- Treat \`decision_applied\` history as binding input, never as model prose.
- Never silently repair, invent, or delete planner criteria.
- Do not create another decision for a choice already answered in the unchanged project revision.
- Preserve the existing unresolved \`context/self/zade.md\` conflict and all unrelated changes.
- The Dark Index handoff README is canonical written UI scope; the HTML files remain visual-only references.

---

## File Structure

- \`src/cofounder_kernel/project_mvp_planner.py\` — binding-answer prompt contract.
- \`src/cofounder_kernel/project_autonomy_orchestrator.py\` — controlled plan failure and duplicate-decision recovery.
- \`tests/test_project_mvp_planner.py\` — prompt contract regression test.
- \`tests/test_project_autonomy_orchestrator.py\` — durable-block recovery tests.
- \`C:\\AI Brain\\project-intake\\The Dark Index\\THE_DARK_INDEX_UI_HANDOFF.md\` — extracted canonical UI handoff.

### Task 1: Bind resolved founder answers in the planner prompt

**Files:**

- Modify: \`src/cofounder_kernel/project_mvp_planner.py:264-286\`
- Modify: \`tests/test_project_mvp_planner.py\`

**Interfaces:**

- Consumes: \`project["metadata"]["planner_founder_answers"]: list[str]\`.
- Produces: \`_planning_messages(project, documents) -> list[dict[str, str]]\`.

- [ ] **Step 1: Write the failing test**

\`\`\`python
def test_planner_prompt_treats_founder_answers_as_binding_constraints(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    fake = FakeOllama(valid_payload())
    project = project_record(root)
    project["metadata"] = {"planner_founder_answers": ["Stick to current ABIs."]}

    ProjectMvpPlanner(config=config_for(root), ollama=fake).plan(project)

    system = str(fake.calls[0]["messages"][0]["content"])
    prompt = "\n".join(str(message["content"]) for message in fake.calls[0]["messages"])
    assert "binding founder constraints" in system
    assert "must not return needs_decision" in system
    assert "Stick to current ABIs." in prompt
\`\`\`

- [ ] **Step 2: Verify red**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_mvp_planner.py::test_planner_prompt_treats_founder_answers_as_binding_constraints -q\`

Expected: FAIL because the current prompt has no binding-constraint rule.

- [ ] **Step 3: Implement the minimal prompt rule**

\`\`\`python
"Accepted founder answers are binding founder constraints. Treat each as resolved "
"scope, and must not return needs_decision for a choice already answered there. "
\`\`\`

Insert it after the existing no-invention rule in \`_planning_messages\`.

- [ ] **Step 4: Verify green**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_mvp_planner.py -q\`

Expected: PASS.

- [ ] **Step 5: Commit if the index is conflict-free**

\`\`\`powershell
git add src/cofounder_kernel/project_mvp_planner.py tests/test_project_mvp_planner.py
git commit -m "fix: bind founder answers in MVP planning"
\`\`\`

Expected: a commit containing only Task 1 files. If Git refuses because of the pre-existing unresolved conflict, do not touch or stage \`context/self/zade.md\`.

### Task 2: Turn invalid plans and duplicate decisions into durable blocks

**Files:**

- Modify: \`src/cofounder_kernel/project_autonomy_orchestrator.py:269-306\`
- Modify: \`tests/test_project_autonomy_orchestrator.py\`

**Interfaces:**

- Consumes: \`ProjectMvpPlanner.plan(project) -> MvpPlanResult\`.
- Consumes: \`ProjectAutonomyReporter.report_blocked(project_id, reason, verification_output, attempts, needed)\`.
- Produces: \`run_once() -> {"status": "blocked", "project_id": int, "reason": str}\` for both failure modes.

- [ ] **Step 1: Write the failing invalid-plan regression test**

\`\`\`python
def test_invalid_planner_dependency_becomes_a_durable_block(tmp_path: Path) -> None:
    orchestrator, reporter, db, config, _ = make_services(tmp_path)
    project_id, _root = make_project(db, config, "The Dark Index")
    orchestrator.planner = RaisingPlanner(
        ValueError("Criterion mvp-cloud-backup-mvp depends on unknown criterion mvp-account-creation.")
    )

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "depends on unknown criterion" in result["reason"]
    assert reporter.state(project_id)["phase"] == "blocked"
\`\`\`

Add \`RaisingPlanner.plan(project)\` to raise its injected \`ValueError\`.

- [ ] **Step 2: Verify red**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_autonomy_orchestrator.py::test_invalid_planner_dependency_becomes_a_durable_block -q\`

Expected: FAIL because the planner \`ValueError\` escapes the worker.

- [ ] **Step 3: Write the failing duplicate-decision regression test**

\`\`\`python
def test_repeated_planning_decision_is_blocked_without_a_new_work_item(tmp_path: Path) -> None:
    planner = FakePlanner(MvpPlanResult(
        criteria=[], external_boundaries=[], source_hash="same", plan_revision="same",
        needs_decision={
            "question": "Should the project support additional Android ABIs?",
            "recommendation": "Keep the current ABIs.",
            "options": [
                {"option": "Keep the current ABIs", "impact": "Matches the resolved choice."},
                {"option": "Add more ABIs", "impact": "Broadens package scope."},
            ],
        },
    ))
    orchestrator, reporter, db, config, _ = make_services(tmp_path, planner=planner)
    project_id, _root = make_project(db, config, "Same Ground")
    db.append_project_event(project_id, event_type="decision_applied", detail="Stick to current ABIs.")

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "already resolved" in result["reason"]
    assert reporter.state(project_id)["phase"] == "blocked"
    assert [item for item in db.list_work_items() if item.kind == "founder_decision"] == []
\`\`\`

- [ ] **Step 4: Verify red**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_autonomy_orchestrator.py::test_repeated_planning_decision_is_blocked_without_a_new_work_item -q\`

Expected: FAIL because the current orchestrator files a work item.

- [ ] **Step 5: Implement guarded planning**

\`\`\`python
try:
    planned = self.planner.plan(planning_project)
except ValueError as exc:
    reason = f"Local MVP planner returned an invalid plan: {exc}"
    self.reporter.report_blocked(
        project_id, reason=reason, verification_output=str(exc), attempts=1,
        needed="correct the documented MVP plan and wake project autonomy",
    )
    return {"status": "blocked", "project_id": project_id, "reason": reason}

if planned.needs_decision is not None and _decision_repeats_accepted_answer(
    planned.needs_decision, planning_metadata["planner_founder_answers"]
):
    reason = "Local MVP planner repeated a founder decision that is already resolved."
    self.reporter.report_blocked(
        project_id, reason=reason,
        verification_output=json.dumps(planned.needs_decision, sort_keys=True), attempts=1,
        needed="re-plan from recorded founder constraints without filing another decision",
    )
    return {"status": "blocked", "project_id": project_id, "reason": reason}
\`\`\`

Implement private \`_decision_repeats_accepted_answer(decision: dict[str, Any], answers: list[str]) -> bool\` using conservative normalized token overlap over question, recommendation, options, and answer. It must return false when no substantive token overlap exists.

- [ ] **Step 6: Verify green**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_autonomy_orchestrator.py -q\`

Expected: PASS.

- [ ] **Step 7: Commit if the index is conflict-free**

\`\`\`powershell
git add src/cofounder_kernel/project_autonomy_orchestrator.py tests/test_project_autonomy_orchestrator.py
git commit -m "fix: block invalid project MVP plans safely"
\`\`\`

Expected: a commit with only Task 2 files, subject to the pre-existing conflict constraint.

### Task 3: Add the Dark Index handoff as planner-eligible documentation

**Files:**

- Create: \`C:\\AI Brain\\project-intake\\The Dark Index\\THE_DARK_INDEX_UI_HANDOFF.md\`
- Source: \`The Dark Index Mobile App.zip::design_handoff_dark_index_mvp/README.md\`

**Interfaces:**

- Consumes: user-supplied ZIP README.
- Produces: Markdown automatically discovered by existing \`DOCUMENT_SUFFIXES\` scanning.

- [ ] **Step 1: Verify it is absent**

Run: \`rg -n "High-fidelity mobile UI" "C:\\AI Brain\\project-intake\\The Dark Index\\THE_DARK_INDEX_UI_HANDOFF.md"\`

Expected: file-not-found before extraction.

- [ ] **Step 2: Extract only the README**

\`\`\`powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead('C:\AI Brain\project-intake\The Dark Index\The Dark Index Mobile App.zip')
try {
  $entry = $zip.GetEntry('design_handoff_dark_index_mvp/README.md')
  if ($null -eq $entry) { throw 'The Dark Index UI handoff README is absent from the supplied archive.' }
  $reader = [System.IO.StreamReader]::new($entry.Open())
  try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
} finally { $zip.Dispose() }
Set-Content -LiteralPath 'C:\AI Brain\project-intake\The Dark Index\THE_DARK_INDEX_UI_HANDOFF.md' -Value $content -NoNewline -Encoding utf8
\`\`\`

- [ ] **Step 3: Verify preservation and commit it in the Dark Index repository**

Run: \`rg -n "Library home|Android variant|Three user-selectable themes" "C:\\AI Brain\\project-intake\\The Dark Index\\THE_DARK_INDEX_UI_HANDOFF.md"\`

Expected: all phrases present.

\`\`\`powershell
git -C 'C:\AI Brain\project-intake\The Dark Index' add THE_DARK_INDEX_UI_HANDOFF.md
git -C 'C:\AI Brain\project-intake\The Dark Index' commit -m "docs: add canonical mobile UI handoff"
\`\`\`

Expected: archive unchanged; Markdown committed as the planner citation source.

### Task 4: Run full verification and recover the two live projects

**Files:**

- Verify: \`tests/test_project_mvp_planner.py\`, \`tests/test_project_autonomy_orchestrator.py\`, \`tests/test_project_autonomy.py\`, \`tests/test_project_autonomy_api.py\`, \`tests/test_project_autonomy_live_contract.py\`.
- Operate through existing \`/project-intake/*\` APIs only; never write the SQLite database directly.

**Interfaces:**

- Consumes: verified code and the Task 3 handoff document.
- Produces: valid planned criteria or one truthful durable block/decision per project; never false MVP completion.

- [ ] **Step 1: Run the focused suite**

Run: \`.venv\\Scripts\\python.exe -m pytest tests/test_project_mvp_planner.py tests/test_project_autonomy_orchestrator.py tests/test_project_autonomy.py tests/test_project_autonomy_api.py tests/test_project_autonomy_live_contract.py -q\`

Expected: PASS.

- [ ] **Step 2: Restart verified kernel**

\`\`\`powershell
.\scripts\stop.ps1
.\scripts\start.ps1 -NoOpen -TimeoutSec 60
\`\`\`

Expected: \`GET /health\` reports \`ok: true\`, \`project_autonomy.started: true\`, and two workers.

- [ ] **Step 3: Reconcile Same Ground through the supported wake/resume path**

Read \`GET /project-intake/projects/1\` and project events before and after the existing wake/resume action. Expected: no newly filed ABI decision; either valid criteria or one reporter-backed planning block; \`mvp_complete: false\`.

- [ ] **Step 4: Reconcile The Dark Index through the supported wake/resume path**

Read \`GET /project-intake/projects/3\` and events before and after the existing wake/resume action. Expected: no recurring raw unknown-dependency \`worker_error\`; any valid UI criterion cites \`THE_DARK_INDEX_UI_HANDOFF.md\`; \`mvp_complete: false\` unless every real criterion and commit-bound verification completed.

- [ ] **Step 5: Report verified state**

Run: \`git status --short\` and read \`GET /health\` plus \`GET /project-intake/status\`.

Expected: final report separates our edits, the pre-existing conflict, live project phases, and any real unresolved founder boundary.

