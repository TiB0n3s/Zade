from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cofounder_kernel.build_budget import BuildBudgetExceeded, BuildBudgetService
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import BuildAssessment, BuildTier, LeaseLimits
from cofounder_kernel.config import OpenAIReviewConfig
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.openai_review import OpenAIReviewClient, OpenAIReviewUnavailable


class FakeResponses:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(
            output_text='{"summary":"Solid","findings":["Add a rollback test"],"recommendation":"revise"}',
            usage=SimpleNamespace(
                input_tokens=900,
                output_tokens=120,
                input_tokens_details=SimpleNamespace(cached_tokens=300),
            ),
            _request_id="req-openai-1",
        )


class FakeSDKClient:
    def __init__(self, responses: FakeResponses):
        self.responses = responses


def make_runtime(tmp_path: Path, *, lease_provider: str = "openai"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(
        BuildAssessment(
            id=None,
            task="Build checkout",
            acceptance="Security tests pass",
            workspace=str(workspace),
            repo_fingerprint="fingerprint",
            deterministic_score=45,
            local_adjustment=0,
            final_score=45,
            confidence=0.9,
            recommended_tier=BuildTier.MEDIUM,
            dimensions={},
            floor_rules=(),
            evidence={},
            unknowns=(),
            local_work=(),
            cloud_reasons=("Independent review",),
            created_at="2026-07-19T12:00:00+00:00",
        )
    )
    config = OpenAIReviewConfig(enabled=True, max_output_tokens=1000)
    pricing = config.pricing.snapshot()
    store.create_lease(
        session.id,
        BuildTier.SMALL,
        LeaseLimits(1_000_000, 120_000, 16_000, 6, 7200),
        provider=lease_provider,
        model=config.model if lease_provider == "openai" else "claude-opus-4-8",
        approval_request_id=9,
    )
    budget = BuildBudgetService(store, pricing)
    return store, session, config, budget


def test_disabled_missing_key_and_missing_sdk_states_fail_closed(tmp_path: Path) -> None:
    _store, _session, config, budget = make_runtime(tmp_path)

    disabled = OpenAIReviewClient(
        config=OpenAIReviewConfig(enabled=False),
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={"OPENAI_API_KEY": "key"},
        sdk_available=lambda: True,
    ).status()
    missing_key = OpenAIReviewClient(
        config=config,
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={},
        sdk_available=lambda: True,
    ).status()
    missing_sdk = OpenAIReviewClient(
        config=config,
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={"OPENAI_API_KEY": "key"},
        sdk_available=lambda: False,
    ).status()

    assert disabled["blockers"] == ["openai_review_disabled"]
    assert missing_key["blockers"] == ["missing_OPENAI_API_KEY"]
    assert missing_sdk["blockers"] == ["openai_sdk_unavailable"]


def test_only_official_https_api_host_is_accepted(tmp_path: Path) -> None:
    _store, _session, config, budget = make_runtime(tmp_path)
    client = OpenAIReviewClient(
        config=OpenAIReviewConfig(
            enabled=True,
            base_url="http://attacker.invalid/v1",
            max_output_tokens=config.max_output_tokens,
        ),
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={"OPENAI_API_KEY": "key"},
        sdk_available=lambda: True,
    )

    with pytest.raises(OpenAIReviewUnavailable, match="api.openai.com"):
        client.review(session_id=1, prompt="Review", context="diff")


def test_openai_review_requires_openai_specific_lease(tmp_path: Path) -> None:
    _store, session, config, budget = make_runtime(tmp_path, lease_provider="anthropic")
    responses = FakeResponses()
    client = OpenAIReviewClient(
        config=config,
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={"OPENAI_API_KEY": "key"},
        client_factory=lambda **_kwargs: FakeSDKClient(responses),
        sdk_available=lambda: True,
    )

    with pytest.raises(BuildBudgetExceeded, match="approved lease"):
        client.review(session_id=session.id, prompt="Review", context="diff")

    assert responses.calls == []


def test_openai_and_anthropic_leases_can_coexist_with_separate_limits(
    tmp_path: Path,
) -> None:
    store, session, config, _budget = make_runtime(tmp_path, lease_provider="anthropic")
    openai = store.create_lease(
        session.id,
        BuildTier.SMALL,
        LeaseLimits(500_000, 20_000, 2_000, 2, 3600),
        provider="openai",
        model=config.model,
        approval_request_id=10,
    )

    anthropic = store.get_active_lease(session.id, provider="anthropic")

    assert anthropic is not None and anthropic.provider == "anthropic"
    assert store.get_active_lease(session.id, provider="openai") == openai
    assert anthropic.limits != openai.limits


def test_response_api_review_disables_storage_and_settles_reported_usage(
    tmp_path: Path,
) -> None:
    store, session, config, budget = make_runtime(tmp_path)
    responses = FakeResponses()
    authorizations = []
    factories = []
    client = OpenAIReviewClient(
        config=config,
        budget=budget,
        authorize_egress=lambda request: authorizations.append(request) or True,
        environ={"OPENAI_API_KEY": "test-key"},
        client_factory=lambda **kwargs: factories.append(kwargs) or FakeSDKClient(responses),
        sdk_available=lambda: True,
    )

    review = client.review(
        session_id=session.id,
        prompt="Review this release diff",
        context="diff --git a/app.py b/app.py",
        request_id="openai-review-1",
    )

    assert review.ok is True
    assert review.summary == "Solid"
    assert review.findings == ("Add a rollback test",)
    assert review.recommendation == "revise"
    assert review.usage.input_tokens == 600
    assert review.usage.cache_read_tokens == 300
    assert review.usage.output_tokens == 120
    assert responses.calls[0]["model"] == "gpt-5.6-terra"
    assert responses.calls[0]["store"] is False
    assert "tools" not in responses.calls[0]
    assert factories[0]["api_key"] == "test-key"
    assert len(authorizations) == 1
    lease = store.get_active_lease(session.id, provider="openai")
    assert lease is not None
    assert lease.actual_input_tokens == 900
    assert lease.actual_output_tokens == 120


def test_provider_failure_is_not_retried_or_fallbacked_and_marks_spend_uncertain(
    tmp_path: Path,
) -> None:
    store, session, config, budget = make_runtime(tmp_path)
    responses = FakeResponses(failure=TimeoutError("provider timeout"))
    client = OpenAIReviewClient(
        config=config,
        budget=budget,
        authorize_egress=lambda _request: True,
        environ={"OPENAI_API_KEY": "key"},
        client_factory=lambda **_kwargs: FakeSDKClient(responses),
        sdk_available=lambda: True,
    )

    with pytest.raises(TimeoutError, match="provider timeout"):
        client.review(
            session_id=session.id,
            prompt="Review",
            context="diff",
            request_id="uncertain-openai",
        )

    assert len(responses.calls) == 1
    lease = store.get_active_lease(session.id, provider="openai")
    assert lease is not None and lease.state == "paused"
    assert store.list_usage(lease.id)[0].status == "uncertain_spend"
