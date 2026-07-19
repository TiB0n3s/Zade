"""Tests for the founder_brief → Anthropic strategic-review consumer.

Pins the first cloud egress: the client refuses unless configured + policy-clear,
the brief is HELD for founder approval (nothing sent), approval re-checks the
egress gate before sending, a policy that dropped to local_only blocks the send
even after approval, and the returned review is filed through the governed path.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cofounder_kernel.anthropic_client import (
    AnthropicClient,
    AnthropicNotConfigured,
    AnthropicPolicyError,
)
from cofounder_kernel.config import AnthropicConfig, AppConfig, KernelConfig, OllamaConfig, PathConfig, ensure_local_paths
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.founder import FounderService
from cofounder_kernel.ingestion import IngestionService
from cofounder_kernel.strategy_review import StrategyReviewService

PHRASE = "make the jump to hyperspace"


class FakeEmbedder:
    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        return []


class FakeAnthropic:
    """Stands in for the real client — records what it was asked to send."""

    def __init__(self, reply: str = "The weakest assumption is retention; the key move is a pilot."):
        self.reply = reply
        self.sent: list[str] = []

    def review(self, *, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        self.sent.append(prompt)
        return self.reply


class _FakeSDKMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="here is the review")]
        )


class _FakeSDK:
    def __init__(self):
        self.messages = _FakeSDKMessages()


# --------------------------------------------------------------------------
# AnthropicClient — the transport
# --------------------------------------------------------------------------
def test_client_refuses_when_disabled() -> None:
    client = AnthropicClient(AnthropicConfig(enabled=False), provider_policy="local_preferred")
    with pytest.raises(AnthropicNotConfigured):
        client.review(prompt="hi")


def test_client_refuses_under_local_only() -> None:
    client = AnthropicClient(AnthropicConfig(enabled=True), provider_policy="local_only")
    with pytest.raises(AnthropicPolicyError):
        client.review(prompt="hi")


def test_client_refuses_without_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicClient(AnthropicConfig(enabled=True), provider_policy="local_preferred")
    with pytest.raises(AnthropicNotConfigured):
        client.review(prompt="hi")


def test_client_refuses_tampered_endpoint_before_sdk_construction(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    factory_calls: list[dict] = []
    client = AnthropicClient(
        AnthropicConfig(enabled=True, base_url="https://example.com/v1/messages"),
        provider_policy="local_preferred",
        sdk_factory=lambda **kwargs: factory_calls.append(kwargs),
    )

    with pytest.raises(AnthropicPolicyError, match="expected https://api.anthropic.com"):
        client.review(prompt="hi")

    assert factory_calls == []


def test_client_sends_and_parses(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake = _FakeSDK()
    factory_calls: list[dict] = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return fake

    client = AnthropicClient(
        AnthropicConfig(enabled=True, model="claude-opus-4-8"),
        provider_policy="local_preferred",
        sdk_factory=factory,
    )
    out = client.review(prompt="assess this", system="be direct")

    assert out == "here is the review"
    assert factory_calls[0]["api_key"] == "sk-ant-test"
    assert factory_calls[0]["base_url"] == "https://api.anthropic.com"
    assert factory_calls[0]["max_retries"] == 0
    body = fake.messages.calls[0]
    assert body["model"] == "claude-opus-4-8"
    assert body["messages"][0]["content"] == "assess this"
    assert body["system"] == "be direct"


# --------------------------------------------------------------------------
# StrategyReviewService — the gated consumer
# --------------------------------------------------------------------------
def _service(tmp_path: Path, *, provider_policy: str = "local_preferred", enabled: bool = True, anthropic=None):
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy=provider_policy),
        anthropic=AnthropicConfig(enabled=enabled),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    founder = FounderService(config=config, db=db)
    ingestion = IngestionService(config=config, db=db, embedder=FakeEmbedder())
    service = StrategyReviewService(
        config=config, db=db, founder=founder, ingestion=ingestion, anthropic=anthropic or FakeAnthropic()
    )
    return service, db, config


def _pending(db: KernelDatabase) -> list:
    return [r for r in db.list_approval_requests(status="pending", limit=100) if r.source_type == "strategy_review"]


def test_request_holds_brief_for_approval_and_sends_nothing(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    service, db, _ = _service(tmp_path, anthropic=fake)
    result = service.request_review(focus="fundraising", question="Are we ready to raise?")
    assert result["status"] == "awaiting_approval"
    assert result["approval_request_id"]
    # the exact text to be sent is previewed, and it carries the founder's focus
    assert "founder brief" in result["preview"].lower()
    assert "fundraising" in result["preview"]
    # nothing was sent, and a pending review exists
    assert fake.sent == []
    assert len(_pending(db)) == 1


def test_request_under_local_only_is_refused_without_filing(tmp_path: Path) -> None:
    service, db, _ = _service(tmp_path, provider_policy="local_only")
    result = service.request_review(question="thoughts?")
    assert result["status"] == "denied"
    assert result["matched_rule"] == "policy.local_only"
    assert _pending(db) == []  # nothing queued


def test_approve_requires_typed_phrase(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    service, db, _ = _service(tmp_path, anthropic=fake)
    rid = service.request_review(question="q")["approval_request_id"]
    with pytest.raises(ValueError):
        service.approve(rid, typed_phrase="please")
    assert fake.sent == []  # still not sent


def test_approve_sends_and_files_the_review(tmp_path: Path) -> None:
    fake = FakeAnthropic(reply="Focus on retention before raising.")
    service, db, _ = _service(tmp_path, anthropic=fake)
    rid = service.request_review(focus="fundraising", question="ready to raise?")["approval_request_id"]

    result = service.approve(rid, typed_phrase=PHRASE)
    assert result["status"] == "completed"
    assert result["review"] == "Focus on retention before raising."
    # the brief was actually sent (the curated founder brief text)
    assert fake.sent and "founder brief" in fake.sent[0].lower()
    # the review is filed as governed memory, attributed to Anthropic
    matches = db.search_memories("retention", 5)
    assert matches and matches[0].source == "anthropic:strategic-review"
    # the request is resolved and no longer pending
    assert _pending(db) == []
    assert any(e["action"] == "strategy.review.completed" for e in db.recent_audit_events(15))


def test_approval_blocked_if_policy_dropped_to_local_only(tmp_path: Path) -> None:
    """Defense in depth: the gate is re-checked at execution. If the founder
    lowered provider_policy after requesting, the send is blocked even with the
    phrase."""
    fake = FakeAnthropic()
    service, db, config = _service(tmp_path, anthropic=fake)
    rid = service.request_review(question="q")["approval_request_id"]
    # rebuild the service over the SAME db but with local_only — simulating the
    # founder lowering the policy between request and approval
    locked = StrategyReviewService(
        config=KernelConfig(paths=config.paths, ollama=OllamaConfig(provider_policy="local_only"),
                            anthropic=AnthropicConfig(enabled=True)),
        db=db, founder=FounderService(config=config, db=db),
        ingestion=IngestionService(config=config, db=db, embedder=FakeEmbedder()), anthropic=fake,
    )
    result = locked.approve(rid, typed_phrase=PHRASE)
    assert result["status"] == "blocked"
    assert result["matched_rule"] == "policy.local_only"
    assert fake.sent == []  # never sent
    assert _pending(db) == []  # resolved (denied at execute)


def test_deny_sends_nothing(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    service, db, _ = _service(tmp_path, anthropic=fake)
    rid = service.request_review(question="q")["approval_request_id"]
    assert service.deny(rid)["status"] == "denied"
    assert fake.sent == []
    assert _pending(db) == []


# --------------------------------------------------------------------------
# readiness() — the /ops/providers cloud-readiness readout
# --------------------------------------------------------------------------
def _real_service(tmp_path: Path, *, provider_policy: str = "local_preferred", enabled: bool = True):
    """A service backed by a REAL AnthropicClient so readiness() reads live
    config/env (the FakeAnthropic stand-in has no provider_info)."""
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy=provider_policy),
        anthropic=AnthropicConfig(enabled=enabled),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    founder = FounderService(config=config, db=db)
    ingestion = IngestionService(config=config, db=db, embedder=FakeEmbedder())
    service = StrategyReviewService(config=config, db=db, founder=founder, ingestion=ingestion)
    return service, db, config


def test_readiness_lists_blockers_when_cloud_is_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    service, _, _ = _real_service(tmp_path, provider_policy="local_preferred", enabled=False)

    r = service.readiness()

    assert r["ready"] is False
    assert r["enabled"] is False
    assert r["key_present"] is False
    # the unmet gates are named, without leaking any secret
    assert any("enabled = false" in b for b in r["blockers"])
    assert any("ANTHROPIC_API_KEY" in b for b in r["blockers"])
    # a send always needs a per-request grant on top of the standing gates
    assert r["requires_per_request_grant"] is True


def test_readiness_true_when_all_standing_gates_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    service, _, _ = _real_service(tmp_path, provider_policy="local_preferred", enabled=True)

    r = service.readiness()

    assert r["ready"] is True
    assert r["blockers"] == []
    assert r["enabled"] is True
    assert r["key_present"] is True
    assert r["policy_allows_cloud"] is True
    assert r["egress_cell"] == "per_request"


def test_readiness_reads_matrix_cell_directly_under_local_only(tmp_path: Path, monkeypatch) -> None:
    """Under local_only the gate short-circuits before consulting the matrix; the
    readout must still show the cell's true disposition (per_request), flagging
    only the policy as the blocker — not a phantom 'forbidden' cell."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    service, _, _ = _real_service(tmp_path, provider_policy="local_only", enabled=True)

    r = service.readiness()

    assert r["ready"] is False
    assert r["policy_allows_cloud"] is False
    assert any("local_only" in b for b in r["blockers"])
    # the cell itself is not forbidden — direct inspection proves it
    assert r["egress_cell"] == "per_request"
    assert r["egress_cell_ok"] is True
