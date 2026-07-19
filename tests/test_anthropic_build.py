from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cofounder_kernel.anthropic_build import (
    AnthropicBuildModelClient,
    BuildEgressRequired,
    BuildLeaseRequired,
)
from cofounder_kernel.build_budget import BuildBudgetService
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import (
    BuildAssessment,
    BuildTier,
    LeaseLimits,
    PricingSnapshot,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.model_client import CodingModelError


PRICING = PricingSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    base_input_per_mtok="5",
    cache_write_5m_per_mtok="6.25",
    cache_write_1h_per_mtok="10",
    cache_read_per_mtok="0.5",
    output_per_mtok="25",
    review_after="2099-01-01",
)
MESSAGES = [
    {"role": "system", "content": "Stable build instructions"},
    {"role": "user", "content": "Inspect calc.py"},
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def assessment() -> BuildAssessment:
    return BuildAssessment(
        id=None,
        task="Review calc",
        acceptance="Return findings",
        workspace="C:/workspace",
        repo_fingerprint="abc",
        deterministic_score=30,
        local_adjustment=0,
        final_score=30,
        confidence=0.8,
        recommended_tier=BuildTier.MEDIUM,
        dimensions={},
        floor_rules=(),
        evidence={},
        unknowns=(),
        local_work=(),
        cloud_reasons=("high risk review",),
        created_at="2026-07-18T12:00:00+00:00",
    )


def final_message(*, tool_use: bool = False) -> SimpleNamespace:
    content: list[Any] = [SimpleNamespace(type="text", text="Reviewed.")]
    if tool_use:
        content.append(
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="read_file", input={"path": "calc.py"}
            )
        )
    usage = SimpleNamespace(
        input_tokens=50,
        cache_creation_input_tokens=50,
        cache_read_input_tokens=25,
        output_tokens=10,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=20,
            ephemeral_1h_input_tokens=30,
        ),
    )
    return SimpleNamespace(content=content, usage=usage, model="claude-opus-4-8")


class FakeStream:
    def __init__(self, messages: "FakeMessages"):
        self.messages = messages

    def __enter__(self) -> "FakeStream":
        self.messages.entered += 1
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def get_final_message(self) -> SimpleNamespace:
        if self.messages.raise_after_enter:
            raise TimeoutError("lost after headers")
        return self.messages.final


class FakeMessages:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_request: dict[str, Any] = {}
        self.count_error: Exception | None = None
        self.stream_error: Exception | None = None
        self.raise_after_enter = False
        self.entered = 0
        self.final = final_message()

    def count_tokens(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("count", kwargs))
        if self.count_error:
            raise self.count_error
        return SimpleNamespace(input_tokens=100)

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(("stream", kwargs))
        self.last_request = kwargs
        if self.stream_error:
            raise self.stream_error
        return FakeStream(self)


class FakeSDK:
    def __init__(self):
        self.messages = FakeMessages()


def make_adapter(
    tmp_path: Path,
    *,
    with_lease: bool,
    authorize=lambda _session_id, _summary: True,
) -> tuple[AnthropicBuildModelClient, FakeSDK, BuildBudgetService, BuildStore, int]:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(assessment())
    if with_lease:
        store.create_lease(
            session.id,
            BuildTier.MEDIUM,
            LeaseLimits(3_000_000, 400_000, 40_000, 16, 14400),
            provider="anthropic",
            model="claude-opus-4-8",
            approval_request_id=1,
        )
    budget = BuildBudgetService(store, PRICING)
    fake = FakeSDK()
    adapter = AnthropicBuildModelClient(
        session_id=session.id,
        budget=budget,
        sdk_client=fake,
        authorize_egress=authorize,
        provider_overhead_tokens=50,
    )
    return adapter, fake, budget, store, session.id


