from __future__ import annotations

from pathlib import Path

from cofounder_kernel.build_routing import (
    BuildContextSelector,
    BuildRouter,
    BuildStep,
    LocalAttempt,
)
from cofounder_kernel.build_types import BuildLease, BuildSession, BuildTier, LeaseLimits


def session() -> BuildSession:
    return BuildSession(
        id=1,
        assessment_id=1,
        work_item_id=None,
        workspace="C:/workspace",
        repo_fingerprint="abc",
        phase="implementation",
        status="active",
        checkpoint={},
        created_at="2026-07-18T12:00:00+00:00",
        updated_at="2026-07-18T12:00:00+00:00",
    )


def lease(*, state: str = "active") -> BuildLease:
    return BuildLease(
        id=1,
        session_id=1,
        version=1,
        tier=BuildTier.MEDIUM,
        provider="anthropic",
        model="claude-opus-4-8",
        limits=LeaseLimits(3_000_000, 400_000, 40_000, 16, 14400),
        state=state,
        approval_request_id=1,
        actual_input_tokens=0,
        actual_output_tokens=0,
        actual_microdollars=0,
        reserved_input_tokens=0,
        reserved_output_tokens=0,
        reserved_microdollars=0,
        cloud_turns=0,
        started_at="2026-07-18T12:00:00+00:00",
        expires_at="2099-07-18T16:00:00+00:00",
    )


def router(active_lease: BuildLease | None = None, *, enabled: bool = True) -> BuildRouter:
    return BuildRouter(
        lease_lookup=lambda _session_id: active_lease,
        cloud_enabled=enabled,
        pricing_current=lambda: True,
    )


def test_small_routine_edit_stays_local_even_with_lease() -> None:
    decision = router(lease()).route_step(
        session(), BuildStep(kind="edit", risk="low"), []
    )

    assert decision.route == "local"
    assert decision.reasons == ("routine_work_stays_local",)


def test_two_distinct_local_failures_make_debugging_cloud_eligible() -> None:
    attempts = [
        LocalAttempt("pytest failure A"),
        LocalAttempt("different fix; failure B"),
    ]

    decision = router(lease()).route_step(
        session(), BuildStep(kind="debug", risk="high"), attempts
    )

    assert decision.route == "cloud"
    assert decision.reasons == ("difficult_debugging_after_two_local_attempts",)


def test_repeated_same_local_action_does_not_qualify_for_cloud() -> None:
    attempts = [LocalAttempt("Run pytest"), LocalAttempt("  run   PYTEST ")]

    decision = router(lease()).route_step(
        session(), BuildStep(kind="debug", risk="high"), attempts
    )

    assert decision.route == "local"


def test_eligible_work_without_lease_routes_to_founder_not_provider() -> None:
    decision = router(None).route_step(
        session(),
        BuildStep(kind="architecture", risk="high", cross_module=True),
        [],
    )

    assert decision.route == "founder"
    assert decision.blockers == ("no_active_lease",)


def test_cloud_disabled_policy_never_selects_provider() -> None:
    decision = router(lease(), enabled=False).route_step(
        session(),
        BuildStep(kind="review", risk="high", critical_domains=("billing",)),
        [],
    )

    assert decision.route == "founder"
    assert "cloud_disabled" in decision.blockers


def test_context_excludes_secrets_dependencies_and_unrelated_history(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    auth = tmp_path / "src" / "auth.py"
    auth.write_text("def callback():\n    return 'ok'\n", encoding="utf-8")
    leaky = tmp_path / "src" / "settings.py"
    leaky.write_text(
        "API_KEY = 'sk-should-never-leave-this-machine'\n", encoding="utf-8"
    )
    env = tmp_path / ".env"
    env.write_text("SECRET=sentinel\n", encoding="utf-8")
    vendor = tmp_path / "node_modules" / "vendor.js"
    vendor.parent.mkdir()
    vendor.write_text("vendor\n", encoding="utf-8")
    history = tmp_path / "conversations" / "old.txt"
    history.parent.mkdir()
    history.write_text("old chat turn\n", encoding="utf-8")

    selected = BuildContextSelector(tmp_path).select(
        task="fix auth callback",
        candidates=[auth, leaky, env, vendor, history],
    )

    assert selected.paths == ("src/auth.py",)
    assert selected.total_bytes <= 48_000
    assert selected.total_chars <= 48_000
    assert "sentinel" not in "".join(excerpt.content for excerpt in selected.excerpts)
    assert selected.excerpts[0].start_line == 1
    assert selected.excerpts[0].content_hash


def test_context_ranking_and_utf8_byte_limit_are_deterministic(tmp_path: Path) -> None:
    auth = tmp_path / "auth.py"
    auth.write_text("auth callback\n" + ("x" * 30_000), encoding="utf-8")
    other = tmp_path / "other.py"
    other.write_text("y" * 30_000, encoding="utf-8")
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[project]\nname='demo'\n", encoding="utf-8")

    selected = BuildContextSelector(tmp_path, max_bytes=20_000).select(
        task="fix auth callback",
        candidates=[other, manifest, auth],
        changed_files=[auth],
    )

    assert selected.paths[0] == "auth.py"
    assert selected.total_bytes <= 20_000
    assert any(excerpt.truncated for excerpt in selected.excerpts)
    assert sum(len(item.content.encode("utf-8")) for item in selected.excerpts) == selected.total_bytes


def test_context_rejects_paths_outside_workspace_and_binary_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("private", encoding="utf-8")
    binary = tmp_path / "image.png"
    binary.write_bytes(b"\x89PNG\r\n")

    selected = BuildContextSelector(tmp_path).select(
        task="inspect files", candidates=[outside, binary]
    )

    assert selected.paths == ()
    assert selected.excerpts == ()
