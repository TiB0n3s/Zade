"""Tests for the hallucination-grounding discipline.

Three rails, all deterministic:
1. Recall hits are rendered with citable tags ([M<id>] memory, [S<id>] semantic)
   and the governed prompt carries the grounding-discipline rules (cite what you
   recall, mark inference as your read, "not on file" is a complete answer).
2. The regulator audits citation tags in the draft against the recall actually
   injected this turn: invented tags are stripped and flagged; valid ones are
   recorded as verified.
3. The investigation loop's prompt requires the final answer to be grounded in
   this turn's tool results — gaps are named, never filled.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.authority import AuthorityRequest
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.runtime import _audit_citations, _brief_hits, _valid_citation_tags


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    return client.app.state.runtime


_MEMORY_HITS = [
    {"id": 12, "kind": "decision", "title": "Pricing decision", "content": "Keep annual pricing at launch."},
]
_SEMANTIC_HITS = [
    {"document_id": 4, "document_title": "Q2 audit notes", "text": "Churn concentrated in monthly plans."},
]


def _grounded_context(runtime, message: str) -> dict:
    context = runtime.context(
        message=message,
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )
    context["memory_hits"] = list(_MEMORY_HITS)
    context["semantic_hits"] = list(_SEMANTIC_HITS)
    context["evidence_state"]["local_evidence_present"] = True
    return context


def _authority(runtime):
    return runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )


# ---------------------------------------------------------------- rendering


def test_brief_hits_renders_citable_tags() -> None:
    rendered = _brief_hits(
        _MEMORY_HITS, "id", "title", "content", empty="none", tag="M", note_key="kind"
    )
    assert "- [M12] (decision) Pricing decision: Keep annual pricing at launch." == rendered
    semantic = _brief_hits(
        _SEMANTIC_HITS, "document_id", "document_title", "text", empty="none", tag="S"
    )
    assert semantic.startswith("- [S4] Q2 audit notes:")


def test_valid_citation_tags_mirror_injected_recall() -> None:
    tags = _valid_citation_tags({"memory_hits": _MEMORY_HITS, "semantic_hits": _SEMANTIC_HITS})
    assert tags == {"M12", "S4"}
    # Only the top-5 rendered hits are citable — the audit mirrors the prompt cut.
    many = [{"id": index, "title": "t", "content": "c"} for index in range(1, 9)]
    assert _valid_citation_tags({"memory_hits": many, "semantic_hits": []}) == {
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
    }


def test_governed_prompt_carries_grounding_discipline_and_tagged_recall(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    message = "What did we decide about the pricing page?"
    context = _grounded_context(runtime, message)
    prompt = runtime._build_governed_prompt(
        message=message,
        context=context,
        authority=_authority(runtime),
        conversation_block="",
    )
    assert "Grounding discipline" in prompt
    assert "[M12] (decision) Pricing decision" in prompt
    assert "[S4] Q2 audit notes" in prompt
    assert "I don't have that on file" in prompt
    assert "cite its tag inline" in prompt


# ---------------------------------------------------------------- the audit


def test_audit_citations_strips_only_invented_tags() -> None:
    audited = _audit_citations(
        "We kept annual pricing [M12]. Support load doubled [M99] and churn moved [S4].",
        {"M12", "S4"},
    )
    assert audited["cited"] == ["M12", "S4"]
    assert audited["fabricated"] == ["M99"]
    assert "[M99]" not in audited["text"]
    assert "[M12]" in audited["text"] and "[S4]" in audited["text"]
    assert "Support load doubled and churn moved" in audited["text"]


def test_audit_citations_ignores_plain_brackets() -> None:
    text = "The log line was [truncated] and the diff shows [MERGE] markers."
    audited = _audit_citations(text, set())
    assert audited == {"text": text, "cited": [], "fabricated": []}


def test_regulator_strips_fabricated_citation_and_flags_it(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    message = "What did we decide about the pricing page?"
    context = _grounded_context(runtime, message)
    regulated = runtime._regulate_response(
        "We decided to keep annual pricing [M12]. The audit also flagged onboarding drop-off [S9].",
        message=message,
        recent_turns=[],
        authority=_authority(runtime),
        context=context,
    )
    assert "[S9]" not in regulated["response"]
    assert "[M12]" in regulated["response"]
    assert "fabricated_citation_stripped" in regulated["governor"]["applied_rules"]
    audit = regulated["governor"]["citation_audit"]
    assert audit["fabricated_stripped"] == ["S9"]
    assert audit["cited"] == ["M12"]
    assert any("S9" in note for note in regulated["governor"]["notes"])


def test_regulator_marks_fully_supported_citations_verified(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    message = "What did we decide about the pricing page?"
    context = _grounded_context(runtime, message)
    regulated = runtime._regulate_response(
        "We kept annual pricing at launch [M12]; churn sits in monthly plans [S4].",
        message=message,
        recent_turns=[],
        authority=_authority(runtime),
        context=context,
    )
    assert "citations_verified" in regulated["governor"]["applied_rules"]
    assert regulated["governor"]["citation_audit"]["fabricated_stripped"] == []
    assert regulated["governor"]["citation_audit"]["cited"] == ["M12", "S4"]


def test_regulator_without_citations_reports_empty_audit(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    message = "What did we decide about the pricing page?"
    context = _grounded_context(runtime, message)
    regulated = runtime._regulate_response(
        "We kept annual pricing at launch; churn sits in monthly plans.",
        message=message,
        recent_turns=[],
        authority=_authority(runtime),
        context=context,
    )
    governor = regulated["governor"]
    assert governor["citation_audit"] == {"cited": [], "fabricated_stripped": []}
    assert "citations_verified" not in governor["applied_rules"]
    assert "fabricated_citation_stripped" not in governor["applied_rules"]


# ------------------------------------------------------- investigation loop


def test_investigation_prompt_requires_tool_result_grounding(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    block = runtime.investigation.prompt_block()
    assert "Ground the final answer in this turn's tool results" in block
    assert "say the read did not return it" in block
    assert "do not fill the gap" in block