def test_chat_refuses_before_sdk_request_without_lease(tmp_path: Path) -> None:
    adapter, fake, _budget, _store, _session_id = make_adapter(
        tmp_path, with_lease=False
    )

    with pytest.raises(BuildLeaseRequired):
        adapter.chat(messages=[{"role": "user", "content": "x"}], tools=[])

    assert fake.messages.calls == []


def test_egress_refusal_happens_before_token_count_or_stream(tmp_path: Path) -> None:
    adapter, fake, _budget, _store, _session_id = make_adapter(
        tmp_path, with_lease=True, authorize=lambda *_args: False
    )

    with pytest.raises(BuildEgressRequired):
        adapter.chat(messages=MESSAGES, tools=TOOLS)

    assert fake.messages.calls == []


def test_chat_caches_stable_system_and_tool_prefix(tmp_path: Path) -> None:
    adapter, fake, _budget, _store, _session_id = make_adapter(
        tmp_path, with_lease=True
    )

    adapter.chat(messages=MESSAGES, tools=TOOLS, num_predict=512)

    sent = fake.messages.last_request
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [call[0] for call in fake.messages.calls] == ["count", "stream"]


def test_usage_categories_settle_exactly_and_tool_calls_are_normalized(
    tmp_path: Path,
) -> None:
    adapter, fake, budget, store, session_id = make_adapter(tmp_path, with_lease=True)
    fake.messages.final = final_message(tool_use=True)

    result = adapter.chat(messages=MESSAGES, tools=TOOLS, num_predict=512)

    assert result.response == "Reviewed."
    tool_call = result.raw["message"]["tool_calls"][0]
    assert tool_call["id"] == "toolu_1"
    assert tool_call["function"]["arguments"] == {"path": "calc.py"}
    lease = budget.active_lease(session_id)
    usage = store.list_usage(lease.id)
    assert len(usage) == 1
    assert usage[0].status == "settled"
    assert usage[0].cache_write_5m_tokens == 20
    assert usage[0].cache_write_1h_tokens == 30
    assert usage[0].cache_read_tokens == 25
    assert usage[0].settled_microdollars == 938


def test_timeout_after_stream_start_marks_uncertain(tmp_path: Path) -> None:
    adapter, fake, budget, store, session_id = make_adapter(tmp_path, with_lease=True)
    fake.messages.raise_after_enter = True

    with pytest.raises(CodingModelError, match="lost after headers"):
        adapter.chat(messages=MESSAGES, tools=TOOLS)

    lease = budget.active_lease(session_id)
    assert lease.state == "paused"
    assert store.list_usage(lease.id)[0].status == "uncertain_spend"


def test_failure_before_stream_entry_releases_reservation(tmp_path: Path) -> None:
    adapter, fake, budget, store, session_id = make_adapter(tmp_path, with_lease=True)
    fake.messages.stream_error = RuntimeError("constructor failed")

    with pytest.raises(CodingModelError, match="constructor failed"):
        adapter.chat(messages=MESSAGES, tools=TOOLS)

    lease = budget.active_lease(session_id)
    assert lease.reserved_microdollars == 0
    assert lease.cloud_turns == 0
    assert store.list_usage(lease.id) == []


def test_count_tokens_failure_uses_conservative_local_upper_bound(tmp_path: Path) -> None:
    adapter, fake, _budget, store, session_id = make_adapter(tmp_path, with_lease=True)
    fake.messages.count_error = RuntimeError("count endpoint unavailable")

    adapter.chat(messages=MESSAGES, tools=TOOLS, num_predict=128)

    lease = store.get_active_lease(session_id)
    assert lease is not None
    usage = store.list_usage(lease.id)
    assert len(usage) == 1
    assert usage[0].status == "settled"


def test_model_must_match_the_approved_lease(tmp_path: Path) -> None:
    adapter, fake, _budget, _store, _session_id = make_adapter(
        tmp_path, with_lease=True
    )

    with pytest.raises(BuildLeaseRequired, match="model"):
        adapter.chat(messages=MESSAGES, tools=TOOLS, model="claude-other")

    assert fake.messages.calls == []
