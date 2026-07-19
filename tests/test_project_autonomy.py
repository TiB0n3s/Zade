import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cofounder_kernel.db import KernelDatabase, utc_now
from cofounder_kernel.project_autonomy import (
    ProjectAutonomyReporter,
    autonomy_projection,
    portfolio_status,
)


class FakeBus:
    """Records notifications; never contacts a real channel or Telegram."""

    def __init__(self):
        self.calls = []
        self.next_id = 100

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        self.next_id += 1
        return {"id": self.next_id, "status": "delivered"}

    def topic_calls(self, topic):
        return [call for call in self.calls if call["topic"] == topic]


def make_reporter(tmp_path: Path, *, bus: FakeBus | None = None):
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return ProjectAutonomyReporter(db=db, bus=bus), db


def make_project(db: KernelDatabase, tmp_path: Path, *, name="Same Ground", state="verified") -> int:
    root = tmp_path / "intake" / name
    root.mkdir(parents=True, exist_ok=True)
    return db.upsert_project(
        canonical_path=str(root),
        name=name,
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state=state,
        repo_fingerprint="fp",
        metadata={},
    )


def fresh_verification(checks=1) -> dict:
    return {
        "ok": True,
        "checked_at": utc_now(),
        "checks": [
            {"argv": ["npm", "test"], "ok": True, "returncode": 0, "output": "42 passed"}
            for _ in range(checks)
        ],
    }


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=test@test", "-c", "user.name=test", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_git_project(db: KernelDatabase, tmp_path: Path, *, name="Same Ground") -> tuple[int, Path, str]:
    project_id = make_project(db, tmp_path, name=name)
    root = tmp_path / "intake" / name
    (root / "app.py").write_text("print('mvp')\n", encoding="utf-8")
    _git(root, "init", "--initial-branch=main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "mvp implementation")
    head = _git(root, "rev-parse", "HEAD")
    return project_id, root, head


CRITERIA = [
    {"id": "auth", "title": "Local account sign-in works"},
    {"id": "feed", "title": "Feed renders real data"},
]


# ---- state transitions -------------------------------------------------------


def test_plan_and_increment_transitions(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id = make_project(db, tmp_path)

    planned = reporter.plan(project_id, criteria=CRITERIA, priority="high", next_action="start auth")
    assert autonomy_projection(planned)["phase"] == "planning"
    assert autonomy_projection(planned)["priority"] == "high"
    assert autonomy_projection(planned)["mvp_criteria_total"] == 2
    assert autonomy_projection(planned)["mvp_criteria_completed"] == 0

    building = reporter.begin_increment(project_id, criterion_id="auth", run_id=7)
    view = autonomy_projection(building)
    assert view["phase"] == "building"
    assert view["current_criterion_id"] == "auth"
    assert view["current_increment"] == 1
    assert view["active_run_id"] == 7

    verifying = reporter.begin_verification(project_id)
    assert autonomy_projection(verifying)["phase"] == "verifying"

    ready = reporter.record_increment(project_id, summary="auth screen wired", verification=fresh_verification())
    view = autonomy_projection(ready)
    assert view["phase"] == "ready_for_next_increment"
    assert view["active_run_id"] is None
    assert view["last_verified_at"]

    events = [row["event_type"] for row in db.list_project_events(project_id)]
    assert events[:4] == [
        "increment_completed",
        "verification_started",
        "increment_started",
        "autonomy_planned",
    ]


def test_plan_rejects_empty_or_duplicate_criteria(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id = make_project(db, tmp_path)

    with pytest.raises(ValueError, match="at least one"):
        reporter.plan(project_id, criteria=[])
    with pytest.raises(ValueError, match="Duplicate"):
        reporter.plan(
            project_id,
            criteria=[{"id": "a", "title": "one"}, {"id": "a", "title": "two"}],
        )


def test_criterion_completion_requires_mechanical_evidence(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)

    with pytest.raises(ValueError, match="did not pass"):
        reporter.complete_criterion(project_id, "auth", verification={"ok": False}, commit="abc")
    with pytest.raises(ValueError, match="prose is not evidence"):
        reporter.complete_criterion(
            project_id,
            "auth",
            verification={"ok": True, "checked_at": utc_now(), "checks": []},
            commit="abc",
        )
    with pytest.raises(ValueError, match="without the verified repository commit"):
        reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit="")
    stale = fresh_verification()
    stale["checked_at"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    with pytest.raises(ValueError, match="stale"):
        reporter.complete_criterion(project_id, "auth", verification=stale, commit="abc")

    done = reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit="abc123")
    view = autonomy_projection(done)
    assert view["mvp_criteria_completed"] == 1
    assert view["phase"] == "ready_for_next_increment"
    assert view["repo_head"] == "abc123"
    assert view["mvp_complete"] is False


# ---- scaffold verified is never MVP complete ---------------------------------


def test_scaffold_verified_projection_is_not_mvp_complete(tmp_path: Path) -> None:
    _reporter, db = make_reporter(tmp_path)
    project_id = make_project(db, tmp_path, state="verified")
    project = db.get_project(project_id)

    view = autonomy_projection(project)

    assert view["phase"] == "ready_for_next_increment"
    assert view["mvp_complete"] is False
    assert view["mvp_criteria_total"] == 0
    status = portfolio_status([project])
    assert status["totals"]["scaffold_verified"] == 1
    assert status["totals"]["mvp_complete"] == 0


def test_mvp_completion_rejected_for_scaffold_verified_project_without_plan(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id, _root, _head = make_git_project(db, tmp_path)

    with pytest.raises(ValueError, match="scaffold verification alone never completes an MVP"):
        reporter.complete_mvp(project_id, final_verification=fresh_verification())


# ---- the MVP completion gate -------------------------------------------------


def test_mvp_completion_rejected_with_incomplete_criteria(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id, _root, head = make_git_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit=head)

    with pytest.raises(ValueError, match="'feed' is not complete"):
        reporter.complete_mvp(project_id, final_verification=fresh_verification())


def test_mvp_completion_rejected_without_final_verification_or_clean_git(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path)
    project_id, root, head = make_git_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    for criterion in ("auth", "feed"):
        reporter.complete_criterion(project_id, criterion, verification=fresh_verification(), commit=head)

    with pytest.raises(ValueError, match="prose is not evidence"):
        reporter.complete_mvp(project_id, final_verification={"ok": True, "summary": "all good, trust me"})

    (root / "untracked.tmp").write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="not clean"):
        reporter.complete_mvp(project_id, final_verification=fresh_verification())


def test_mvp_completion_rejected_when_required_criterion_blocked(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id, _root, head = make_git_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit=head)
    reporter.report_blocked(
        project_id,
        criterion_id="feed",
        reason="feed test fails on pagination",
        verification_output="expected 20 items, got 0",
        attempts=3,
        needed="fix pagination cursor",
    )

    with pytest.raises(ValueError, match="'feed' is blocked"):
        reporter.complete_mvp(project_id, final_verification=fresh_verification())


def test_valid_mvp_completion_emits_exactly_one_notification(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id, root, head = make_git_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA, external_boundaries=["Google Play submission"])
    for criterion in ("auth", "feed"):
        reporter.complete_criterion(project_id, criterion, verification=fresh_verification(), commit=head)

    completed = reporter.complete_mvp(project_id, final_verification=fresh_verification(checks=3))
    again = reporter.complete_mvp(project_id, final_verification=fresh_verification(checks=3))

    view = autonomy_projection(completed)
    assert view["phase"] == "mvp_complete"
    assert view["mvp_complete"] is True
    assert view["repo_head"] == head
    calls = bus.topic_calls("project.mvp_complete")
    assert len(calls) == 1
    call = calls[0]
    assert call["severity"] == "info"
    assert call["dedupe_key"] == f"project:{project_id}:mvp:{head}"
    assert "Same Ground" in call["title"]
    assert "MVP criteria complete: 2/2" in call["body"]
    assert "3 checks passed" in call["body"]
    assert str(root.resolve()) in call["body"]
    assert head in call["body"]
    assert "Google Play submission" in call["body"]
    assert call.get("force_channels") == ("telegram",)
    assert autonomy_projection(again)["mvp_complete"] is True
    events = [row["event_type"] for row in db.list_project_events(project_id)]
    assert events.count("mvp_completed") == 1


def test_mutations_rejected_after_mvp_complete(tmp_path: Path) -> None:
    reporter, db = make_reporter(tmp_path, bus=FakeBus())
    project_id, _root, head = make_git_project(db, tmp_path)
    reporter.plan(project_id, criteria=[{"id": "auth", "title": "Sign-in works"}])
    reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit=head)
    reporter.complete_mvp(project_id, final_verification=fresh_verification())

    with pytest.raises(ValueError, match="already mvp_complete"):
        reporter.begin_increment(project_id, criterion_id="auth")


# ---- founder boundary notifications ------------------------------------------


def test_needs_decision_notification_content_and_dedupe(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    reporter.begin_increment(project_id, criterion_id="auth")

    options = [
        {"option": "SQLite", "impact": "zero-dependency, single-device only"},
        {"option": "Realm", "impact": "sync-ready, adds a native dependency"},
    ]
    updated = reporter.report_needs_decision(
        project_id,
        decision_id=91,
        question="Which local database should the app use?",
        recommendation="SQLite: it matches the offline-first MVP scope.",
        options=options,
    )

    view = autonomy_projection(updated)
    assert view["phase"] == "needs_decision"
    assert view["blocking_type"] == "decision"
    assert view["decision_id"] == 91
    assert view["last_notification_id"] == 101
    call = bus.topic_calls("project.decision_required")[0]
    assert call["severity"] == "warning"
    assert call["dedupe_key"] == f"project:{project_id}:decision:91"
    assert "Same Ground" in call["title"]
    assert "Which local database should the app use?" in call["body"]
    assert "Recommendation: SQLite" in call["body"]
    assert "1. SQLite — impact: zero-dependency, single-device only" in call["body"]
    assert "2. Realm — impact: sync-ready, adds a native dependency" in call["body"]
    assert "Reply exactly: decision 91: <your answer>" in call["body"]

    with pytest.raises(ValueError, match="2-3 concrete options"):
        reporter.report_needs_decision(
            project_id,
            decision_id=92,
            question="q?",
            recommendation="r",
            options=[{"option": "only one", "impact": "x"}],
        )


def test_approval_required_notification_content_and_boundary(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)

    updated = reporter.report_approval_required(
        project_id,
        approval_request_id=55,
        action="Create a Google Play developer account",
        reason="External account creation is an authority boundary.",
        boundary="external_account_creation",
    )

    view = autonomy_projection(updated)
    assert view["phase"] == "approval_required"
    assert view["blocking_type"] == "approval"
    assert view["approval_request_id"] == 55
    call = bus.topic_calls("project.approval_required")[0]
    assert call["severity"] == "warning"
    assert call["dedupe_key"] == f"project:{project_id}:approval:55"
    assert "Same Ground" in call["title"]
    assert "Proposed action: Create a Google Play developer account" in call["body"]
    assert "Why approval is required:" in call["body"]
    assert "Authority boundary: external_account_creation" in call["body"]
    assert "Approval request: 55" in call["body"]
    assert "/work/items/55/approve" in call["body"]
    assert "/work/items/55/deny" in call["body"]

    with pytest.raises(ValueError, match="Authority boundary"):
        reporter.report_approval_required(
            project_id,
            approval_request_id=56,
            action="pick a CSS framework",
            reason="not a real boundary",
            boundary="reversible_local_choice",
        )


def test_blocked_notification_carries_real_failure_detail(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)

    updated = reporter.report_blocked(
        project_id,
        criterion_id="feed",
        reason="flutter test fails after 3 repair attempts",
        verification_output="Expected: 20 items. Actual: 0. Null cursor in FeedRepository.",
        attempts=3,
        needed="founder guidance on the data source contract",
        severity="critical",
    )

    view = autonomy_projection(updated)
    assert view["phase"] == "blocked"
    assert view["blocking_type"] == "error"
    call = bus.topic_calls("project.build_blocked")[0]
    assert call["severity"] == "critical"
    assert "Failed criterion: Feed renders real data" in call["body"]
    assert "Null cursor in FeedRepository" in call["body"]
    assert "Repair attempts: 3" in call["body"]
    assert "founder guidance on the data source contract" in call["body"]

    with pytest.raises(ValueError, match="severity"):
        reporter.report_blocked(project_id, reason="x", severity="info")


def test_routine_increment_records_ledger_but_never_notifies(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    reporter.begin_increment(project_id, criterion_id="auth")

    reporter.record_increment(project_id, summary="login form built", verification=fresh_verification())
    reporter.complete_criterion(project_id, "auth", verification=fresh_verification(), commit="abc123")

    assert bus.calls == []
    events = [row["event_type"] for row in db.list_project_events(project_id)]
    assert "increment_completed" in events
    assert "criterion_completed" in events


# ---- resumption --------------------------------------------------------------


def test_decision_resolution_resumes_correct_project_and_criterion(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    first = make_project(db, tmp_path, name="Same Ground")
    second = make_project(db, tmp_path, name="The Dark Index")
    for project_id, criterion in ((first, "auth"), (second, "feed")):
        reporter.plan(project_id, criteria=CRITERIA)
        reporter.begin_increment(project_id, criterion_id=criterion)
        reporter.report_needs_decision(
            project_id,
            decision_id=project_id * 10,
            question="Which storage engine?",
            recommendation="SQLite",
            options=[
                {"option": "SQLite", "impact": "local"},
                {"option": "Realm", "impact": "sync"},
            ],
        )

    resumed = reporter.resume_after_decision(second * 10, answer="Use SQLite")

    assert resumed["project"]["id"] == second
    assert resumed["criterion_id"] == "feed"
    view = autonomy_projection(resumed["project"])
    assert view["phase"] == "building"
    assert view["decision_id"] is None
    assert view["blocking_type"] is None
    untouched = autonomy_projection(db.get_project(first))
    assert untouched["phase"] == "needs_decision"
    assert untouched["decision_id"] == first * 10

    with pytest.raises(ValueError, match="No project is waiting"):
        reporter.resume_after_decision(9999)


def test_approval_resolution_resumes_or_blocks(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    project_id = make_project(db, tmp_path)
    reporter.plan(project_id, criteria=CRITERIA)
    reporter.begin_increment(project_id, criterion_id="auth")
    reporter.report_approval_required(
        project_id,
        approval_request_id=55,
        action="publish beta",
        reason="publishing boundary",
        boundary="publishing_deployment",
    )

    resumed = reporter.resume_after_approval(55, approved=True)
    assert autonomy_projection(resumed["project"])["phase"] == "building"
    assert resumed["criterion_id"] == "auth"

    reporter.report_approval_required(
        project_id,
        approval_request_id=56,
        action="publish beta",
        reason="publishing boundary",
        boundary="publishing_deployment",
    )
    denied = reporter.resume_after_approval(56, approved=False, note="not yet")
    view = autonomy_projection(denied["project"])
    assert view["phase"] == "blocked"
    assert view["blocking_reason"] == "not yet"


# ---- portfolio ---------------------------------------------------------------


def test_portfolio_distinguishes_every_status(tmp_path: Path) -> None:
    bus = FakeBus()
    reporter, db = make_reporter(tmp_path, bus=bus)
    scaffold_only = make_project(db, tmp_path, name="Scaffold Only", state="verified")
    building = make_project(db, tmp_path, name="Building")
    deciding = make_project(db, tmp_path, name="Deciding")
    approving = make_project(db, tmp_path, name="Approving")
    blocked = make_project(db, tmp_path, name="Blocked")
    complete_id, _root, head = make_git_project(db, tmp_path, name="Complete")

    reporter.plan(building, criteria=CRITERIA)
    reporter.begin_increment(building, criterion_id="auth")
    reporter.plan(deciding, criteria=CRITERIA)
    reporter.report_needs_decision(
        deciding,
        decision_id=71,
        question="q?",
        recommendation="r",
        options=[{"option": "a", "impact": "x"}, {"option": "b", "impact": "y"}],
    )
    reporter.plan(approving, criteria=CRITERIA)
    reporter.report_approval_required(
        approving,
        approval_request_id=72,
        action="buy service",
        reason="paid",
        boundary="paid_services",
    )
    reporter.plan(blocked, criteria=CRITERIA)
    reporter.report_blocked(blocked, reason="toolchain missing")
    reporter.plan(complete_id, criteria=[{"id": "auth", "title": "Sign-in works"}])
    reporter.complete_criterion(complete_id, "auth", verification=fresh_verification(), commit=head)
    reporter.complete_mvp(complete_id, final_verification=fresh_verification())

    status = portfolio_status(db.list_projects())

    assert status["totals"] == {
        "scaffold_verified": 1,
        "actively_building": 1,
        "waiting_decision": 1,
        "waiting_approval": 1,
        "blocked": 1,
        "mvp_complete": 1,
        "intake": 0,
    }
    by_name = {item["name"]: item["status"] for item in status["projects"]}
    assert by_name["Scaffold Only"] == "scaffold_verified"
    assert by_name["Complete"] == "mvp_complete"
