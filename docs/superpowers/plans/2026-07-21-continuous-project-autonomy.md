# Continuous Project Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continue safe documented project work after a verified MVP milestone while preserving founder-approval boundaries.

**Architecture:** Persist MVP attainment as a milestone and treat continuation as a new active scope using the existing criterion executor. Generalize the planner prompt by scope, make continuation runnable, and expose honest portfolio states for active delivery and external-only completion.

**Tech Stack:** Python 3.11, FastAPI, SQLite-backed `ProjectAutonomyReporter`, local Ollama planner, pytest.

## Global Constraints

- MVP evidence and its commit remain immutable milestone history.
- Only documented internal work may be delegated automatically.
- Deployment, store submission, paid services, external accounts or credentials, legal/publication work, and irreversible public actions stay approval-gated.
- Continuation keeps existing clean-Git, protected manifest, local-provider, fresh-verification, and bounded-repair controls.

---

### Task 1: Define test-first continuation state

**Files:**

- Modify: `tests/test_project_autonomy.py`
- Modify: `src/cofounder_kernel/project_autonomy.py`

**Interfaces:** `complete_mvp` records an MVP milestone and enters `continuation_planning`; `begin_continuation` accepts a fresh continuation plan.

- [ ] Write failing tests that assert MVP achievement is retained while the active state becomes continuation planning, and that a continuation plan becomes `ready_for_next_increment`.
- [ ] Run `pytest tests/test_project_autonomy.py -k "continuation or mvp" -q` and verify the old terminal state fails the new tests.
- [ ] Add `scope_kind`, `mvp_achieved`, milestone history, continuation source hash, `begin_continuation`, and external-boundary wait transitions.
- [ ] Run `pytest tests/test_project_autonomy.py -q`.

### Task 2: Make planning continuation-aware

**Files:**

- Modify: `tests/test_project_mvp_planner.py`
- Modify: `src/cofounder_kernel/project_mvp_planner.py`

**Interfaces:** `ProjectMvpPlanner.plan` receives active autonomy state through project metadata and returns only remaining internal criteria for a continuation scope.

- [ ] Write a failing prompt test that gives the planner prior MVP criteria and asserts its continuation prompt excludes them and preserves external actions as boundaries.
- [ ] Run the focused planner test and verify it fails against the MVP-only prompt.
- [ ] Add scope-specific prompt instructions and pass completed criterion IDs/milestone facts in the planner header.
- [ ] Run `pytest tests/test_project_mvp_planner.py -q`.

### Task 3: Resume completed projects automatically

**Files:**

- Modify: `tests/test_project_autonomy_orchestrator.py`
- Modify: `src/cofounder_kernel/project_autonomy_orchestrator.py`

**Interfaces:** `continuation_planning` invokes the planner; safe criteria use existing delegation; no safe criteria enters `awaiting_external_boundary` without delegation.

- [ ] Write failing tests that run a one-criterion MVP followed by a continuation criterion and verify the second `run_once` delegates it; add a no-criteria test that asserts no extra delegation.
- [ ] Run `pytest tests/test_project_autonomy_orchestrator.py -k "continuation" -q` and verify it fails because `_is_runnable` rejects MVP-complete projects.
- [ ] Replace the MVP terminal veto with phase-aware continuation eligibility, route `continuation_planning` through the planner, and generalize delegation copy from MVP criterion to documented scope criterion.
- [ ] Run `pytest tests/test_project_autonomy_orchestrator.py -q`.

### Task 4: Keep portfolio and source scans truthful

**Files:**

- Modify: `tests/test_project_autonomy.py`
- Modify: `tests/test_project_intake.py`
- Modify: `src/cofounder_kernel/project_autonomy.py`
- Modify: `src/cofounder_kernel/project_intake.py`

**Interfaces:** active continuation reports `continuing_delivery`; external-only delivery reports `awaiting_external_boundary`; a changed documentation fingerprint queues continuation planning and wakes the worker.

- [ ] Write failing projection and rescan tests for both portfolio states and a changed document.
- [ ] Run the focused tests and confirm the existing MVP terminal behavior fails them.
- [ ] Implement the projection and fingerprint-driven continuation transition.
- [ ] Run `pytest tests/test_project_autonomy.py tests/test_project_intake.py tests/test_project_autonomy_api.py -q`.

### Task 5: Verify and activate live delivery

**Files:**

- Modify: `docs/superpowers/specs/2026-07-21-continuous-project-autonomy-design.md`
- Modify: `docs/superpowers/plans/2026-07-21-continuous-project-autonomy.md`

- [ ] Run `pytest -q` and `git diff --check`.
- [ ] Review the current diff for lifecycle, safety-boundary, recovery, and migration regressions.
- [ ] Commit the implementation with `git commit -m "feat: continue project delivery after MVP"`.
- [ ] Restart the local kernel, queue Same Ground and The Dark Index continuation scopes, and confirm each project is actively delivering or explicitly waiting on an external boundary.
