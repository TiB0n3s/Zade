from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cofounder_kernel.prompts import (
    DEFAULT_PROFILE_ID,
    PERSONA_PROFILE_IDS,
    PROFILE_IDS,
    PromptProfileError,
    PromptProfileRegistry,
    PromptRuntimeBindings,
    resolve_supported_placeholders,
    validate_no_supported_placeholders,
    validate_tool_compatibility,
)


def fixed_bindings(tmp_path: Path) -> PromptRuntimeBindings:
    return PromptRuntimeBindings(
        zade_home=tmp_path / "zade-home",
        skills_root=tmp_path / "skills",
        now=datetime(2026, 7, 15, 8, 30, tzinfo=ZoneInfo("America/Chicago")),
    )


def test_every_profile_loads_and_general_is_default(tmp_path: Path) -> None:
    registry = PromptProfileRegistry()

    assert registry.resolve_profile_id(None, configured_default=None) == DEFAULT_PROFILE_ID
    assert [item["id"] for item in registry.list_profiles()] == list(PROFILE_IDS)

    for profile_id in PROFILE_IDS:
        rendered = registry.render_profile(profile_id, bindings=fixed_bindings(tmp_path))
        assert rendered.profile_id == profile_id
        assert rendered.source_file.endswith(".md")
        assert rendered.content.strip()


def test_persona_profiles_include_baseline_and_exclude_other_personas(tmp_path: Path) -> None:
    registry = PromptProfileRegistry()
    selected = registry.render_profile("study-mentor", bindings=fixed_bindings(tmp_path)).content

    assert "## Shared Baseline" in selected
    assert "# Study Mentor" in selected
    for profile_id in PERSONA_PROFILE_IDS:
        heading = registry.profile(profile_id).source_heading
        assert heading
        if profile_id == "study-mentor":
            continue
        assert f"# {heading}" not in selected


def test_build_profile_includes_autonomous_execution_policy(tmp_path: Path) -> None:
    registry = PromptProfileRegistry()
    rendered = registry.render_profile("build", bindings=fixed_bindings(tmp_path)).content

    assert "AUTONOMOUS EXECUTION POLICY" in rendered
    assert "Do not ask whether to proceed. Begin execution immediately." in rendered
    assert "Creating local Git branches, commits, diffs, and patches." in rendered
    assert "System-enforced approval prompts take precedence." in rendered


def test_supported_placeholders_resolve_exact_tokens_without_corrupting_json(tmp_path: Path) -> None:
    bindings = fixed_bindings(tmp_path)
    source = (
        'Paths: {ZADE_HOME} | {SKILLS_ROOT} | {CURRENT_TIME} | {CURRENTDATE}\n'
        'Schema example: {"type": "object", "properties": {"value": {"type": "string"}}}'
    )

    resolved = resolve_supported_placeholders(source, bindings=bindings)

    assert str(tmp_path / "zade-home") in resolved
    assert str(tmp_path / "skills") in resolved
    assert "2026-07-15" in resolved
    assert '{"type": "object", "properties": {"value": {"type": "string"}}}' in resolved
    validate_no_supported_placeholders(resolved)
    with pytest.raises(PromptProfileError, match="Unresolved supported placeholder"):
        validate_no_supported_placeholders("still malformed: {CURRENT_TIME}")


def test_active_prompts_strip_source_tool_advertisements_and_legacy_branding(tmp_path: Path) -> None:
    registry = PromptProfileRegistry()
    forbidden = (
        "remote sandbox",
        "web_search",
        "x_keyword_search",
        "browse_page",
        "generate_image",
        "todo_write",
        "run_terminal_command",
        "grok",
        "xai",
        "elon musk",
    )

    for profile_id in PROFILE_IDS:
        rendered = registry.render_profile(profile_id, bindings=fixed_bindings(tmp_path))
        lowered = rendered.content.lower()
        for term in forbidden:
            assert term not in lowered, f"{profile_id} advertised {term}"


def test_tool_compatibility_validation_rejects_unavailable_tool_claims() -> None:
    with pytest.raises(PromptProfileError, match="advertises unavailable local capability terms"):
        validate_tool_compatibility("Use web_search before answering.", profile_id="general")


def test_unknown_profile_fails_with_valid_profile_list() -> None:
    registry = PromptProfileRegistry()

    with pytest.raises(PromptProfileError) as excinfo:
        registry.resolve_profile_id("unknown", configured_default=None)

    message = str(excinfo.value)
    assert "Unknown Zade prompt profile 'unknown'" in message
    assert "general" in message
    assert "study-mentor" in message
