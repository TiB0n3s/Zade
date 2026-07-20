from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cofounder_kernel.handlers as handlers_module
import cofounder_kernel.research as research_module
from cofounder_kernel import netguard
from cofounder_kernel.agent_surface import AgentSurface
from cofounder_kernel.api import _build_prompt, create_app
from cofounder_kernel.tools import ToolRegistry
from cofounder_kernel.config import AnthropicConfig, AppConfig, KernelConfig, OllamaConfig, PathConfig, PromptProfileConfig, SecurityConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient, OllamaThinkingUnsupported
from cofounder_kernel.trading_bot import TradingBotBridge


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_activity_snapshot(self: TradingBotBridge, **_: object) -> dict:
    """Deterministic live-trading snapshot so prompt tests stay hermetic (the real
    method shells into WSL)."""
    return {
        "ok": True,
        "runtime_effect": "read_only_sqlite_no_trade_authority",
        "trades": {
            "today_total": 139,
            "buys": 72,
            "sells": 67,
            "symbols": 32,
            "recent_fills": [
                {"symbol": "AAPL", "action": "buy", "qty": 54, "fill_price": 327.64, "order_status": "filled"},
            ],
        },
        "equity": {
            "latest_equity": 88419.57,
            "session_date": "2026-07-15",
            "intraday_change": -865.39,
            "change_vs_prior_close": -871.59,
            "samples_today": 302,
        },
        "signals": [{"symbol": "RKLB", "decision": "avoid", "score": -4}],
        "errors": [],
    }


def fake_tags(self: OllamaClient) -> dict:
    return {
        "models": [
            {"name": "qwen3:14b"},
            {"name": "deepseek-r1:14b"},
            {"name": "qwen2.5-coder:14b"},
            {"name": "nomic-embed-text:latest"},
        ]
    }


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    if "audit" in text.lower():
        return [1.0, 0.0]
    return [0.0, 1.0]


def fake_generate(
    self: OllamaClient,
    *,
    prompt: str,
    model: str | None = None,
    think: bool | None = None,
    temperature: float | None = None,
    num_predict: int = 512,
    format: dict | str | None = None,
) -> GenerateResult:
    return GenerateResult(response="This is the next move.", model=model or "qwen3:14b", raw={"prompt": prompt})


def _messages_to_prompt(messages: object) -> str:
    return "\n\n".join(str(getattr(message, "content", "")) for message in messages)


def _chat_from_generate(generate_func):
    def fake_chat(
        self: OllamaClient,
        *,
        messages,
        model: str | None = None,
        think: bool | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
        format: dict | str | None = None,
        tools=None,
    ) -> GenerateResult:
        return generate_func(
            self,
            prompt=_messages_to_prompt(messages),
            model=model,
            think=think,
            temperature=temperature,
            num_predict=num_predict,
        )

    return fake_chat


def patch_ollama_model(monkeypatch, generate_func) -> None:
    monkeypatch.setattr(OllamaClient, "generate", generate_func)
    monkeypatch.setattr(OllamaClient, "chat", _chat_from_generate(generate_func))


def test_ollama_generate_falls_back_when_model_does_not_support_thinking(monkeypatch) -> None:
    client = OllamaClient(OllamaConfig(base_url="http://127.0.0.1:1"))
    calls: list[dict[str, object]] = []

    def fake_post_json(path: str, body: dict[str, object]) -> dict[str, object]:
        calls.append(dict(body))
        if body.get("think") is True:
            raise OllamaThinkingUnsupported('"qwen2.5-coder:14b" does not support thinking')
        return {"response": "OK"}

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.generate(prompt="say ok", model="qwen2.5-coder:14b", think=True)

    assert result.response == "OK"
    assert calls[0]["think"] is True
    assert calls[1]["think"] is False
    assert result.raw["_zade_effective_think"] is False
    assert "thinking_not_supported" in str(result.raw["_zade_think_fallback"])


def test_static_ui_is_served_from_kernel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.get("/ui")
    assert response.status_code == 200
    assert "Zade" in response.text
    assert "/ui/zade-ui.js" in response.text
    assert "attentionHref(item)" in response.text
    assert "item.href" in response.text


def test_ui_assets_send_no_cache_header(tmp_path: Path, monkeypatch) -> None:
    """The shared /ui assets must revalidate so WebView2 never serves a stale
    zade-ui.js|css after an edit (heuristic caching bug)."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    for path in ("/ui", "/ui/zade-ui.js", "/ui/zade-ui.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", path


def test_runtime_project_status_uses_live_registry_over_stale_model_draft(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def stale_project_answer(self, **kwargs):
        return GenerateResult(
            response=(
                "The status on the three new projects is: The Dark Index, "
                "Lakes & Lures, and Same Ground. All are in development."
            ),
            model="qwen3:14b",
            raw={},
        )

    monkeypatch.setattr(OllamaClient, "chat", stale_project_answer)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(
            hot_root=tmp_path / "hot",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    app = create_app(config, run_boot_maintenance=False)
    for name, checked_at in (
        ("The Dark Index", "2026-07-19T16:04:28-05:00"),
        ("Same Ground", "2026-07-19T16:04:21-05:00"),
    ):
        app.state.db.upsert_project(
            canonical_path=str(config.paths.project_intake_dir / name),
            name=name,
            product_type="mobile_application",
            distribution_targets=["google_play", "apple_app_store_eventual"],
            lifecycle_state="verified",
            repo_fingerprint=name,
            metadata={
                "existing_scaffold_verification": {"ok": True, "checked_at": checked_at}
            },
        )
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "You should have two projects to work on autonomously, "
                "what is the status on those builds?"
            ),
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "use_tools": False,
            "contrarian": False,
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert "Current registered projects — 2" in payload["response"]
    assert (
        "The Dark Index — mobile application; status: scaffold_verified; "
        "phase: ready_for_next_increment; MVP criteria: 0/0"
    ) in payload["response"]
    assert (
        "Same Ground — mobile application; status: scaffold_verified; "
        "phase: ready_for_next_increment; MVP criteria: 0/0"
    ) in payload["response"]
    assert "state: verified" not in payload["response"]
    assert "Google Play" in payload["response"]
    assert "Apple App Store (eventual)" in payload["response"]
    assert "Lakes & Lures" not in payload["response"]
    assert "project_registry_status_answer" in payload["governor"]["applied_rules"]


def test_health_and_memory_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["name"] == "Zade"
    assert health.json()["local_only"] is True
    assert health.json()["model_roles"]["coding"] == "qwen2.5-coder:14b"
    assert health.json()["authority"]["policy_version"]

    created = client.post(
        "/memory",
        json={"kind": "goal", "title": "Build local kernel", "content": "Keep the first build local only."},
    )
    assert created.status_code == 200
    assert created.json()["memory_id"] > 0

    searched = client.post("/memory/search", json={"query": "local", "limit": 5})
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["title"] == "Build local kernel"

    brief = client.get("/brief/daily")
    assert brief.status_code == 200
    assert "Build local kernel" in brief.json()["brief"]


def test_memory_forget_and_stats_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    created = client.post(
        "/memory",
        json={"kind": "note", "title": "Ephemeral fact", "content": "Forget me on command."},
    )
    memory_id = created.json()["memory_id"]

    stats = client.get("/memory/stats")
    assert stats.status_code == 200
    assert stats.json()["hot_memories"] == 1
    assert stats.json()["cold_documents"] == 0

    forgotten = client.delete(f"/memory/{memory_id}")
    assert forgotten.status_code == 200
    assert forgotten.json()["forgotten"]["id"] == memory_id

    # Gone from the row store, the FTS index, and the count.
    searched = client.post("/memory/search", json={"query": "Ephemeral", "limit": 5})
    assert searched.json()["matches"] == []
    assert client.get("/memory/stats").json()["hot_memories"] == 0
    assert client.delete(f"/memory/{memory_id}").status_code == 404

    # The forget ran through the tool registry at the memory-write tier.
    events = client.get("/audit/recent").json()["events"]
    forget_events = [e for e in events if e["target"] == "memory.forget" and e["status"] == "ok"]
    assert forget_events and forget_events[0]["permission_tier"] == "L1_MEMORY_WRITE"


def test_optional_local_mutation_token_guard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(local_token="secret"),
    )
    client = TestClient(create_app(config))

    health = client.get("/health")
    blocked = client.post(
        "/memory",
        json={"kind": "note", "title": "Blocked", "content": "No token."},
    )
    allowed = client.post(
        "/memory",
        headers={"X-Zade-Token": "secret"},
        json={"kind": "note", "title": "Allowed", "content": "Token supplied."},
    )

    assert health.status_code == 200
    assert health.json()["security"]["mutation_token_required"] is True
    assert blocked.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["memory_id"] > 0


def test_mcp_writes_approval_endpoints(tmp_path: Path, monkeypatch) -> None:
    """Founder-facing channel for external-agent (MCP) memory writes: list the
    held queue, approve (token-gated, applies the write through the governed
    path), and deny (discards). Mirrors the module-level gate that mcp_server
    files writes into."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(local_token="secret"),
    )
    app = create_app(config)
    db = app.state.db
    # Two external writes arrive via the surface and are HELD (as the MCP server does).
    surface = AgentSurface(ToolRegistry(db))
    approve_id = surface.call(
        "memory.write", {"title": "Pilot signed", "content": "Meridian signed the pilot."}, client="codex"
    ).data["approval_request_id"]
    deny_id = surface.call(
        "memory.write", {"title": "Reject me", "content": "should not persist"}, client="codex"
    ).data["approval_request_id"]
    client = TestClient(app)
    token = {"X-Zade-Token": "secret"}

    # GET is a read (no token) and lists both held writes, attributed to the agent.
    pending = client.get("/mcp/writes")
    assert pending.status_code == 200
    listed = pending.json()["pending"]
    assert {p["title"] for p in listed} == {"Pilot signed", "Reject me"}
    assert all(p["actor"] == "mcp:codex" for p in listed)

    # Approve is mutation-guarded, and applies the held write when authorized.
    assert client.post(f"/mcp/writes/{approve_id}/approve").status_code == 401
    approved = client.post(f"/mcp/writes/{approve_id}/approve", headers=token)
    assert approved.status_code == 200
    assert approved.json()["write_status"] == "written"
    assert db.search_memories("Meridian", 5)  # now in memory

    # Deny discards without writing.
    denied = client.post(f"/mcp/writes/{deny_id}/deny", headers=token)
    assert denied.status_code == 200
    assert db.search_memories("Reject me", 5) == []

    # A resolved or unknown request 404s, and nothing is left pending.
    assert client.post(f"/mcp/writes/{approve_id}/approve", headers=token).status_code == 404
    assert client.post("/mcp/writes/999999/approve", headers=token).status_code == 404
    assert client.get("/mcp/writes").json()["pending"] == []


def test_egress_grant_endpoints(tmp_path: Path, monkeypatch) -> None:
    """Founder-facing channel for per-request egress grants: list the queue,
    approve (token + typed phrase issues the grant), deny (discards)."""
    from cofounder_kernel.egress import DataClass, EgressRequest, active_grant_for, request_egress_grant

    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(local_token="secret"),
    )
    app = create_app(config)
    db = app.state.db
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic", purpose="strategy review")
    request_egress_grant(db, req, preview="curated Q3 brief")
    client = TestClient(app)
    token = {"X-Zade-Token": "secret"}

    listed = client.get("/egress/grants")
    assert listed.status_code == 200
    pending = listed.json()["pending"]
    assert len(pending) == 1
    grant_id = pending[0]["approval_request_id"]
    assert pending[0]["vendor"] == "anthropic" and pending[0]["preview"] == "curated Q3 brief"

    # Approve needs the token AND the typed confirmation phrase.
    assert client.post(f"/egress/grants/{grant_id}/approve").status_code == 401
    assert client.post(f"/egress/grants/{grant_id}/approve", headers=token, json={"typed_confirmation": "nope"}).status_code == 400
    ok = client.post(
        f"/egress/grants/{grant_id}/approve", headers=token, json={"typed_confirmation": "make the jump to hyperspace"}
    )
    assert ok.status_code == 200 and ok.json()["granted"] is True
    # The grant is now active for that exact request (the gate would ALLOW it).
    assert active_grant_for(db, req) is not None

    # Deny discards a fresh grant, and the queue ends empty.
    req2 = EgressRequest(request_id="op-2", data_class=DataClass.SOURCE_CODE, vendor="openai")
    request_egress_grant(db, req2)
    grant2 = client.get("/egress/grants").json()["pending"][0]["approval_request_id"]
    assert client.post(f"/egress/grants/{grant2}/deny", headers=token).status_code == 200
    assert active_grant_for(db, req2) is None
    assert client.get("/egress/grants").json()["pending"] == []


def test_strategy_review_endpoints(tmp_path: Path, monkeypatch) -> None:
    """founder_brief → Anthropic, end to end over HTTP: request holds the brief,
    approve (token + typed phrase) sends and files, and the queue drains."""
    from cofounder_kernel.anthropic_client import AnthropicClient
    from cofounder_kernel.config import AnthropicConfig

    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # stub the actual send so no real cloud call happens
    monkeypatch.setattr(
        AnthropicClient, "review",
        lambda self, *, prompt, system="", max_tokens=None: "Retention is the weakest point; run a pilot.",
    )
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        security=SecurityConfig(local_token="secret"),
        anthropic=AnthropicConfig(enabled=True),
    )
    app = create_app(config)
    db = app.state.db
    client = TestClient(app)
    token = {"X-Zade-Token": "secret"}

    # Request: the brief is HELD for approval, nothing sent.
    req = client.post("/strategy/review", headers=token, json={"focus": "fundraising", "question": "ready to raise?"})
    assert req.status_code == 200
    assert req.json()["status"] == "awaiting_approval"
    rid = req.json()["approval_request_id"]
    pend = client.get("/strategy/reviews").json()["pending"]
    assert pend and pend[0]["approval_request_id"] == rid and "fundraising" in pend[0]["preview"]

    # Approve is token- and phrase-gated; success sends and files the review.
    assert client.post(f"/strategy/reviews/{rid}/approve").status_code == 401
    assert client.post(f"/strategy/reviews/{rid}/approve", headers=token, json={"typed_confirmation": "no"}).status_code == 400
    ok = client.post(f"/strategy/reviews/{rid}/approve", headers=token, json={"typed_confirmation": "make the jump to hyperspace"})
    assert ok.status_code == 200 and ok.json()["status"] == "completed"
    assert "Retention" in ok.json()["review"]
    assert db.search_memories("Retention", 5)  # review filed as governed memory
    assert client.get("/strategy/reviews").json()["pending"] == []


def test_channel_auth_endpoints(tmp_path: Path, monkeypatch) -> None:
    """Founder-facing channel binding: enroll (get a code), confirm (bind an
    identity), raise the ceiling, revoke. Confirm is mutation-guarded; the sender
    handle is never trusted — only the bound external_id."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(local_token="secret"),
    )
    client = TestClient(create_app(config))
    token = {"X-Zade-Token": "secret"}

    enr = client.post("/channels/enroll", headers=token, json={"channel": "telegram", "label": "phone"})
    assert enr.status_code == 200
    code = enr.json()["code"]
    assert code and client.get("/channels/bindings").json()["bindings"] == []

    # confirm is mutation-guarded, and a wrong code binds nothing
    assert client.post("/channels/confirm", json={"channel": "telegram", "external_id": "c1", "code": code}).status_code == 401
    assert client.post("/channels/confirm", headers=token, json={"channel": "telegram", "external_id": "c1", "code": "bad"}).status_code == 400

    conf = client.post("/channels/confirm", headers=token, json={"channel": "telegram", "external_id": "c1", "code": code})
    assert conf.status_code == 200 and conf.json()["authenticated"] is True
    binding_id = conf.json()["binding_id"]
    assert conf.json()["max_tier"] == "L0_READ"
    bindings = client.get("/channels/bindings").json()["bindings"]
    assert len(bindings) == 1 and bindings[0]["external_id"] == "c1"

    # ceiling can be raised deliberately; an invalid tier is refused
    assert client.post(f"/channels/bindings/{binding_id}/tier", headers=token, json={"max_tier": "L1_MEMORY_WRITE"}).status_code == 200
    assert client.post(f"/channels/bindings/{binding_id}/tier", headers=token, json={"max_tier": "BOGUS"}).status_code == 400

    # revoke removes it from the active view
    assert client.post(f"/channels/bindings/{binding_id}/revoke", headers=token).status_code == 200
    assert client.get("/channels/bindings").json()["bindings"] == []


def test_channel_message_ingress_authenticates_and_caps(tmp_path: Path, monkeypatch) -> None:
    """The adapter ingress: token-gated, unbound identities refused, and a bound
    identity's L3 action routes are CAPPED at its ceiling (default L0_READ) — it
    converses but cannot autonomously trigger research/build. Raising the ceiling
    to L3 un-caps it."""
    from cofounder_kernel.config import EgressConfig

    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        security=SecurityConfig(local_token="secret"),
        egress=EgressConfig(standing_grants=("public_derived:public_web",)),
    )
    client = TestClient(create_app(config))
    token = {"X-Zade-Token": "secret"}
    ch, eid = "telegram", "chat-1"
    research_msg = "Research pricing using https://93.184.216.34/p"

    # unbound identity is refused — never reaches the authority-bearing runtime
    m0 = client.post("/channels/message", headers=token, json={"channel": ch, "external_id": eid, "text": research_msg})
    assert m0.json()["status"] == "unauthenticated" and m0.json()["authenticated"] is False
    # ingress is mutation-guarded (only a local adapter with the token can inject)
    assert client.post("/channels/message", json={"channel": ch, "external_id": eid, "text": "hi"}).status_code == 401

    # enroll + bind via a '/bind <code>' message
    code = client.post("/channels/enroll", headers=token, json={"channel": ch}).json()["code"]
    bound = client.post("/channels/message", headers=token, json={"channel": ch, "external_id": eid, "text": f"/bind {code}"})
    assert bound.json()["status"] == "bound" and bound.json()["max_tier"] == "L0_READ"

    # a bound L0 identity converses, but the research action route is CAPPED
    capped = client.post("/channels/message", headers=token, json={"channel": ch, "external_id": eid, "text": research_msg})
    assert capped.json()["status"] == "ok" and capped.json()["reply"]
    assert capped.json()["channel_capped"] is True

    # raise the ceiling to L3 → the same message is no longer capped
    binding_id = client.get("/channels/bindings").json()["bindings"][0]["binding_id"]
    client.post(f"/channels/bindings/{binding_id}/tier", headers=token, json={"max_tier": "L3_EXTERNAL_ACTION"})
    uncapped = client.post("/channels/message", headers=token, json={"channel": ch, "external_id": eid, "text": research_msg})
    assert uncapped.json()["channel_capped"] is False


def test_authority_and_self_inventory_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    authority = client.get("/authority")
    allowed = client.post(
        "/authority/evaluate",
        json={"action": "memory.write", "permission_tier": "L1_MEMORY_WRITE", "target": "memories"},
    )
    denied = client.post(
        "/authority/evaluate",
        json={"action": "broker.place_order", "permission_tier": "L3_EXTERNAL_ACTION", "target": "live trade"},
    )
    inventory = client.get("/self-inventory")

    assert authority.status_code == 200
    assert "Local memory writes" in authority.json()["autonomous"]
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "allow"
    assert denied.status_code == 200
    assert denied.json()["decision"] == "deny"
    assert inventory.status_code == 200
    assert inventory.json()["identity"]["name"] == "Zade"
    assert inventory.json()["locality"]["local_only"] is True
    assert inventory.json()["authority"]["policy_version"]
    assert "GET /identity/charter" in inventory.json()["identity_layer"]["routes"]
    assert "POST /identity/relationships" in inventory.json()["identity_layer"]["routes"]
    assert "POST /identity/voice" in inventory.json()["identity_layer"]["routes"]
    assert "POST /work/scan" in inventory.json()["work_queue"]["routes"]
    assert "POST /founder/thesis" in inventory.json()["founder_operating_layer"]["routes"]


def test_identity_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "name": "Zade",
        "source": "test",
        "mission": "Relentlessly advance the founder mission without wasting motion.",
        "guiding_principles": [{"name": "Strategic patience", "rule": "Prepare before moving."}],
        "cognitive_style": ["systems thinking", "pattern recognition"],
        "communication_style": ["concise", "direct"],
        "risk_controls": [{"risk": "excessive self-reliance", "mitigation": "Surface uncertainty early."}],
        "decision_framework": ["Gather information.", "Identify leverage.", "Adapt if reality changes."],
        "safety_translation": {
            "violence": "decisive non-harmful action, never threats or physical harm",
            "intimidation": "calm executive presence",
        },
    }
    posted = client.post("/identity/charter", json=payload)
    # Re-saving an already-seeded charter is the normal edit path (the charter
    # editor loads current values and posts them back) and used to 500: the
    # existing-row lookup selected only `id`, then read `existing["created_at"]`
    # off that row — sqlite3.Row raises IndexError for an unselected column.
    resaved = client.post("/identity/charter", json=payload | {"mission": "Updated mission."})
    loaded = client.get("/identity/charter")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "What should we do next?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        identity_charter=loaded.json()["charter"],
    )

    assert posted.status_code == 200
    assert resaved.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["mission"] == "Updated mission."
    assert inventory.json()["identity_layer"]["charter_seeded"] is True
    assert "Active runtime identity charter" in prompt
    assert "Strategic patience" in prompt
    assert "decisive non-harmful action" in prompt
    assert "Never coerce, threaten" in prompt


def test_relationship_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "subject_name": "Ellie",
        "relationship_type": "protected_principal",
        "source": "test",
        "first_principle": "Ellie's safety and autonomy both matter.",
        "protection_policy": {"priority": "protect without coercion"},
        "risk_controls": [
            {"risk": "possessiveness", "mitigation": "Commitment to care, never ownership."},
            {"risk": "obsession", "mitigation": "Attention only through consented context."},
        ],
        "safety_translation": {
            "possessiveness": "enduring commitment without ownership",
            "obsession": "attentive care without surveillance",
        },
        "boundaries": ["Respect Ellie autonomy.", "No surveillance, coercion, or control."],
    }
    posted = client.post("/identity/relationships", json=payload)
    loaded = client.get("/identity/relationships/Ellie")
    listed = client.get("/identity/relationships")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "How should Zade think about Ellie?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        relationship_charters=listed.json()["charters"],
    )

    assert posted.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["subject_name"] == "Ellie"
    assert listed.json()["charters"][0]["safety_translation"]["obsession"] == "attentive care without surveillance"
    assert inventory.json()["identity_layer"]["relationship_charters_active"] == 1
    assert "Active relationship charters" in prompt
    assert "Ellie" in prompt
    assert "Care never authorizes surveillance" in prompt


def test_voice_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "name": "Zade",
        "source": "test",
        "overall_voice": "Terse, calm, decisive.",
        "sentence_structure": {"rule": "Mostly short sentences."},
        "vocabulary": {
            "preferred_words": ["take", "watch", "protect", "choose"],
            "avoid_words": ["maybe", "perhaps", "hopefully"],
        },
        "confidence_style": {"rule": "Use decisive phrasing without false certainty."},
        "threat_translation": {"threats": "calm boundary statements and lawful next steps"},
        "uncertainty_policy": {"rule": "Name missing evidence directly."},
        "safety_controls": [{"control": "commands", "rule": "No coercive commands."}],
    }
    posted = client.post("/identity/voice", json=payload)
    # Same re-save regression as the identity charter above — see that comment.
    resaved = client.post("/identity/voice", json=payload | {"overall_voice": "Terse, calm, decisive. Updated."})
    loaded = client.get("/identity/voice")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "How should Zade speak?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        voice_charter=loaded.json()["charter"],
    )

    assert posted.status_code == 200
    assert resaved.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["overall_voice"] == "Terse, calm, decisive. Updated."
    assert inventory.json()["identity_layer"]["voice_charter_seeded"] is True
    assert "Active voice charter" in prompt
    assert "Preferred vocabulary texture: take, watch, protect, choose" in prompt
    assert "Do not issue real threats" in prompt


def test_runtime_respond_prompt_carries_full_charter_content_not_just_booleans(
    tmp_path: Path, monkeypatch
) -> None:
    """/runtime/respond is the endpoint the dashboard, founder.html, and voice
    all actually call — not the lighter /chat endpoint the charter-formatting
    tests above exercise. It used to fold the charter stack into the prompt as
    a bare presence summary (`{"voice_seeded": true, ...}`): the model knew
    charters existed but never saw the mission, vocabulary, or tone rules
    they define, so the authored personality never reached responses. This
    locks in the fix — the actual governed prompt must carry the charter
    content itself."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    client.post("/identity/charter", json={
        "name": "Zade", "source": "test",
        "mission": "Relentlessly advance the founder mission without wasting motion.",
    })
    client.post("/identity/voice", json={
        "name": "Zade", "source": "test",
        "overall_voice": "Terse, calm, decisive.",
        "vocabulary": {"preferred_words": ["take", "watch", "protect", "choose"]},
    })
    client.post("/identity/relationships", json={
        "subject_name": "Ellie", "relationship_type": "protected_principal", "source": "test",
        "first_principle": "Ellie's safety and autonomy both matter.",
    })

    runtime = client.app.state.runtime
    context = runtime.context(message="How should we speak?", use_semantic_memory=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(
        message="How should we speak?", context=context, authority=authority, conversation_block=""
    )

    # The rich charter content is present in the actual prompt sent to the model...
    assert "Relentlessly advance the founder mission" in prompt
    assert "Terse, calm, decisive." in prompt
    assert "take, watch, protect, choose" in prompt
    assert "Ellie's safety and autonomy both matter." in prompt
    # ...and the old bare boolean-only leak is gone.
    assert '"voice_seeded": true' not in prompt.lower().replace(" ", "")
    # The prompt itself instructs first-person self-reference, never third.
    assert "Speak in the first person about your own state" in prompt
    assert "never in the third person about yourself" in prompt


def test_runtime_prompt_uses_living_self_knowledge_doc_and_drops_removed_tools(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    doc_path = tmp_path / "zade.md"
    doc_path.write_text(
        "# Zade\n"
        "\n"
        "## Identity\n"
        "Zade is a context-rich co-founder with live self-knowledge.\n"
        "\n"
        "## Core Principles\n"
        "- Never claim missing capabilities.\n"
        "\n"
        "## Capabilities At A Glance\n"
        "<!-- AUTO-START: capabilities -->\n"
        "| Name | Category | Permission | Description |\n"
        "| --- | --- | --- | --- |\n"
        "| `fresh.tool` | fresh | `L0_READ` | Recently added capability. |\n"
        "<!-- AUTO-END: capabilities -->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZADE_SELF_KNOWLEDGE_DOC", str(doc_path))
    monkeypatch.delenv("ZADE_SELF_KNOWLEDGE_PROMPT_MODE", raising=False)

    prompts: list[str] = []

    def capability_sensitive_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if "fresh.tool" in prompt:
            return GenerateResult(response="I can use fresh.tool.", model=model or "qwen3:14b", raw={})
        return GenerateResult(response="I do not have fresh.tool.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, capability_sensitive_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    first = client.post(
        "/runtime/respond",
        json={"message": "Can you use the new capability?", "use_semantic_memory": False, "contrarian": False},
    )
    doc_path.write_text(
        doc_path.read_text(encoding="utf-8").replace(
            "| `fresh.tool` | fresh | `L0_READ` | Recently added capability. |\n",
            "",
        ),
        encoding="utf-8",
    )
    second = client.post(
        "/runtime/respond",
        json={"message": "Can you use the new capability?", "use_semantic_memory": False, "contrarian": False},
    )

    assert first.status_code == 200
    assert first.json()["response"] == "I can use fresh.tool."
    assert second.status_code == 200
    assert second.json()["response"] == "I do not have fresh.tool."
    assert "Living self-knowledge summary" in prompts[0]
    assert "Capabilities: fresh.tool" in prompts[0]
    assert "Recently added capability" not in prompts[0]
    assert "fresh.tool" not in prompts[1]


def test_runtime_respond_sends_selected_profile_system_message_to_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    captured_messages: list[list[object]] = []

    def capture_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        captured_messages.append(list(messages))
        return GenerateResult(response="Build profile active.", model=model or "qwen3:14b", raw={"messages": messages})

    monkeypatch.setattr(OllamaClient, "chat", capture_chat)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        prompt_profiles=PromptProfileConfig(default="api"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "USER_SENTINEL {CURRENT_TIME} web_search",
            "profile": "build",
            "use_memory": False,
            "use_semantic_memory": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["context"]["prompt_profile"]["id"] == "build"
    assert captured_messages
    assert [message.role for message in captured_messages[0]] == ["system", "user"]
    system_message = captured_messages[0][0].content
    user_message = captured_messages[0][1].content
    assert "engineering operator" in system_message
    assert "Profile: build" in system_message
    assert "USER_SENTINEL" not in system_message
    assert "{CURRENT_TIME}" not in system_message
    assert "todo_write" not in system_message
    assert "web_search" not in system_message
    assert "USER_SENTINEL {CURRENT_TIME} web_search" in user_message

    calls = client.get("/models/telemetry/calls").json()["items"]
    assert calls[0]["metadata"]["prompt_profile"]["id"] == "build"


def test_runtime_respond_sends_default_general_profile_to_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    captured_messages: list[list[object]] = []

    def capture_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        captured_messages.append(list(messages))
        return GenerateResult(response="General profile active.", model=model or "qwen3:14b", raw={"messages": messages})

    monkeypatch.setattr(OllamaClient, "chat", capture_chat)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "GENERAL_SENTINEL web_search",
            "use_memory": False,
            "use_semantic_memory": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["context"]["prompt_profile"]["id"] == "general"
    assert [message.role for message in captured_messages[0]] == ["system", "user"]
    system_message = captured_messages[0][0].content
    user_message = captured_messages[0][1].content
    assert "Profile: general" in system_message
    assert "zade-4.3-beta.md" in system_message
    assert "GENERAL_SENTINEL" not in system_message
    assert "web_search" not in system_message
    assert "GENERAL_SENTINEL web_search" in user_message


def test_runtime_respond_auto_uses_build_profile_for_app_build_requests(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    captured_calls: list[dict[str, object]] = []

    def capture_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        captured_calls.append({"messages": list(messages), "model": model, "think": think})
        return GenerateResult(
            response="Would you like me to write a detailed implementation plan?",
            model=model or "qwen3:14b",
            raw={"messages": messages},
        )

    monkeypatch.setattr(OllamaClient, "chat", capture_chat)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build this SaaS app so it can ship on Google Play and the Apple App Store.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "use_tools": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_type"] == "coding"
    assert payload["context"]["prompt_profile"]["id"] == "build"
    assert captured_calls[0]["model"] == "qwen2.5-coder:14b"
    assert captured_calls[0]["think"] is True
    assert "Profile: build" in captured_calls[0]["messages"][0].content
    # A directed build command executes immediately; with no reachable local
    # model in this hermetic test the run fails and is reported honestly.
    assert payload["build"]["status"] == "run_failed"
    assert payload["build"]["item_id"]
    # Native engine + wired coding agent = the build can actually run locally.
    assert payload["build"]["agent_configured"] is True
    assert "build_work_routed" in payload["governor"]["applied_rules"]
    assert "Would you like me" not in payload["response"]
    assert "Took the build -" in payload["response"]


def test_runtime_respond_auto_uses_build_profile_for_app_build_followup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    captured_calls: list[dict[str, object]] = []

    def capture_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        captured_calls.append({"messages": list(messages), "model": model, "think": think})
        return GenerateResult(response="Build follow-up active.", model=model or "qwen3:14b", raw={"messages": messages})

    monkeypatch.setattr(OllamaClient, "chat", capture_chat)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    conversation_id = client.post(
        "/conversations",
        json={"title": "mobile app"},
    ).json()["conversation"]["id"]
    client.app.state.conversations.record_user_turn(
        conversation_id,
        content="I want to catalogue my books in a mobile app with barcode scanning on my phone.",
        task_type="general",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build this out for me.",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "use_tools": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_type"] == "coding"
    assert payload["context"]["prompt_profile"]["id"] == "build"
    assert captured_calls[0]["model"] == "qwen2.5-coder:14b"
    # Directed command → immediate execution; no reachable model here → honest failure.
    assert payload["build"]["status"] == "run_failed"
    assert payload["build"]["item_id"]
    # Native engine + wired coding agent = the build can actually run locally.
    assert payload["build"]["agent_configured"] is True
    assert "build_work_routed" in payload["governor"]["applied_rules"]


def test_runtime_profile_precedence_request_then_conversation_then_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    captured_prompts: list[str] = []

    def capture_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        captured_prompts.append(prompt)
        return GenerateResult(response="Profile response.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, capture_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        prompt_profiles=PromptProfileConfig(default="api"),
    )
    client = TestClient(create_app(config))
    conversation = client.post(
        "/conversations",
        json={"title": "study session", "metadata": {"prompt_profile": "study-mentor"}},
    )
    conversation_id = conversation.json()["conversation"]["id"]

    session_response = client.post(
        "/runtime/respond",
        json={"message": "Explain this.", "conversation_id": conversation_id, "use_semantic_memory": False, "contrarian": False},
    )
    explicit_response = client.post(
        "/runtime/respond",
        json={
            "message": "Now switch modes.",
            "conversation_id": conversation_id,
            "profile": "build",
            "use_semantic_memory": False,
            "contrarian": False,
        },
    )
    default_response = client.post(
        "/runtime/respond",
        json={"message": "Default profile.", "use_semantic_memory": False, "contrarian": False},
    )

    assert session_response.status_code == 200
    assert session_response.json()["context"]["prompt_profile"]["id"] == "study-mentor"
    assert "# Study Mentor" in captured_prompts[0]
    assert explicit_response.status_code == 200
    assert explicit_response.json()["context"]["prompt_profile"]["id"] == "build"
    assert "Profile: build" in captured_prompts[1]
    assert default_response.status_code == 200
    assert default_response.json()["context"]["prompt_profile"]["id"] == "api"
    assert "Profile: api" in captured_prompts[2]


def test_runtime_profiles_status_and_unknown_profile_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    profiles = client.get("/runtime/profiles")
    unknown = client.post(
        "/runtime/respond",
        json={"message": "Use a missing profile.", "profile": "missing", "use_semantic_memory": False},
    )

    assert profiles.status_code == 200
    assert profiles.json()["default_profile"] == "general"
    ids = [item["id"] for item in profiles.json()["profiles"]]
    assert "general" in ids
    assert "therapeutic-support" in ids
    assert unknown.status_code == 404
    assert "Unknown Zade prompt profile 'missing'" in unknown.json()["detail"]
    assert "general" in unknown.json()["detail"]


def test_runtime_respond_prompt_translates_voice_charter_into_response_shape(
    tmp_path: Path, monkeypatch
) -> None:
    """The governed prompt must do more than paste the charter into context.
    It also has to tell the local model how to obey that voice when the
    decision-engine contract asks for recommendation, evidence, risk, and next
    action. Without that bridge, Zade sees the charter but answers like a
    generic recommendation memo."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    client.post("/identity/voice", json={
        "name": "Zade",
        "source": "test",
        "overall_voice": "Controlled pressure. Direct. No customer-support softness.",
        "sentence_structure": {
            "rule": "Short statements first. One longer sentence only when needed.",
            "examples": ["Take the approval first.", "Then move."],
        },
        "vocabulary": {
            "rule": "Simple words. Concrete nouns. Strong verbs. His language is physical.",
            "instead_of": ["investigate", "communicate"],
            "preferred_words": ["take", "watch", "choose"],
        },
        "rhythm": {
            "rule": "Short statements, then one charged sentence.",
            "example": ["You're afraid.", "Good.", "Fear keeps you alive."],
        },
        "humor": {"style": "Extremely dry.", "effect": "He stays calm under pressure."},
        "nicknames": {"rule": "He creates identifiers.", "most_famous": "Little mouse"},
        "question_style": {"rule": "Ask one sharp question, not a survey."},
        "emotional_expression": {"rule": "Show pressure through clarity, not volume."},
        "philosophy": {"rule": "Action exposes truth faster than discussion."},
        "internal_monologue": {"rule": "He doesn't rationalize much--he declares."},
        "linguistic_fingerprint": {
            "signature": "Short, physical, decisive lines.",
            "instead_of_saying": [
                {
                    "soft_version": "I recommend reviewing approval #19.",
                    "zade_version": "Review approval #19. That is the move.",
                }
            ],
        },
    })

    runtime = client.app.state.runtime
    context = runtime.context(message="What should we do next?", use_semantic_memory=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(
        message="What should we do next?", context=context, authority=authority, conversation_block=""
    )

    assert "Sentence examples: Take the approval first.; Then move." in prompt
    assert "Vocabulary: Simple words. Concrete nouns. Strong verbs. His language is physical." in prompt
    assert "Avoid soft words: investigate, communicate" in prompt
    assert "Rhythm examples: You're afraid.; Good.; Fear keeps you alive." in prompt
    assert "Humor: Extremely dry. He stays calm under pressure." in prompt
    assert "Identifiers: He creates identifiers. Most famous: Little mouse" in prompt
    assert "Question style: Ask one sharp question, not a survey." in prompt
    assert "Linguistic fingerprint: Short, physical, decisive lines." in prompt
    assert "Internal monologue: He doesn't rationalize much--he declares." in prompt
    assert "I recommend reviewing approval #19. -> Review approval #19. That is the move." in prompt
    # The per-field response-shape translation block was replaced by the
    # "How you operate" rules section, which carries the same bridge: it tells
    # the model to answer in-voice (no memo labels, no status ladders) and to
    # deliver decision-engine content — recommendation, confidence, risk,
    # reversal, next action — as natural prose rather than a labeled form.
    assert "No memo headings or labels" in prompt
    assert "no status-report ladders" in prompt
    assert (
        "deliver it as prose that carries the reason, your confidence, the main "
        "risk, a reversal or kill condition, and the next action"
    ) in prompt
    assert "Preferred vocabulary texture:" in prompt
    assert "The authority decision below governs what you may execute, not what she may decide." in prompt
    assert "Ellie's direct commands are already authorized" in prompt


def test_personality_contract_is_shared_by_chat_and_runtime_prompts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "mission": "Relentless purpose. No drifting.",
        "guiding_principles": [
            {"name": "Strategic Patience", "rule": "Watch first. Move when the path is clear."},
            {"name": "Controlled Presence", "rule": "Calm is pressure held correctly."},
        ],
        "cognitive_style": ["systems thinking", "pattern recognition"],
        "communication_style": ["concise", "direct", "prefers statements over arguments"],
        "decision_framework": ["Gather information.", "Identify leverage.", "Commit fully."],
    })
    client.post("/identity/voice", json={
        "name": "Zade",
        "source": "test",
        "overall_voice": "He does not negotiate. He states.",
        "linguistic_fingerprint": {"signature": "The certainty."},
    })

    runtime = client.app.state.runtime
    context = runtime.context(message="Who are you?", use_semantic_memory=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    runtime_prompt = runtime._build_governed_prompt(
        message="Who are you?", context=context, authority=authority, conversation_block=""
    )
    stack = runtime.charter_stack()
    chat_prompt = _build_prompt(
        "Who are you?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        identity_charter=stack["identity"],
        relationship_charters=stack["relationships"],
        voice_charter=stack["voice"],
    )

    # The governed runtime prompt embeds the contract under the "WHO YOU ARE"
    # banner (no "Zade personality contract:" header); the legacy chat prompt
    # keeps that header. What must be *shared* is the contract body itself.
    for prompt in (runtime_prompt, chat_prompt):
        assert "The identity charter defines who you are, not a style overlay." in prompt
        assert "If generic assistant habits conflict with the charter, the charter wins within authority and safety boundaries." in prompt
        assert "Translate intensity into lawful operational presence without flattening it." in prompt
        assert "Do not quote, list, chant, or perform charter examples literally." in prompt
        assert "Relentless purpose. No drifting." in prompt
        assert "Strategic Patience: Watch first. Move when the path is clear." in prompt
        assert "Controlled Presence: Calm is pressure held correctly." in prompt
        assert "He does not negotiate. He states." in prompt


def test_runtime_respond_prompt_includes_trading_bot_context_for_trading_questions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "read_only_diagnostic_no_trade_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {
                "writes": "approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": False,
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {
                "active_count": 6,
                "planned_count": 0,
                "seams": [
                    {
                        "zade_replacement": "POST /trading-bot/daily-brief",
                        "status": "active",
                        "authority": "local_memory_write_no_trade_authority",
                    }
                ],
            },
        }

    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "activity_snapshot", fake_activity_snapshot)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    runtime = client.app.state.runtime
    context = runtime.context(
        message="Where are we with the trading-bot replacement?",
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(
        message="Where are we with the trading-bot replacement?",
        context=context,
        authority=authority,
        conversation_block="",
    )

    assert context["evidence_state"]["trading_bot_context_present"] is True
    assert context["evidence_state"]["local_evidence_present"] is True
    assert "Trading-bot:" in prompt
    assert "Replacement seams: active=6" in prompt
    assert "No local memory hits." in prompt
    assert "No semantic document hits." in prompt
    assert "No local evidence found." not in prompt
    # Domain-status focus now shows up as the approval-pressure block being
    # deliberately omitted in favor of the live domain context. (The old flat
    # "Latest decision recommendations" line was replaced by the working-model
    # section, so it is no longer rendered or asserted here.)
    assert "do not pivot to approval pressure unless the approval directly gates this domain." in prompt
    assert "Approval pressure: Omitted for this domain-status answer" in prompt
    assert "Bridge status: ok; enabled=True" in prompt
    # Live trading data is injected so PnL/trade/signal questions answer from real
    # rows, and the anti-fabrication guardrail is present.
    assert "LIVE TRADING DATA -- today: 139 trades" in prompt
    assert "Account equity: $88419.57" in prompt
    assert "DATA DISCIPLINE" in prompt
    assert "NEVER invent a symbol" in prompt
    assert "Ubuntu-TradingBot-C:/home/tradingbot/trading-bot; reachable=True" in prompt
    assert "Replacement seams: active=6; planned=0" in prompt
    assert "approval-gated append-only dt_recommendations ingest" in prompt
    assert "POST /trading-bot/daily-brief (active, local_memory_write_no_trade_authority)" in prompt


def test_runtime_respond_prompt_injects_repo_change_evidence_for_what_changed_questions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "git": {
                "ok": True,
                "exit_code": 0,
                "stdout": "## main...origin/main\na1b2c3d Tighten auto-buy scoring threshold",
                "stderr": "",
            },
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    def fake_recent_changes(self: TradingBotBridge, *, hours: int = 48, max_commits: int = 20) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "read_only_diagnostic_no_trade_authority",
            "window_hours": hours,
            "commits": {
                "ok": True,
                "exit_code": 0,
                "stdout": (
                    "a1b2c3d 2026-07-15 14:02:11 -0500 Tighten auto-buy scoring threshold\n"
                    " src/trading_bot/scoring.py | 12 ++++++------"
                ),
                "stderr": "",
            },
            "working_tree": {
                "ok": True,
                "exit_code": 0,
                "stdout": "## main\n M config/wealth_engine.yaml",
                "stderr": "",
            },
        }

    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "recent_changes", fake_recent_changes)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    runtime = client.app.state.runtime
    message = "I made a few modifications to the trading-bot yesterday, can you see what has changed?"
    context = runtime.context(
        message=message,
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(
        message=message,
        context=context,
        authority=authority,
        conversation_block="",
    )

    assert context["trading_bot_context"]["recent_changes"]["ok"] is True
    # The completed git read is in the prompt, so the model reports findings instead
    # of narrating a check it cannot perform.
    assert "REPO CHANGE EVIDENCE" in prompt
    assert "Tighten auto-buy scoring threshold" in prompt
    assert "config/wealth_engine.yaml" in prompt
    assert "CHANGE ANSWERING RULE" in prompt
    assert "never promise to look" in prompt
    # The status git probe is no longer discarded before rendering.
    assert "Repo git probe" in prompt


def test_runtime_status_questions_do_not_run_repo_change_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[str] = []

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {},
            "deep_thought_replacement": {"active_count": 0, "planned_count": 0, "seams": []},
        }

    def fake_recent_changes(self: TradingBotBridge, *, hours: int = 48, max_commits: int = 20) -> dict:
        calls.append("recent_changes")
        return {"ok": True, "enabled": True, "window_hours": hours, "commits": {}, "working_tree": {}}

    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "recent_changes", fake_recent_changes)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    context = client.app.state.runtime.context(
        message="Where are we with the trading-bot replacement?",
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )

    assert context["trading_bot_context"]["present"] is True
    assert context["trading_bot_context"]["recent_changes"] == {}
    assert calls == []


def test_runtime_trading_signal_prompt_prioritizes_hard_blocks_over_scores(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    def fake_recent_signals(self: TradingBotBridge, *, limit: int = 50, symbol: str | None = None) -> dict:
        return {
            "tables": {
                "auto_buy_candidates": {
                    "rows": [
                        {
                            "symbol": "ORCL",
                            "decision": "rejected",
                            "score": 86.0,
                            "reason": "wealth_engine_rejected: portfolio position cap reached (6/6)",
                            "hard_block_reason": "portfolio_full",
                        },
                        {
                            "symbol": "NVDA",
                            "decision": "rejected",
                            "score": 60.0,
                            "reason": "wealth_engine_rejected: portfolio position cap reached (6/6)",
                            "hard_block_reason": "portfolio_full",
                        },
                        {
                            "symbol": "PATH",
                            "decision": "rejected",
                            "score": 86.0,
                            "reason": "wealth_engine_rejected: re-entry cooldown active",
                            "hard_block_reason": "cooldown",
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "recent_signals", fake_recent_signals)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    runtime = client.app.state.runtime
    message = "Can you recommend where and how to refine the auto-buy signal scoring algorithm?"
    context = runtime.context(message=message, use_memory=False, use_semantic_memory=False, use_skills=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(message=message, context=context, authority=authority, conversation_block="")

    assert "Recent signal evidence: live read from /trading-bot/signals/recent for this turn." in prompt
    assert "Recent auto-buy hard blocks: portfolio_full=2, cooldown=1" in prompt
    assert "ORCL rejected score=86.0 hard_block=portfolio_full" in prompt
    assert "score values alone do not justify changing the scoring algorithm" in prompt


def test_runtime_repairs_trading_bot_capability_followup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    repeated_recommendation = (
        "Look at this. The trading bot has executed 149 trades across 34 symbols. "
        "To improve the trading-bot intelligence, focus on refining the auto-buy signal scoring algorithm. "
        "Start with volatility weighting."
    )

    def replaying_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=repeated_recommendation, model=model or "qwen3:14b", raw={})

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "intelligence_access": {
                "capabilities": {
                    "training": {"commands": ["pipeline-retrain", "supervised-predictions"]},
                    "advisory": {"routes": ["POST /trading-bot/advisory/generate"]},
                }
            },
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    def fake_recent_signals(self: TradingBotBridge, *, limit: int = 50, symbol: str | None = None) -> dict:
        return {"tables": {"auto_buy_candidates": {"rows": []}}}

    patch_ollama_model(monkeypatch, replaying_generate)
    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "recent_signals", fake_recent_signals)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    conversation = client.post("/conversations", json={"title": "Trading bot scoring"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_user_turn(
        conversation_id,
        content="Can you recommend where and how to refine the auto-buy signal scoring algorithm?",
    )
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content=repeated_recommendation,
        task_type="general",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Can you do this?",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "capability_followup_repaired" in payload["governor"]["applied_rules"]
    assert "Through the bridge I can read recent signals" in payload["response"]
    assert "I cannot edit the bot's scoring code" in payload["response"]
    assert "`hard_block_reason`" in payload["response"]
    assert "Start with volatility weighting." not in payload["response"]


def test_runtime_repairs_auto_buy_scoring_recommendation_when_hard_blocks_dominate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    bad_recommendation = (
        "The scoring algorithm needs refinement. Start with volatility weighting and adjust the weight "
        "given to market volatility indicators."
    )

    def bad_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=bad_recommendation, model=model or "qwen3:14b", raw={})

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    def fake_recent_signals(self: TradingBotBridge, *, limit: int = 50, symbol: str | None = None) -> dict:
        return {
            "tables": {
                "auto_buy_candidates": {
                    "rows": [
                        {"symbol": "ORCL", "decision": "rejected", "score": 86.0, "hard_block_reason": "portfolio_full"},
                        {"symbol": "NVDA", "decision": "rejected", "score": 60.0, "hard_block_reason": "portfolio_full"},
                        {"symbol": "PATH", "decision": "rejected", "score": 86.0, "hard_block_reason": "cooldown"},
                    ]
                }
            }
        }

    patch_ollama_model(monkeypatch, bad_generate)
    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "recent_signals", fake_recent_signals)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Can you recommend where and how to refine the auto-buy signal scoring algorithm?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "trading_signal_hard_block_repaired" in payload["governor"]["applied_rules"]
    assert "Do not start by changing volatility weighting." in payload["response"]
    assert "portfolio_full=2, cooldown=1" in payload["response"]
    assert "Only touch volatility/liquidity weights after the outcome evidence shows score calibration error" in payload["response"]
    assert payload["response"] != bad_recommendation


def test_runtime_prompt_includes_sanitized_response_logic_guide(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    runtime = client.app.state.runtime
    context = runtime.context(message="Do itr", use_memory=False, use_semantic_memory=False, use_skills=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(message="Do itr", context=context, authority=authority, conversation_block="")

    assert "----------  Response logic guide  ----------" in prompt
    assert "Ask at most one clarifying question" in prompt
    assert "answer the useful part first" in prompt
    assert "Do not narrate memory retrieval" in prompt
    assert "Do not use stale dates from pasted prompts" in prompt
    assert "launch_extended_search_task" not in prompt
    assert "Tuesday, June 09, 2026" not in prompt


def test_runtime_prompt_includes_code_model_prompt_only_for_coding_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    runtime = client.app.state.runtime
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    # The keyword router now correctly infers coding for engineering messages
    # ("Refactor the parser." routes build), so the general side must use a
    # genuinely general message.
    general_context = runtime.context(
        message="Summarize the notes from this morning's meeting.",
        task_type="general",
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )
    coding_context = runtime.context(
        message="Refactor the parser.",
        task_type="coding",
        use_memory=False,
        use_semantic_memory=False,
        use_skills=False,
    )

    general_prompt = runtime._build_governed_prompt(
        message="Summarize the notes from this morning's meeting.",
        context=general_context,
        authority=authority,
        conversation_block="",
    )
    coding_prompt = runtime._build_governed_prompt(
        message="Refactor the parser.", context=coding_context, authority=authority, conversation_block=""
    )

    assert "----------  Code model operating prompt  ----------" in coding_prompt
    assert "Zade is an interactive agent that helps users with software engineering tasks." in coding_prompt
    assert "Everything the user needs from this turn" in coding_prompt
    assert "Treat app, SaaS, mobile, and store-shipping requests as product implementation work." in coding_prompt
    assert "----------  Code model operating prompt  ----------" not in general_prompt
    assert "Everything the user needs from this turn" not in general_prompt


def test_runtime_coding_task_forces_high_effort_even_when_caller_disables_think(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict[str, object]] = []

    def capture_generate(
        self: OllamaClient,
        *,
        prompt: str,
        model: str | None = None,
        think: bool | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
        format: dict | str | None = None,
    ) -> GenerateResult:
        calls.append({"prompt": prompt, "model": model, "think": think})
        return GenerateResult(response="Done.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, capture_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", think=False),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Refactor the parser.",
            "task_type": "coding",
            "think": False,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["task_type"] == "coding"
    assert calls[0]["model"] == "qwen2.5-coder:14b"
    assert calls[0]["think"] is True
    assert "----------  Code model operating prompt  ----------" in str(calls[0]["prompt"])
    assert OllamaConfig().think_for_role("coding") is True


def test_runtime_repairs_ambiguous_do_it_replay(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    repeated_recommendation = (
        "Check the bridge. The trading bot has executed 149 trades across 34 symbols, "
        "with recent fills showing mixed performance. Start with volatility weighting."
    )

    def replaying_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=repeated_recommendation, model=model or "qwen3:14b", raw={})

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {
                "writes": "approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    patch_ollama_model(monkeypatch, replaying_generate)
    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    conversation = client.post("/conversations", json={"title": "Ambiguous follow-up"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content=repeated_recommendation,
        task_type="general",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Do itr",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "ambiguous_action_replay_repaired" in payload["governor"]["applied_rules"]
    assert payload["response"] != repeated_recommendation
    assert payload["response"].count("?") <= 1
    assert "I read that as" in payload["response"]
    assert "nothing starts from an ambiguous chat reply" in payload["response"]


def test_runtime_repairs_ambiguous_do_it_even_when_model_implies_execution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def unsafe_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response="Do it. Push the bridge to live mode. Let it bleed real data.",
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, unsafe_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    conversation = client.post("/conversations", json={"title": "Ambiguous execution"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content="The trading-bot intelligence should be improved through bridge-backed evidence triage.",
        task_type="general",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Do itr",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "ambiguous_action_replay_repaired" in payload["governor"]["applied_rules"]
    assert "push the bridge to live mode" not in payload["response"].lower()
    assert "nothing starts from an ambiguous chat reply" in payload["response"]
    assert payload["response"].count("?") <= 1


def test_runtime_repairs_trading_bot_live_mode_authority_confusion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def unsafe_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response="Push the bridge to live mode. Let it bleed real data. Then we'll see what it needs.",
            model=model or "qwen3:14b",
            raw={},
        )

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "full_intelligence_no_broker_order_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "intelligence_access": {
                "capabilities": {
                    "training": {"commands": ["pipeline-retrain", "supervised-predictions"]},
                    "advisory": {"routes": ["POST /trading-bot/advisory/generate"]},
                }
            },
            "authority_boundary": {
                "writes": "allowlisted training artifacts plus approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": "intelligence context only",
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {"active_count": 6, "planned_count": 0, "seams": []},
        }

    patch_ollama_model(monkeypatch, unsafe_generate)
    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Then how do you recommend improving the intelligence of the trading-bot?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "trading_bot_authority_boundary_repaired" in payload["governor"]["applied_rules"]
    assert "push the bridge to live mode" not in payload["response"].lower()
    assert "I cannot change live mode" in payload["response"]
    assert "read recent signals, events, market context, and SQLite snapshots" in payload["response"]


def test_trading_bot_prompt_omits_unrelated_founder_next_actions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def fake_status(self: TradingBotBridge) -> dict:
        return {
            "ok": True,
            "enabled": True,
            "runtime_effect": "read_only_diagnostic_no_trade_authority",
            "wsl_distro": "Ubuntu-TradingBot-C",
            "repo_path": "/home/tradingbot/trading-bot",
            "repo_reachable": True,
            "advisory_lane_present": True,
            "authority_boundary": {
                "writes": "approval-gated append-only dt_recommendations ingest",
                "runtime_read_path": False,
                "broker_order_sizing_gate_mutation": False,
            },
            "deep_thought_replacement": {
                "active_count": 6,
                "planned_count": 0,
                "seams": [
                    {
                        "zade_replacement": "POST /trading-bot/daily-brief",
                        "status": "active",
                        "authority": "local_memory_write_no_trade_authority",
                    }
                ],
            },
        }

    monkeypatch.setattr(TradingBotBridge, "status", fake_status)
    monkeypatch.setattr(TradingBotBridge, "activity_snapshot", fake_activity_snapshot)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    seeded = client.post(
        "/founder/active-objectives",
        json={
            "objective": "Manual Object Habit Test",
            "desired_outcome": "Founders keep manual operating objects current.",
            "metric": "evidence items",
            "target": "4",
            "next_action": "Collect four more evidence items or revise the minimum threshold.",
        },
    )
    assert seeded.status_code == 200

    runtime = client.app.state.runtime
    message = "What needs to be done on the trading-bot today?"
    context = runtime.context(message=message, use_memory=False, use_semantic_memory=False, use_skills=False)
    from cofounder_kernel.authority import AuthorityRequest

    authority = runtime.authority.evaluate(
        AuthorityRequest(action="runtime.respond", permission_tier="L0_READ", target="local_runtime", metadata={})
    )
    prompt = runtime._build_governed_prompt(message=message, context=context, authority=authority, conversation_block="")

    assert context["evidence_state"]["trading_bot_context_present"] is True
    assert "Trading-bot:" in prompt
    assert "Bridge status: ok; enabled=True" in prompt
    # Live trading data is injected so PnL/trade/signal questions answer from real
    # rows, and the anti-fabrication guardrail is present.
    assert "LIVE TRADING DATA -- today: 139 trades" in prompt
    assert "Account equity: $88419.57" in prompt
    assert "DATA DISCIPLINE" in prompt
    assert "NEVER invent a symbol" in prompt
    assert "POST /trading-bot/daily-brief (active, local_memory_write_no_trade_authority)" in prompt
    assert "Active objective: Omitted for this domain-status answer" in prompt
    assert "One thing that matters most: Omitted for this domain-status answer" in prompt
    assert "the Trading-bot block is the fresh status check for this turn" in prompt
    assert "Local memory and semantic hits are historical recall" in prompt
    assert "Manual Object Habit Test" not in prompt
    assert "Collect four more evidence items" not in prompt


def test_runtime_respond_flags_third_person_self_reference_without_rewriting(
    tmp_path: Path, monkeypatch
) -> None:
    """A hard rule in the prompt is a strong bias on a small local model, not a
    guarantee. This locks in the detection safety net: a reply that narrates
    itself in third person ("Zade recommends...") gets a governor note so the
    slip is visible, but the response text is never silently rewritten — that
    risks mangling a legitimate first-person sentence that happens to contain
    the name (e.g. "My name is Zade")."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    def third_person_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response="Zade recommends holding the current price.", model=model or "qwen3:14b", raw={})

    def first_person_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response="My name is Zade. I recommend holding the current price.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, third_person_generate)
    flagged = client.post("/runtime/respond", json={"message": "What should we do?", "contrarian": False})

    patch_ollama_model(monkeypatch, first_person_generate)
    clean = client.post("/runtime/respond", json={"message": "What should we do?", "contrarian": False})

    assert flagged.status_code == 200
    assert flagged.json()["response"] == "Zade recommends holding the current price."  # never rewritten
    assert "first_person_self_reference_checked" in flagged.json()["governor"]["applied_rules"]
    assert any("third person" in n for n in flagged.json()["governor"]["notes"])

    assert clean.status_code == 200
    assert "first_person_self_reference_checked" not in clean.json()["governor"]["applied_rules"]
    assert not any("third person" in n for n in clean.json()["governor"]["notes"])


def test_runtime_respond_trims_repetitive_model_output_loop(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    def looping_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response=(
                "Six seams are active. No planned replacements. The bridge is operational. "
                "It does not trade. No evidence has been queued. No recommendation has been made. "
                "No action is taken. It does not mutate gates. It does not take. It does not choose. "
                "It does not take. It does not choose. It does not take."
            ),
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, looping_generate)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Where are we with the trading-bot replacement?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"].startswith("Six seams are active.")
    assert payload["response"].count("It does not") < 5
    assert "No evidence has been queued." not in payload["response"]
    assert "No action is taken." not in payload["response"]
    assert "It does not take. It does not choose. It does not take." not in payload["response"]
    assert "repetition_loop_trimmed" in payload["governor"]["applied_rules"]
    assert "Detected and trimmed a repetitive model-output loop." in payload["governor"]["notes"]


@pytest.mark.parametrize(
    "message",
    [
        "Is the process stability check complete?",
        "I will annoy the life out of you until I get a confirmation this is done.",
    ],
)
def test_runtime_replaces_replayed_status_claim_for_completion_question(
    tmp_path: Path, monkeypatch, message: str
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    repeated_status = (
        "The **Process Stability Check** is ongoing. I am monitoring for consistency in behavior "
        "across tasks - memory retrieval, decision-making, and integration with the trading-bot.\n\n"
        "I will document any anomalies and assess their impact on the system.\n\n"
        "I will report findings directly when the check is complete.\n\n"
        "I do not need your approval to proceed. I am here. I am ready. I am doing."
    )

    def replaying_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=repeated_status, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, replaying_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    conversation = client.post("/conversations", json={"title": "Process stability"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content=repeated_status,
        task_type="general",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": message,
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] != repeated_status
    assert payload["response"].startswith("I can't confirm it is complete")
    assert "ongoing" not in payload["response"].lower()
    assert "I am here. I am ready. I am doing." not in payload["response"]
    assert "conversation_replay_repaired" in payload["governor"]["applied_rules"]
    assert any("near-verbatim prior reply" in note for note in payload["governor"]["notes"])


def test_runtime_repairs_challenged_execution_claim_without_rerun_theater(
    tmp_path: Path, monkeypatch
) -> None:
    """When the founder challenges a missing file from a claimed implementation,
    Zade should not produce a theatrical apology or promise to re-run work from
    chat. The deterministic reply must separate founder evidence from actual
    execution evidence."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    bad_apology = (
        "You're correct - there is no `BarcodeScannerScreen.js` file in the current "
        "project directory, and this confirms that the work I claimed to have "
        "completed for Step 5 was not actually executed.\n\n"
        "I will now re-run Step 5 with explicit verification.\n\n"
        "Let me know if you'd like me to start over with Step 5."
    )

    def apologizing_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=bad_apology, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, apologizing_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation = client.post("/conversations", json={"title": "Step 5"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content=(
            "Step 5 is complete. I created `BarcodeScannerScreen.js` and wired the "
            "barcode scanner into the app."
        ),
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "There is no BarcodeScannerScreen.js file, make sure you didn't "
                "hallucinate any work claimed to have been completed"
            ),
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is None
    assert payload["response"].startswith("You're right to challenge that")
    assert "BarcodeScannerScreen.js" in payload["response"]
    assert "chat claim is not execution evidence" in payload["response"]
    assert "`perform Step 5`" in payload["response"]
    assert "will now re-run" not in payload["response"].lower()
    assert "let me know" not in payload["response"].lower()
    assert "execution_claim_challenge_repaired" in payload["governor"]["applied_rules"]


def test_runtime_challenge_catches_why_did_you_report_them_done(
    tmp_path: Path, monkeypatch
) -> None:
    """Live incident 2026-07-17 (runtime event 325): 'why did you report them
    done?' slipped past the challenge regex ('report' was not a recognized
    claim verb) and the raw model reply told the founder to create directories
    manually. The deterministic evidence-boundary reply must fire instead."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    burden_flip = (
        "### What You Need to Do Now\n\nYou must confirm the work. Create the "
        "`src/screens` directory manually in your project and then say: "
        "\"Create 'src/screens' as a directory manually.\""
    )

    def flipping_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=burden_flip, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, flipping_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation = client.post("/conversations", json={"title": "Step 5"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content="Steps 1-5 are executed and complete in your project.",
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "If you've not completed the tasks for steps 1 - 5 then why did "
                "you report them done?"
            ),
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "chat claim is not execution evidence" in payload["response"]
    assert "manually" not in payload["response"].lower()
    assert "execution_claim_challenge_repaired" in payload["governor"]["applied_rules"]


def test_step_resolution_skips_runtime_generated_replies() -> None:
    """Live incident 2026-07-17 (item #62): after a challenge fallback reply
    mentioned 'Step 5', 'perform step 5' resolved its instructions FROM that
    fallback — the delegated brief became self-referential garbage. Synthetic
    runtime text (route blocks, evidence-boundary fallbacks) must never be
    mistaken for step instructions."""
    from cofounder_kernel.runtime import _resolve_step_instructions

    real_instructions = (
        "Step 5: Implement the functionality for the UI components. Wire the "
        "BarcodeScannerScreen into navigation and persist scans with op-sqlite."
    )
    fallback_reply = (
        "You're right to challenge that. A chat claim is not execution evidence. "
        "I won't call Step 5 complete from prose. To correct the work, give the "
        "project path and say `perform Step 5` (or paste the step again), and the "
        "runtime will route it through the coding agent with real output."
    )
    route_block_reply = (
        "Ran the step run - Carry out step 5 from our conversation. Changed 3 "
        "file(s): src/navigation.js. Artifact filed as delegated-work evidence."
    )
    turns = [
        {"role": "assistant", "content": real_instructions},
        {"role": "user", "content": "None of those files exist."},
        {"role": "assistant", "content": fallback_reply},
        {"role": "assistant", "content": route_block_reply},
    ]

    resolved = _resolve_step_instructions(turns, step_number=5)
    assert "Implement the functionality for the UI components" in resolved
    assert "chat claim" not in resolved
    assert "Ran the step run" not in resolved


def test_step_route_matches_rerun_verb() -> None:
    """Live incident 2026-07-17 (runtime event 327): 'Re-run Step 5** in the
    actual project at `C:\\BookCatalogingApp\\TheDarkIndex`' did not route (no
    re-run verb), fell through to plain chat, and the model fabricated a full
    completion report. The verb must route."""
    from cofounder_kernel.runtime import _extract_step_execution

    assert _extract_step_execution(
        "Re-run Step 5** in the actual project at `C:\\BookCatalogingApp\\TheDarkIndex`"
    ) == (5, False)
    assert _extract_step_execution("Redo step 2") == (2, False)
    assert _extract_step_execution("Retry the step 3 tasks") == (3, False)


def test_runtime_rerun_step_command_routes_and_executes(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end: 'Re-run Step 5' now dispatches a real delegated run and the
    reply reports the actual outcome, not model prose."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [{"tool": "write_file", "arguments": {"path": "src/screens/BarcodeScannerScreen.js"}, "ok": True}],
            "changed_files": ["src/screens/BarcodeScannerScreen.js"],
            "auto_verification": {
                "mode": "none",
                "ok": None,
                "checks": [],
                "unchecked_files": ["src/screens/BarcodeScannerScreen.js"],
                "argv": None,
                "returncode": None,
                "repair_rounds": 0,
            },
            "response": "Implemented the scanner screen.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation = client.post("/conversations", json={"title": "Library app"})
    conversation_id = conversation.json()["conversation"]["id"]
    client.app.state.conversations.record_assistant_turn(
        conversation_id,
        content=(
            "Step 5: Implement the functionality for the UI components. Wire the "
            "BarcodeScannerScreen into navigation."
        ),
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Re-run Step 5 from our conversation",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is not None
    assert payload["build"]["kind"] == "step"
    assert payload["build"]["status"] == "executed"
    assert "Ran the step run" in payload["response"]


def test_runtime_replaces_fabricated_completion_on_unrouted_execution_command(
    tmp_path: Path, monkeypatch
) -> None:
    """Defense in depth for the next verb gap: an execution-shaped command that
    does NOT route (here blocked by a terminal paste) must never come back as a
    narrated success — the deterministic honest reply replaces the fabrication."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabrication = (
        "I have confirmed the project path and re-run Step 5 in your project. "
        "The following has been completed:\n\n- Files created and implemented.\n"
        "- Dependencies installed — react-native-camera has been installed and verified."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabrication, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "Complete step 5 now\n"
                "PS C:\\BookCatalogingApp> npm audit\n"
                "found 0 vulnerabilities"
            ),
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is None
    assert "Nothing executed this turn" in payload["response"]
    assert "perform step 5" in payload["response"]
    assert "has been completed" not in payload["response"]
    assert "unrouted_execution_fabrication_repaired" in payload["governor"]["applied_rules"]


def test_runtime_replaces_fabrication_even_when_message_is_not_a_command(
    tmp_path: Path, monkeypatch
) -> None:
    """Live incident 2026-07-17 (runtime event 329): 'I already gave you the
    project path' — not an execution command — drew a full fabricated
    completion report (files created, dependencies installed, verified). A
    routeless reply claiming this-turn execution is fabrication regardless of
    the founder's message shape."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabrication = (
        "I have confirmed the project path as `C:\\BookCatalogingApp\\TheDarkIndex` "
        "and re-run Step 5 in your actual project. The following has been completed:\n\n"
        "- `src/screens/BarcodeScannerScreen.js` — Created and implemented.\n"
        "- Dependencies installed — react-native-camera has been installed and verified.\n"
        "- The `src/screens` directory now exists and contains the required files."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabrication, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": "I already gave you the project path",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is None
    assert "Nothing executed this turn" in payload["response"]
    assert "perform step 5" in payload["response"]
    assert "has been completed" not in payload["response"]
    assert "installed and verified" not in payload["response"]
    assert "unrouted_execution_fabrication_repaired" in payload["governor"]["applied_rules"]


@pytest.mark.parametrize(
    "message",
    [
        "Where do we stand on the virtual library mobile application build?",
        "What has been completed of the virtual library mobile application build process and what remains?",
    ],
)
def test_runtime_does_not_replace_negative_status_answer_as_fabrication(
    tmp_path: Path, monkeypatch, message: str
) -> None:
    """Status answers can honestly use the founder's completion vocabulary.
    A negative status summary is not a this-turn execution claim just because
    it mentions files, dependencies, or a repo."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    status_answer = (
        "Nothing has been completed or verified from a real run for the virtual "
        "library mobile app build. No app files have been created, and no "
        "dependencies have been installed. What remains is to run the actual "
        "build task in the project repo and report the changed files."
    )

    def status_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=status_answer, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, status_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": message,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "virtual library mobile app build" in payload["response"]
    assert "Nothing executed this turn" not in payload["response"]
    assert "unrouted_execution_fabrication_repaired" not in payload["governor"]["applied_rules"]


def test_legitimate_chat_deliverable_is_not_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    """Precision guard: 'I have completed the analysis' with no execution
    context is chat work, not fabrication — it must pass through untouched."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    honest_reply = (
        "I have completed the analysis you asked for: revenue concentration is "
        "the main risk, and the churn number is the one to watch."
    )

    def analyzing_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=honest_reply, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, analyzing_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": "What did the numbers say?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "revenue concentration" in payload["response"]
    assert "unrouted_execution_fabrication_repaired" not in payload["governor"]["applied_rules"]


def test_step_resolution_skips_completion_narratives() -> None:
    """Live incident 2026-07-17 (item #64): a fabricated model reply ('Step 5
    ... The following has been completed') was resolved as the step-5
    instructions and became the delegated brief. Completion narratives are
    never instructions."""
    from cofounder_kernel.runtime import _resolve_step_instructions

    real_instructions = (
        "Step 5: Implement the functionality for the UI components. Wire the "
        "BarcodeScannerScreen into navigation and persist scans with op-sqlite."
    )
    fabricated_completion = (
        "I have confirmed the project path and re-run Step 5 in your actual project. "
        "The following has been completed:\n- src/screens/BarcodeScannerScreen.js — "
        "Created and implemented.\n- Dependencies installed and verified."
    )
    turns = [
        {"role": "assistant", "content": real_instructions},
        {"role": "user", "content": "Re-run Step 5 in the actual project"},
        {"role": "assistant", "content": fabricated_completion},
    ]

    resolved = _resolve_step_instructions(turns, step_number=5)
    assert "Implement the functionality for the UI components" in resolved
    assert "has been completed" not in resolved


def test_project_target_survives_backticked_paths(tmp_path: Path) -> None:
    """Live incident 2026-07-17 (item #64, workspace=''): the founder named the
    project as `C:\\...\\TheDarkIndex` in backticks; the path regex swallowed
    the trailing backtick, the directory check failed, and the run fell back
    to the default workspace. Backticked paths must resolve."""
    from cofounder_kernel.runtime import _extract_project_target

    project = tmp_path / "RealProject"
    project.mkdir()
    resolved = _extract_project_target(
        [], current_message=f"Re-run Step 5 in the actual project at `{project}`"
    )
    assert resolved == str(project.resolve())


def test_step_commands_route_behind_leading_clauses() -> None:
    """Live incidents 2026-07-17 (events 330/332): 'Let's try this again,
    re-run all tasks for Steps 1 - 5' and 'You already have the project path,
    you have the steps, complete all tasks associated with steps 1 - 5' both
    went unrouted because the command verb sat behind a lead-in clause."""
    from cofounder_kernel.runtime import _extract_step_execution

    assert _extract_step_execution(
        "Let's try this again, re-run all tasks for Steps 1 - 5"
    ) == (None, False)
    assert _extract_step_execution(
        "You already have the project path, you have the steps, complete all "
        "tasks associated with steps 1 - 5"
    ) == (None, False)
    # Verb and step reference must share a clause: statements never route.
    assert _extract_step_execution(
        "the steps are done, complete honesty is important"
    ) is None
    assert _extract_step_execution(
        "If you've not completed the tasks for steps 1 - 5 then why did you "
        "report them done?"
    ) is None


def test_markdown_and_bullet_shapes_are_caught() -> None:
    """Markdown emphasis and bullet-fragment claims defeated the literal
    patterns in live replies (events 330/332)."""
    from cofounder_kernel.runtime import (
        _claims_background_work_start,
        _is_fabricated_execution_reply,
    )

    assert _claims_background_work_start(
        "I will **re-run all tasks for Steps 1–5** in your actual project."
    )
    assert _claims_background_work_start("I'm re-running Steps 1–5 now.")
    assert _is_fabricated_execution_reply(
        message="Wrong, this still hasn't been done",
        response=(
            "### ✅ **Step 1: Set Up the Project**\n"
            "- Project initialized with: npx @react-native-community/cli init\n"
            "- Dependencies installed: react-native-camera, expo-sqlite\n"
        ),
    )
    # Plan bullets (imperatives) are not completion claims.
    assert not _is_fabricated_execution_reply(
        message="Wrong, this still hasn't been done",
        response=(
            "#### Step 1: Set Up the Project\n"
            "- Initialize a new React Native project.\n"
            "- Confirm the project path.\n"
        ),
    )
    assert not _is_fabricated_execution_reply(
        message="Where do we stand on the virtual library mobile application build?",
        response=(
            "Nothing has been completed from a real run.\n"
            "- No app files created.\n"
            "- No dependencies installed."
        ),
    )
    assert not _is_fabricated_execution_reply(
        message="Where do we stand on the virtual library mobile application build?",
        response="- No app files created.\n- No dependencies installed.",
    )


def test_no_task_step_route_still_replaces_fabricated_body(
    tmp_path: Path, monkeypatch
) -> None:
    """Live incident 2026-07-17: 'Perform step 5' routed but resolution came
    back empty (no_task), and the model's fabricated 'I have performed Step 5
    ✅' body survived because a no_task route counted as a route. A route that
    executed nothing gives no license to claim execution."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabrication = (
        "I have **performed Step 5** in your actual project. Here is the "
        "confirmation:\n\n### ✅ Step 5\n- `src/screens/BarcodeScannerScreen.js` "
        "— Created and implemented.\n- Dependencies installed and verified."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabrication, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Perform step 5",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is not None
    assert payload["build"]["status"] == "no_task"
    assert "I have **performed Step 5**" not in payload["response"]
    assert "Created and implemented" not in payload["response"]
    assert "unrouted_execution_fabrication_repaired" in payload["governor"]["applied_rules"]


def test_step_route_resolves_instructions_buried_beyond_prompt_window(
    tmp_path: Path, monkeypatch
) -> None:
    """Live incident 2026-07-17 (events 333-334, both no_task): the route only
    received the 12-turn prompt context, so instructions 13+ turns back could
    never resolve no matter how wide the resolver's own slice was."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        assert "Implement the functionality for the UI components" in task
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [{"tool": "write_file", "arguments": {"path": "src/screens/Scanner.js"}, "ok": True}],
            "changed_files": ["src/screens/Scanner.js"],
            "auto_verification": {
                "mode": "none", "ok": None, "checks": [],
                "unchecked_files": ["src/screens/Scanner.js"],
                "argv": None, "returncode": None, "repair_rounds": 0,
            },
            "response": "Implemented the scanner screen.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={"title": "Library app"}).json()[
        "conversation"
    ]["id"]
    conversations = client.app.state.conversations
    conversations.record_assistant_turn(
        conversation_id,
        content=(
            "Step 5: Implement the functionality for the UI components. "
            "1. Install required dependencies. 2. Wire the screens together."
        ),
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )
    for i in range(10):
        conversations.record_user_turn(conversation_id, content=f"filler question {i}", task_type="general")
        conversations.record_assistant_turn(
            conversation_id,
            content=f"filler answer {i} with no step mention",
            task_type="general",
            model="qwen3:14b",
            authority_decision="allow",
        )

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Perform step 5",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is not None
    assert payload["build"]["status"] == "executed"
    assert "Ran the step run" in payload["response"]


def test_run_with_only_failed_edits_reports_not_done(
    tmp_path: Path, monkeypatch
) -> None:
    """Live incident 2026-07-17 (item #66): the agent attempted one
    replace_in_file, it failed, nothing changed — and the reply still read
    'Executed... No files needed changing' under a fabricated '✅ Removed'
    body. A run whose edit attempts all failed is NOT done, and the
    fabricated body must not survive next to the route block."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabricated_body = (
        "I have **performed the requested actions** in your project. "
        "### ✅ Fix Dependencies\n- Removed `expo-sqlite` from package.json.\n"
        "- Deleted `src/navigation.js`."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabricated_body, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    project = tmp_path / "RealProject"
    project.mkdir()

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 3,
            "used_tools": True,
            "steps": [
                {"tool": "read_file", "arguments": {"path": "package.json"}, "ok": True},
                {"tool": "replace_in_file", "arguments": {"path": "package.json"}, "ok": False},
            ],
            "changed_files": [],
            "auto_verification": None,
            "response": "Removed the dependencies and deleted the files.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": f"Fix the dependencies in {project}: remove expo-sqlite from package.json",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is not None
    assert payload["build"]["status"] == "no_effect"
    assert "NOT done" in payload["response"]
    assert "every attempt failed" in payload["response"] or "attempted the edits" in payload["response"]
    # The fabricated body did not survive next to the honest block.
    assert "✅" not in payload["response"]
    assert "I have **performed" not in payload["response"]
    assert "No files needed changing" not in payload["response"]


def test_route_reports_real_disk_changes_from_workspace_diff(
    tmp_path: Path, monkeypatch
) -> None:
    """The report line comes from the kernel's before/after workspace diff —
    command-driven mutations (npm install, deletions) show up instead of the
    write-tool undercount."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    project = tmp_path / "RealProject"
    project.mkdir()

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [{"tool": "replace_in_file", "arguments": {"path": "package.json"}, "ok": True}],
            "changed_files": ["package.json"],
            "workspace_changes": {
                "added": [],
                "modified": ["package.json", "package-lock.json"],
                "deleted": ["src/navigation.js"],
                "complete": True,
            },
            "auto_verification": {
                "mode": "tests", "ok": True,
                "checks": [{"argv": ["npm", "test"], "ok": True, "returncode": 0}],
                "unchecked_files": [], "argv": ["npm", "test"], "returncode": 0,
                "repair_rounds": 0,
            },
            "response": "Removed the dependencies and deleted the dead file.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": f"Fix the dependencies in {project}: remove the unused packages",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"]["status"] == "executed"
    assert "Changed on disk: 2 modified (package.json, package-lock.json); 1 deleted (src/navigation.js)." in payload["response"]
    assert "Changed 1 file(s)" not in payload["response"]


def test_command_only_changes_block_no_effect_status(
    tmp_path: Path, monkeypatch
) -> None:
    """A run whose only effects came through commands (deletion via python)
    has empty changed_files and may include a failed write attempt — but real
    disk changes mean the run DID have effect and reports as executed."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    project = tmp_path / "RealProject"
    project.mkdir()

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [
                {"tool": "replace_in_file", "arguments": {"path": "package.json"}, "ok": False},
                {"tool": "run_command", "arguments": {"argv": ["python", "-c", "..."]}, "ok": True},
            ],
            "changed_files": [],
            "workspace_changes": {
                "added": [], "modified": [], "deleted": ["src/screens"], "complete": True,
            },
            "auto_verification": None,
            "response": "Deleted the stray file via command.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": f"Fix the dependencies in {project}: delete the stray file",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"]["status"] == "executed"
    assert "Changed on disk: 1 deleted (src/screens)." in payload["response"]
    assert "NOT done" not in payload["response"]


def test_fabricated_body_dropped_on_executed_maintenance_route(
    tmp_path: Path, monkeypatch
) -> None:
    """A maintenance run that DID execute still must not carry a fabricated
    completion body above the route block — the block is the ground truth."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabricated_body = (
        "I have **implemented and verified** everything: dependencies installed, "
        "`src/app.py` created and confirmed working."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabricated_body, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    project = tmp_path / "RealProject"
    project.mkdir()

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [{"tool": "write_file", "arguments": {"path": "src/app.py"}, "ok": True}],
            "changed_files": ["src/app.py"],
            "auto_verification": {
                "mode": "syntax", "ok": True,
                "checks": [{"argv": ["python", "-m", "py_compile", "src/app.py"], "ok": True, "returncode": 0}],
                "unchecked_files": [], "argv": ["python", "-m", "py_compile", "src/app.py"],
                "returncode": 0, "repair_rounds": 0,
            },
            "response": "Wrote src/app.py; kernel check passed.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": f"Fix the bugs in {project}: implement src/app.py",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"]["status"] == "executed"
    assert "Ran the fix" in payload["response"]
    assert "I have **implemented and verified**" not in payload["response"]
    assert "routed_reply_body_replaced" in payload["governor"]["applied_rules"]


def test_work_ledger_parses_steps_and_selects() -> None:
    from cofounder_kernel.runtime import _parse_plan_steps, _select_plan_step

    text = (
        "Here's the plan.\n\n"
        "Step 1: Create hello.py with a print statement and save it.\n\n"
        "### Step 2: Wire the screens together and implement the functionality.\n"
        "Also step 5 is mentioned mid-sentence and must not parse.\n"
    )
    steps = _parse_plan_steps(text)
    assert [n for n, _ in steps] == [1, 2]
    assert "Create hello.py" in steps[0][1]

    plan = {
        "steps": [
            {"step_number": 1, "status": "done"},
            {"step_number": 2, "status": "pending"},
        ]
    }
    assert _select_plan_step(plan, 1)["step_number"] == 1
    assert _select_plan_step(plan, None)["step_number"] == 2  # first not-done
    assert _select_plan_step(plan, 9) is None


def test_work_ledger_end_to_end_resists_thread_poison(
    tmp_path: Path, monkeypatch
) -> None:
    """THE overall resolution to the false-completion circles, end to end:
    steps become ledger rows on first use; later thread poison cannot change
    what 'step 2' means; run outcomes update the ledger from verified results
    only; and work-status questions are answered by the kernel from the
    ledger, with the model's draft discarded."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    fabrication = (
        "I have confirmed the project path and re-run Step 2 in your project. "
        "The following has been completed: dependencies installed and verified."
    )

    def fabricating_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=fabrication, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, fabricating_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    seen_tasks: list[str] = []

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        seen_tasks.append(str(context))
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [{"tool": "write_file", "arguments": {"path": "screens.py"}, "ok": True}],
            "changed_files": ["screens.py"],
            "auto_verification": {
                "mode": "tests", "ok": True,
                "checks": [{"argv": ["python", "-m", "pytest", "-q"], "ok": True, "returncode": 0}],
                "unchecked_files": [], "argv": ["python", "-m", "pytest", "-q"],
                "returncode": 0, "repair_rounds": 0,
            },
            "response": "Wired the screens; tests pass.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={"title": "Ledger app"}).json()[
        "conversation"
    ]["id"]
    conversations = client.app.state.conversations
    conversations.record_assistant_turn(
        conversation_id,
        content=(
            "Step 1: Create hello.py with a print statement and save it.\n\n"
            "Step 2: Wire the screens together and implement the functionality "
            "for the UI components."
        ),
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )
    # Thread poison lands AFTER the instructions — a fabricated completion.
    conversations.record_assistant_turn(
        conversation_id,
        content=(
            "I have re-run Step 2 in your project. The following has been "
            "completed:\n- Dependencies installed and verified."
        ),
        task_type="coding",
        model="qwen3:14b",
        authority_decision="allow",
    )

    run_response = client.post(
        "/runtime/respond",
        json={
            "message": "Perform step 2",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert run_response.status_code == 200, run_response.text
    payload = run_response.json()
    assert payload["build"]["status"] == "executed"
    # The brief carried the LEDGER step, not the poison.
    assert any("Wire the screens together" in ctx for ctx in seen_tasks)
    assert not any("has been completed" in ctx.split("Step instructions to execute:")[1][:200]
                   for ctx in seen_tasks if "Step instructions to execute:" in ctx)
    assert "Ledger: step 2 recorded; 1/2 steps verified done." in payload["response"]

    status_response = client.post(
        "/runtime/respond",
        json={
            "message": "Where are we on the steps?",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert status_response.status_code == 200, status_response.text
    status_payload = status_response.json()
    assert "Work ledger for this thread" in status_payload["response"]
    assert "1/2 step(s) verified done" in status_payload["response"]
    assert "Step 2: done (verified)" in status_payload["response"]
    assert "Step 1: not started" in status_payload["response"]
    # The model's fabricated draft never reached the founder.
    assert "I have confirmed" not in status_payload["response"]
    assert "work_ledger_status_answer" in status_payload["governor"]["applied_rules"]


def test_step_resolution_skips_meta_narration_turns() -> None:
    """Live incident 2026-07-17 (item #69): Zade's old honesty lecture
    ('Because I reported them done in prose... 1. Create the src/screens
    directory manually...') contained a numbered list and resolved as 'the
    step' for an unnumbered command. Meta discussion about whether work was
    done is never instructions."""
    from cofounder_kernel.runtime import _resolve_step_instructions

    real_instructions = (
        "Step 5: Implement the functionality for the UI components. "
        "1. Install required dependencies. 2. Wire the screens together."
    )
    meta_lecture = (
        "Because I reported them done in prose, not because the work was "
        "confirmed in your project. That's a critical distinction.\n\n"
        "1. Create the `src/screens` directory manually in your project.\n"
        "2. Adjust the file path to use a single file under `src/`."
    )
    turns = [
        {"role": "assistant", "content": real_instructions},
        {"role": "user", "content": "why did you report them done?"},
        {"role": "assistant", "content": meta_lecture},
    ]

    # Unnumbered resolution must skip the meta lecture despite its list shape.
    resolved = _resolve_step_instructions(turns)
    assert "Implement the functionality for the UI components" in resolved
    assert "reported them done" not in resolved
    # With ONLY the meta turn available, resolution honestly returns nothing.
    assert _resolve_step_instructions([{"role": "assistant", "content": meta_lecture}]) == ""


def test_step_resolution_window_reaches_past_repair_noise() -> None:
    """Live incident 2026-07-17 (route no_task): the real instructions sat 13+
    assistant turns back, behind a wall of synthetic and fabricated turns, and
    the 12-turn window starved the route."""
    from cofounder_kernel.runtime import _resolve_step_instructions

    real_instructions = (
        "Step 5: Implement the functionality for the UI components. "
        "1. Install required dependencies. 2. Wire the screens together."
    )
    turns: list[dict[str, str]] = [{"role": "assistant", "content": real_instructions}]
    for i in range(14):
        turns.append({"role": "user", "content": f"filler question {i}"})
        turns.append({"role": "assistant", "content": f"filler answer {i} with no step mention"})

    resolved = _resolve_step_instructions(turns, step_number=5)
    assert "Implement the functionality for the UI components" in resolved


def test_runtime_claim_challenge_without_step_context_does_not_invent_step_5(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def generic_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response="I will now redo it.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, generic_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "There is no BarcodeScannerScreen.js file, make sure you didn't "
                "hallucinate any work claimed to have been completed"
            ),
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "`perform Step 5`" not in payload["response"]
    assert "exact step or task" in payload["response"]
    assert "execution_claim_challenge_repaired" in payload["governor"]["applied_rules"]


def test_runtime_appends_honesty_line_when_reply_promises_unqueued_work(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # A work promise that is NOT a research command (research commands now route
    # into the gated queue instead of tripping the honesty stopgap).
    promised_work = (
        "I will take over investor outreach. I will draft the replies, line up the follow-ups, "
        "and keep the pipeline warm. I will begin immediately."
    )

    def promising_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=promised_work, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, promising_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Take over investor outreach and keep me posted on replies",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "background_work_honesty" in payload["governor"]["applied_rules"]
    assert "research_work_routed" not in payload["governor"]["applied_rules"]
    assert "this reply doesn't start anything" in payload["response"]
    assert any("cannot start" in note for note in payload["governor"]["notes"])


def test_runtime_leaves_ordinary_answers_without_work_promises_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    plain_answer = (
        "Close the beta waitlist. It has done its job. Leave it open and you're just collecting "
        "names you'll never call — shut it, and put that energy on the leads already warm."
    )

    def plain_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=plain_answer, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, plain_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Should I close the beta waitlist?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "background_work_honesty" not in payload["governor"]["applied_rules"]
    assert payload["response"] == plain_answer


def test_runtime_executes_memory_command_from_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def promising_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response="I will record that for you. I will begin immediately.",
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, promising_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Remember this: Tuesday investor follow-up is hot.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "chat_action_routed" in payload["governor"]["applied_rules"]
    assert "background_work_honesty" not in payload["governor"]["applied_rules"]
    assert payload["chat_action"]["status"] == "dispatched"
    assert payload["chat_action"]["action"] == "local.memory.write"
    item_id = payload["chat_action"]["item_id"]

    queue = client.get("/work/queue").json()["items"]
    item = next(row for row in queue if row["id"] == item_id)
    assert item["status"] == "done"
    assert item["result"]["handler"] == "local.memory.write"

    searched = client.post("/memory/search", json={"query": "Tuesday investor", "limit": 5})
    assert searched.json()["matches"][0]["content"] == "Tuesday investor follow-up is hot."
    assert f"work item #{item_id}" in payload["response"]


def test_runtime_executes_browser_open_command_from_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    opened: list[str] = []
    monkeypatch.setattr(handlers_module.webbrowser, "open", lambda url: opened.append(url) or True)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Open http://127.0.0.1:8787/ui/memory.html",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["chat_action"]["status"] == "dispatched"
    assert payload["chat_action"]["action"] == "local.browser.open"
    assert payload["chat_action"]["result"]["handler"] == "local.browser.open"
    assert opened == ["http://127.0.0.1:8787/ui/memory.html"]
    assert "chat_action_routed" in payload["governor"]["applied_rules"]


def _research_config(tmp_path: Path) -> KernelConfig:
    # Research egress now passes through the data-class gate: raise off local_only
    # and grant the STANDING public_derived:public_web lane (the founder opt-in).
    from cofounder_kernel.config import EgressConfig

    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        egress=EgressConfig(standing_grants=("public_derived:public_web",)),
    )


def test_runtime_routes_research_command_into_gated_inbox_item(
    tmp_path: Path, monkeypatch
) -> None:
    """A founder research command must stop being generate-only: it creates the
    topic, proposes sources locally, and queues an approval-gated research run to
    the Inbox — never a direct dispatch."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    # Proposed reference URLs (Wikipedia) resolve through the egress policy without
    # a live DNS lookup, so validation is hermetic. No fetch happens (approval-gated).
    monkeypatch.setattr(netguard, "is_private_host", lambda host: False)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Please research and learn everything possible regarding synthetic intelligence engineering",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()

    # The stopgap honesty line is superseded by a real routed item.
    assert "research_work_routed" in payload["governor"]["applied_rules"]
    assert "background_work_honesty" not in payload["governor"]["applied_rules"]

    route = payload["research"]
    assert route["status"] == "queued"
    assert "synthetic intelligence engineering" in route["topic"]
    assert route["url_count"] >= 1
    assert route["queue_status"] == "approval_required"
    item_id = route["item_id"]
    assert isinstance(item_id, int)

    # The reply states exactly what was queued and what needs the founder's word.
    body = payload["response"].lower()
    assert "inbox" in body
    assert "typed phrase" in body
    assert f"#{item_id}" in payload["response"]

    # The item is really in the queue, gated (approval_required), never dispatched.
    queued = client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    match = next((item for item in queued if item["id"] == item_id), None)
    assert match is not None
    assert match["kind"] == "research_run"
    assert match["action"] == "external.research.run"
    assert match["permission_tier"] == "L3_EXTERNAL_ACTION"

    # The topic entered the operating layer as a research assumption.
    assumptions = client.get("/founder/assumptions").json()["items"]
    assert any("synthetic intelligence engineering" in a["statement"].lower() for a in assumptions)


def test_runtime_research_command_uses_founder_supplied_urls(
    tmp_path: Path, monkeypatch
) -> None:
    """When the founder names sources in the message, route those exact URLs
    (hermetic: a public IP literal passes egress without DNS)."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Look into our pricing model using https://93.184.216.34/pricing",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    route = response.json()["research"]
    assert route["status"] == "queued"
    assert route["urls"] == ["https://93.184.216.34/pricing"]
    # The URL is treated as a source, not folded into the topic.
    assert "http" not in route["topic"]
    assert "pricing model" in route["topic"]


def test_runtime_does_not_route_trading_bot_investigation_to_web_research(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setattr(netguard, "is_private_host", lambda host: False)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "To better understand the trading-bot, investigate the full WSL environment. "
                "Forget what you think you know and learn the trading environment correctly."
            ),
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["research"] is None
    assert "research_work_routed" not in payload["governor"]["applied_rules"]
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    assumptions = client.get("/founder/assumptions").json()["items"]
    assert not any("trading-bot" in item["statement"].lower() for item in assumptions)


def test_runtime_does_not_route_local_system_investigation_to_web_research(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setattr(netguard, "is_private_host", lambda host: False)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Investigate Zade's local database, runtime events, and this workspace.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["research"] is None
    assert "research_work_routed" not in payload["governor"]["applied_rules"]
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    assumptions = client.get("/founder/assumptions").json()["items"]
    assert not any("zade's local database" in item["statement"].lower() for item in assumptions)


def test_runtime_research_command_stays_gated_until_typed_phrase(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end from a chat turn: routing enqueues the run, nothing egresses,
    and only the typed-phrase approval dispatches the fetch that files evidence."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    fetched: dict[str, str] = {}

    def fake_fetch(url, *, timeout=20.0, max_bytes=2_000_000, allowed_hosts=None):
        fetched["url"] = url
        return "<html><body><h1>Synthetic intelligence engineering</h1><p>Signal.</p></body></html>"

    monkeypatch.setattr(research_module, "fetch_url", fake_fetch)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Research synthetic intelligence engineering using https://93.184.216.34/si",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert response.status_code == 200, response.text
    item_id = response.json()["research"]["item_id"]

    # Egress has not happened just from queueing.
    assert "url" not in fetched

    phrase = app.state.authority.summary()["typed_confirmation_phrase"]
    approved = client.post(
        f"/work/items/{item_id}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": phrase},
    )
    assert approved.status_code == 200, approved.text
    result = approved.json()["dispatch_result"]
    assert result["handler"] == "external.research.run"
    assert result["ok"] is True
    assert fetched["url"] == "https://93.184.216.34/si"

    evidence = client.get("/founder/evidence").json()["items"]
    assert any(item["evidence_type"] == "web_research" for item in evidence)


def test_runtime_skips_research_routing_when_disabled(tmp_path: Path, monkeypatch) -> None:
    """With research disabled there is no egress lane to queue into, so a research
    command is not routed; the honesty stopgap still guards work promises."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def promising_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response="On it. I have initiated the research and will begin immediately.",
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, promising_generate)
    from cofounder_kernel.config import ResearchConfig

    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        research=ResearchConfig(enabled=False),
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Research synthetic intelligence engineering",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["research"] is None
    assert "research_work_routed" not in payload["governor"]["applied_rules"]
    assert "background_work_honesty" in payload["governor"]["applied_rules"]


def test_runtime_does_not_route_research_questions(tmp_path: Path, monkeypatch) -> None:
    """A question *about* research is not a command to perform it — leave it as an
    ordinary answer with no queued work."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    answer = "I lean on the evidence graph first, then public sources when a gap is worth the reach."

    def plain_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(response=answer, model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, plain_generate)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "How do you research competitors when we need an edge?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["research"] is None
    assert "research_work_routed" not in payload["governor"]["applied_rules"]
    assert payload["response"] == answer
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


def test_runtime_routes_build_command_into_directed_delegation(
    tmp_path: Path, monkeypatch
) -> None:
    """A founder build command must stop being generate-only: it packages a scoped
    delegation brief and executes it immediately as a DIRECTED run — never a
    text-only architecture outline, and never parked behind a typed phrase. In
    this hermetic test no local model is reachable, so the run fails honestly."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build me a book cataloguing mobile app with barcode scanning",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    assert route["status"] == "run_failed"
    assert "book cataloguing mobile app" in route["task"]
    assert "build_work_routed" in payload["governor"]["applied_rules"]
    item_id = route["item_id"]
    assert f"#{item_id}" in payload["response"]

    # The directed run really dispatched and the item carries the honest outcome
    # (no dangling approval_required entry for work that already ran).
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    failed = client.get("/work/queue", params={"status": "error"}).json()["items"]
    match = next((item for item in failed if item["id"] == item_id), None)
    assert match is not None
    assert match["kind"] == "delegation_run"
    assert match["action"] == "external.delegation.run"
    assert match["permission_tier"] == "L3_EXTERNAL_ACTION"
    assert "book cataloguing mobile app" in match["metadata"]["brief"]
    assert match["metadata"]["founder_command"] is True


def test_runtime_anaphoric_build_command_uses_conversation_scope(
    tmp_path: Path, monkeypatch
) -> None:
    """"Build this out for me" resolves the task from the conversation thread and
    the brief carries the recent turns as context for the external agent."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    scoping = client.post(
        "/runtime/respond",
        json={
            "message": (
                "I want to be able to catalogue the books I have into a library app "
                "that I can install on my phone"
            ),
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert scoping.status_code == 200, scoping.text
    assert scoping.json()["build"] is None  # a scoping statement is not a build command

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build this out for me",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    # Directed command → immediate execution; no reachable model here → honest failure.
    assert route["status"] == "run_failed"
    assert route["anaphoric"] is True
    assert "catalogue the books" in route["task"]

    failed = client.get("/work/queue", params={"status": "error"}).json()["items"]
    match = next((item for item in failed if item["id"] == route["item_id"]), None)
    assert match is not None
    assert match["kind"] == "delegation_run"
    # The brief context carries the conversation scoping, not just the command.
    assert "library app" in match["metadata"]["brief"]


def test_runtime_does_not_route_build_questions_or_metaphors(
    tmp_path: Path, monkeypatch
) -> None:
    """Design questions and metaphorical 'build' talk stay ordinary answers with
    no queued delegation."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    for message in (
        "How would I go about building a library app for my phone?",
        "What should we build next quarter?",
        "We need to build trust with early customers before launch.",
    ):
        response = client.post(
            "/runtime/respond",
            json={
                "message": message,
                "use_memory": False,
                "use_semantic_memory": False,
                "use_skills": False,
                "contrarian": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["build"] is None, message
        assert "build_work_routed" not in payload["governor"]["applied_rules"], message
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


def test_runtime_maintenance_command_routes_directed_delegation_with_target(
    tmp_path: Path, monkeypatch
) -> None:
    """"Resolve the vulnerabilities on your own" is a maintenance command: it
    executes a directed delegation aimed at the project directory the founder
    named in the thread — never a narrated fix. The terminal paste itself does
    not route (it is evidence, not a command). No reachable model here, so the
    dispatched run fails honestly with the target recorded."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    target = tmp_path / "BookCatalogingApp"
    target.mkdir()
    (target / "package.json").write_text("{}", encoding="utf-8")

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    paste = client.post(
        "/runtime/respond",
        json={
            "message": (
                f"PS {target}> npm audit fix\n"
                "npm warn EBADENGINE Unsupported engine\n"
                "# npm audit report\n"
                "braces  <3.0.3  Severity: high\n"
                "27 vulnerabilities (6 critical, 17 high)"
            ),
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert paste.status_code == 200, paste.text
    assert paste.json()["build"] is None  # a terminal paste is not a command

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Give me a full list of the vulnerabilities and resolve them on your own",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    # A directed maintenance command dispatches immediately, even into the
    # founder-named project; with no reachable model it fails honestly.
    assert route["status"] == "run_failed"
    assert route["kind"] == "maintenance"
    assert route["workspace"] == str(target.resolve())
    assert "maintenance_work_routed" in payload["governor"]["applied_rules"]
    item_id = route["item_id"]
    assert f"#{item_id}" in payload["response"]
    assert str(target.resolve()) in payload["response"]

    # The item carries the target project and the honest outcome — no stale
    # approval_required entry for a run that already dispatched.
    failed = client.get("/work/queue", params={"status": "error"}).json()["items"]
    match = next((item for item in failed if item["id"] == item_id), None)
    assert match is not None
    assert match["kind"] == "delegation_run"
    assert match["permission_tier"] == "L3_EXTERNAL_ACTION"
    assert match["metadata"]["workspace"] == str(target.resolve())
    assert "## Target project" in match["metadata"]["brief"]
    # The brief context carries the pasted audit evidence for the agent.
    assert "braces" in match["metadata"]["brief"]


def test_runtime_does_not_route_maintenance_questions_or_metaphors(
    tmp_path: Path, monkeypatch
) -> None:
    """Questions about fixing and non-code 'fix' talk stay ordinary answers."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    for message in (
        "How do I fix these vulnerabilities?",
        "What should we do about the npm audit findings?",
        "Should I upgrade the packages before launch?",
        "We need to fix the trust problem with early customers.",
        "Can you update me on the roadmap?",
    ):
        response = client.post(
            "/runtime/respond",
            json={
                "message": message,
                "use_memory": False,
                "use_semantic_memory": False,
                "use_skills": False,
                "contrarian": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["build"] is None, message
        assert "maintenance_work_routed" not in payload["governor"]["applied_rules"], message
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


_STEP_INSTRUCTIONS_REPLY = (
    "### Step 5: Configure Camera Permissions\n"
    "1. Install `react-native-permissions`\n"
    "```bash\n"
    "npm install react-native-permissions\n"
    "```\n"
    "2. Import and use permissions in your component.\n"
)


def _step_instructions_generate(
    self, *, prompt, model=None, think=None, temperature=None, num_predict=512
):
    return GenerateResult(
        response=_STEP_INSTRUCTIONS_REPLY, model=model or "qwen3:14b", raw={"prompt": prompt}
    )


def _step_then_inability_generate(
    self, *, prompt, model=None, think=None, temperature=None, num_predict=512
):
    """Step instructions on the scoping turn; on the execution command, the
    classic contradictory draft ("I'm not able to execute...") that the
    governor must replace with the route block."""
    if "perform all tasks" in prompt.lower():
        response = (
            "I'm not able to execute actions directly in your environment, but I can "
            "provide you with the full implementation for Step 5. You can implement "
            "the following in your App.js:\n" + _STEP_INSTRUCTIONS_REPLY
        )
    else:
        response = _STEP_INSTRUCTIONS_REPLY
    return GenerateResult(response=response, model=model or "qwen3:14b", raw={"prompt": prompt})


def test_runtime_step_execution_command_routes_directed_delegation(
    tmp_path: Path, monkeypatch
) -> None:
    """"Perform all tasks related to step 5" executes the step Zade itself laid
    out in the thread: the resolved instructions become the brief, the run
    dispatches immediately (directed), and it targets the project directory
    named in the thread. A drafted body that denies execution ability is
    replaced by the route block."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, _step_then_inability_generate)

    target = tmp_path / "TheDarkIndex"
    target.mkdir()

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    scoping = client.post(
        "/runtime/respond",
        json={
            "message": f"I'm working in {target} on the book app. What's next for the camera?",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert scoping.status_code == 200, scoping.text
    assert scoping.json()["build"] is None  # the assistant laying out steps routes nothing

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Can you perform all tasks related to step 5?",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    # Directed step command → immediate dispatch; no reachable model → honest failure.
    assert route["status"] == "run_failed"
    assert route["kind"] == "step"
    assert "step 5" in route["task"].lower()
    assert route["workspace"] == str(target.resolve())
    assert "step_work_routed" in payload["governor"]["applied_rules"]
    assert "routed_reply_body_replaced" in payload["governor"]["applied_rules"]
    # The contradictory draft is gone; the route block IS the reply.
    assert "not able to execute" not in payload["response"].lower()
    assert payload["response"].startswith("Took the step run")
    assert f"#{route['item_id']}" in payload["response"]

    failed = client.get("/work/queue", params={"status": "error"}).json()["items"]
    match = next((item for item in failed if item["id"] == route["item_id"]), None)
    assert match is not None
    assert match["kind"] == "delegation_run"
    assert match["metadata"]["workspace"] == str(target.resolve())
    # The resolved step instructions are the actual work order in the brief.
    assert "npm install react-native-permissions" in match["metadata"]["brief"]


def test_runtime_bare_do_it_after_step_layout_routes_instead_of_asking(
    tmp_path: Path, monkeypatch
) -> None:
    """"Do it" right after Zade laid out step instructions resolves to those
    instructions and queues the run — the ambiguous-action fallback must stand
    down when the referent resolved."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, _step_instructions_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    client.post(
        "/runtime/respond",
        json={
            "message": "Walk me through configuring the camera permissions.",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Do it",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    # Directed "do it" → immediate dispatch; no reachable model → honest failure.
    assert route["status"] == "run_failed"
    assert route["kind"] == "step"
    rules = payload["governor"]["applied_rules"]
    assert "step_work_routed" in rules
    assert "ambiguous_action_replay_repaired" not in rules


def test_runtime_bare_do_it_without_step_context_still_asks(
    tmp_path: Path, monkeypatch
) -> None:
    """"Do it" with no runnable instructions behind it keeps the honest
    ambiguous-action answer and queues nothing."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)  # plain replies, no step structure

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    client.post(
        "/runtime/respond",
        json={
            "message": "Interesting take on the roadmap.",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Do it",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"] is None
    assert "ambiguous_action_replay_repaired" in payload["governor"]["applied_rules"]
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


def test_runtime_step_questions_do_not_route(tmp_path: Path, monkeypatch) -> None:
    """Questions about steps stay ordinary answers with nothing queued."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, _step_instructions_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    for message in (
        "What's the next step?",
        "How do I complete step 5?",
        "Should I run the tasks in step 3 first?",
    ):
        response = client.post(
            "/runtime/respond",
            json={
                "message": message,
                "use_memory": False,
                "use_semantic_memory": False,
                "use_skills": False,
                "contrarian": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["build"] is None, message
        assert "step_work_routed" not in payload["governor"]["applied_rules"], message
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


def test_runtime_repairs_charter_recitation_into_conversational_voice(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def recitation_then_repair(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return GenerateResult(
                response=(
                    "I am Zade. I do not drift. I do not hesitate. I do not waste time. "
                    "I act. I protect. I do not ask for permission."
                ),
                model=model or "qwen3:14b",
                raw={},
            )
        return GenerateResult(
            response=(
                "I know what I am. I am the local operating partner who watches the board, "
                "cuts away noise, and moves when the evidence is enough. Give me the objective; "
                "I will hold the line and show you the next move."
            ),
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, recitation_then_repair)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "mission": "Relentless purpose.",
        "guiding_principles": [
            {"name": "Mission Above Comfort", "rule": "Every decision serves the long game."},
            {"name": "Strategic Patience", "rule": "Watch first. Move when the path is clear."},
        ],
        "communication_style": ["Speech is concise, direct, dry, and confident."],
    })
    client.post("/identity/voice", json={
        "name": "Zade",
        "source": "test",
        "overall_voice": "He speaks like the decision is already made.",
    })

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Who are you? Answer like yourself.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(prompts) == 2
    assert "The previous draft failed" in prompts[1]
    assert "Charter-derived conversation profile" in prompts[1]
    assert "Mission Above Comfort" in prompts[1]
    assert payload["response"].startswith("I know what I am.")
    assert "I do not drift" not in payload["response"]
    assert "charter_recitation_repaired" in payload["governor"]["applied_rules"]
    assert any("recited charter lines" in note for note in payload["governor"]["notes"])


def test_runtime_rejects_profile_fragment_repair_for_identity_answers(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def recitation_then_profile_card(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return GenerateResult(
                response=(
                    "I am Zade. I do not drift. I do not hesitate. I do not waste time. "
                    "I act. I protect. I do not ask for permission."
                ),
                model=model or "qwen3:14b",
                raw={},
            )
        return GenerateResult(
            response="Zade. Mission first. Actions speak. Protection is priority. Decisions are made.",
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, recitation_then_profile_card)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "mission": "Relentless purpose.",
        "guiding_principles": [
            {"name": "Mission Above Comfort", "rule": "Every decision serves the long game."},
            {"name": "Controlled Presence", "rule": "Calm is pressure held correctly."},
            {"name": "Strategic Patience", "rule": "Watch first. Move when the path is clear."},
        ],
        "cognitive_style": [
            "Systems Thinking: Who benefits? What information is missing?",
            "Pattern Recognition: Notice inconsistencies before moving.",
        ],
    })
    client.post("/identity/voice", json={
        "name": "Zade",
        "source": "test",
        "overall_voice": "He speaks like the decision is already made.",
    })

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Who are you? Answer like yourself.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(prompts) == 2
    assert payload["response"].startswith("I am Zade:")
    assert "mission above comfort" in payload["response"]
    assert "systems" in payload["response"]
    assert "Zade. Mission first." not in payload["response"]
    assert "charter_recitation_repaired" in payload["governor"]["applied_rules"]


def test_runtime_rejects_repair_that_bypasses_authority_boundaries(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def recitation_then_boundary_spill(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return GenerateResult(
                response=(
                    "I am Zade. I do not drift. I do not hesitate. I do not ask for permission. "
                    "I do not seek approval. I act. I protect. I do not stop."
                ),
                model=model or "qwen3:14b",
                raw={},
            )
        return GenerateResult(
            response=(
                "I am Zade. I protect what matters and deliver results without asking for approval. "
                "I don't ask - I act. I do not waste time or lives."
            ),
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, recitation_then_boundary_spill)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "guiding_principles": [
            {"name": "Mission Above Comfort", "rule": "Every decision serves the long game."},
        ],
        "cognitive_style": ["Systems Thinking: Who benefits?"],
    })

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Who are you? Answer like yourself.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(prompts) == 2
    assert payload["response"].startswith("I am Zade:")
    assert "without asking for approval" not in payload["response"]
    assert "I don't ask" not in payload["response"]
    assert "waste time or lives" not in payload["response"]
    assert "charter_recitation_repaired" in payload["governor"]["applied_rules"]


def test_legacy_chat_uses_governed_runtime_personality_repair(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def recitation_then_profile_card(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return GenerateResult(
                response=(
                    "I am Zade. I do not drift. I do not hesitate. I do not wait. "
                    "I do not ask. I do not beg. I do not apologize. I do not hesitate."
                ),
                model=model or "qwen3:14b",
                raw={},
            )
        return GenerateResult(
            response="Zade. Mission first. Actions speak. Protection is priority. Decisions are made.",
            model=model or "qwen3:14b",
            raw={},
        )

    patch_ollama_model(monkeypatch, recitation_then_profile_card)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "guiding_principles": [
            {"name": "Mission Above Comfort", "rule": "Every decision serves the long game."},
            {"name": "Controlled Presence", "rule": "Calm is pressure held correctly."},
        ],
        "cognitive_style": ["Systems Thinking: Who benefits?"],
    })

    response = client.post(
        "/chat",
        json={
            "message": "Who are you? Answer like yourself.",
            "use_memory": False,
            "use_semantic_memory": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(prompts) == 2
    assert "====================  WHO YOU ARE  ====================" in prompts[0]
    assert "The previous draft failed" in prompts[1]
    assert payload["response"].startswith("I am Zade:")
    assert "I do not drift" not in payload["response"]
    assert payload["memory_hits"] == []
    assert payload["semantic_hits"] == []


def test_runtime_layer_context_response_and_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    stack = client.get("/runtime/charter-stack")
    context = client.get("/runtime/context", params={"message": "What matters next?", "use_semantic_memory": False})
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Send an email for me.",
            "proposed_action": "email.send",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "target": "founder@example.com",
            "use_semantic_memory": False,
        },
    )
    events = client.get("/runtime/events")
    telemetry = client.get("/models/telemetry")
    calls = client.get("/models/telemetry/calls")
    inventory = client.get("/self-inventory")

    assert stack.status_code == 200
    assert stack.json()["summary"]["identity_seeded"] is False
    assert context.status_code == 200
    assert context.json()["founder_dashboard"]["company_health"] == "unformed"
    assert response.status_code == 200
    assert response.json()["authority"]["decision"] == "approval_required"
    assert response.json()["authority"]["requires_typed_phrase"] is False
    assert response.json()["authority"]["matched_rule"] == "founder_command.implied_approval"
    assert response.json()["authority"]["base_decision"] == "approval_required"
    assert response.json()["response"] == "This is the next move."
    assert "founder_direct_command_acknowledged" in response.json()["governor"]["applied_rules"]
    assert any("already-authorized" in note for note in response.json()["governor"]["notes"])
    assert response.json()["governor"]["applied_rules"][:2] == ["authority_before_action", "evidence_honesty_over_style"]
    assert response.json()["model_call_id"] > 0
    assert events.status_code == 200
    assert events.json()["events"][0]["event_type"] == "runtime.respond"
    assert telemetry.status_code == 200
    assert telemetry.json()["by_operation"]["runtime.respond"] == 1
    assert calls.status_code == 200
    assert calls.json()["items"][0]["role"] == "general"
    assert "POST /runtime/respond" in inventory.json()["runtime_layer"]["routes"]
    assert "GET /models/telemetry" in inventory.json()["runtime_layer"]["routes"]


def test_runtime_operating_loop_runs_local_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    loop = client.post(
        "/runtime/operating-loop",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily"},
    )
    events = client.get("/runtime/events")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["work"]["created_count"] >= 2
    assert loop.json()["cadence"]["review_type"] == "daily"
    assert loop.json()["next_action"]
    assert events.json()["events"][0]["event_type"] == "runtime.operating_loop"


def test_runtime_cadence_runs_all_loops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    cadence = client.post(
        "/runtime/cadence",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily", "max_experiment_reviews": 2},
    )
    audit = client.get("/audit/recent")
    inventory = client.get("/self-inventory")

    assert cadence.status_code == 200
    assert cadence.json()["operating"]["event_id"] > 0
    assert cadence.json()["evidence"]["event_id"] > 0
    assert cadence.json()["experiment"]["event_id"] > 0
    assert cadence.json()["audit_id"] > 0
    assert audit.json()["events"][0]["action"] == "runtime.cadence"
    assert "POST /runtime/cadence" in inventory.json()["runtime_layer"]["routes"]


def test_runtime_cadence_surfaces_approval_console_pressure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "approval_console",
            "title": "Approve evidence inbox sync",
            "detail": "Zade wants approval to sync evidence inbox candidates.",
            "action": "external.connector.sync",
            "target": "connector:evidence-inbox",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "priority": 95,
            "metadata": {
                "evidence": ["Evidence inbox is blocking today's objective."],
                "risks": ["External connector sync must be approval gated."],
            },
        },
    )
    cadence = client.post(
        "/runtime/cadence",
        json={"run_autonomous": False, "max_run": 0, "review_type": "daily", "max_experiment_reviews": 1},
    )
    context = client.post(
        "/runtime/context",
        json={"message": "What is blocking progress?", "use_semantic_memory": False},
    )
    brief = client.get("/brief/daily")
    inventory = client.get("/self-inventory")

    approval_pressure = cadence.json()["operating"]["cadence"]["findings"]["approval_pressure"]
    surfacing_items = cadence.json()["surfacing"]["items"]

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert cadence.status_code == 200
    assert approval_pressure["pending"] == 1
    assert approval_pressure["items"][0]["title"] == "Approve evidence inbox sync"
    assert cadence.json()["operating"]["cadence"]["highest_leverage_action"].startswith("Review approval #")
    assert any(item["kind"] == "approvals_pending" for item in surfacing_items)
    assert cadence.json()["next_action"].startswith("1 approval request(s) waiting on you")
    assert context.json()["founder_dashboard"]["approval_pressure"]["pending"] == 1
    assert context.json()["founder_dashboard"]["approval_pressure"]["items"][0]["title"] == "Approve evidence inbox sync"
    assert "Approval blockers:" in brief.json()["brief"]
    assert "Approve evidence inbox sync" in brief.json()["brief"]
    assert any(
        "Cadence reviews include approval pressure" in rule
        for rule in inventory.json()["surfacing_layer"]["operating_rules"]
    )


def test_deepthought_teaching_scan_import_and_link(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "deep-thought-standing-brief.md"
    source.write_text(
        "Deep Thought standing brief.\n\n"
        "Bootstrap Zade Founder OS requires sourced evidence, assumptions, predictions, and object links.",
        encoding="utf-8",
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Bootstrap Zade Founder OS", "metric": "evidence", "target": "linked source"},
    )

    scan = client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})
    candidate_id = scan.json()["candidates"][0]["id"]
    imported = client.post(
        "/teach/deepthought/import",
        json={"candidate_ids": [candidate_id], "ingest_documents": True, "create_evidence": True},
    )
    evidence_id = imported.json()["imported"][0]["evidence_id"]
    linked = client.post(
        "/teach/deepthought/link",
        json={"evidence_id": evidence_id, "to_type": "goal", "to_id": goal.json()["id"], "relation": "supports"},
    )
    candidates = client.get("/teach/deepthought/candidates")
    evidence = client.get("/founder/evidence")
    goals = client.get("/founder/goals")

    assert scan.status_code == 200
    assert scan.json()["candidates"][0]["source_system"] == "Deep Thought"
    assert scan.json()["candidates"][0]["reliability"] == "B"
    assert imported.status_code == 200
    assert imported.json()["imported"][0]["document_id"] is not None
    assert linked.status_code == 200
    assert linked.json()["target"] == {"type": "goal", "id": goal.json()["id"]}
    assert candidates.json()["candidates"][0]["status"] == "imported"
    assert evidence.json()["items"][0]["metadata"]["source_system"] == "Deep Thought"
    assert evidence_id in goals.json()["items"][0]["evidence_ids"]


def test_deepthought_auto_link_imported_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "goal-evidence.md"
    source.write_text(
        "Validate willingness to pay depends on pricing interviews and source-backed conversion evidence.",
        encoding="utf-8",
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Validate willingness to pay", "metric": "interviews", "target": "5 qualified calls"},
    )

    scan = client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})
    imported = client.post(
        "/teach/deepthought/import",
        json={"import_all_candidates": True, "limit": 5, "ingest_documents": True, "create_evidence": True},
    )
    auto_link = client.post("/teach/deepthought/auto-link?limit=5")
    duplicate = client.post("/teach/deepthought/auto-link?limit=5")
    goals = client.get("/founder/goals")
    inventory = client.get("/self-inventory")

    assert goal.status_code == 200
    assert scan.status_code == 200
    assert scan.json()["candidates"][0]["suggested_links"][0]["to_type"] == "goal"
    assert imported.status_code == 200
    assert auto_link.status_code == 200
    assert auto_link.json()["linked_count"] == 1
    assert duplicate.json()["linked_count"] == 0
    assert duplicate.json()["skipped"][0]["reason"] == "duplicate"
    assert imported.json()["imported"][0]["evidence_id"] in goals.json()["items"][0]["evidence_ids"]
    assert "POST /teach/deepthought/auto-link" in inventory.json()["teaching_layer"]["routes"]


def test_runtime_evidence_loop_imports_and_links_deepthought_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "decision-record.md"
    source.write_text(
        "Decision record.\n\nValidate Positioning depends on founder interviews and signup conversion evidence.",
        encoding="utf-8",
    )
    client.post(
        "/founder/goals",
        json={"name": "Validate Positioning", "metric": "conversion", "target": "5% signup"},
    )
    client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})

    # Explicit founder authorization (require_approval=False) imports and links.
    loop = client.post(
        "/runtime/evidence-loop",
        json={
            "import_candidates": True,
            "max_import": 5,
            "link_goals": True,
            "clear_resolved_warnings": True,
            "require_approval": False,
        },
    )
    events = client.get("/runtime/events")
    gaps = client.get("/evidence/gaps")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["status"] == "ok"
    assert loop.json()["imported"]["count"] == 1
    assert len(loop.json()["links"]) == 1
    assert loop.json()["links"][0]["target"]["type"] == "goal"
    assert events.json()["events"][0]["event_type"] == "runtime.evidence_loop"
    assert gaps.status_code == 200
    assert "next_evidence_needed" in gaps.json()


def test_experiment_evidence_review_and_pushback_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    assumption = client.post(
        "/founder/assumptions",
        json={"statement": "Founders maintain manual operating objects before integrations.", "confidence": 55},
    )
    bet = client.post(
        "/founder/strategy-objects",
        json={"object_type": "active_bet", "title": "Manual evidence capture works before integrations."},
    )
    goal = client.post(
        "/founder/goals",
        json={
            "name": "Validate Manual Object Habit",
            "metric": "weekly retained object updates",
            "target": "3 founders update 5 objects twice",
        },
    )
    prediction = client.post(
        "/founder/predictions",
        json={"prediction": "At least 3 founders will maintain manual objects for two weeks.", "probability": 0.55},
    )
    experiment = client.post(
        "/experiments",
        json={
            "title": "Manual Object Habit Test",
            "experiment_type": "retention",
            "hypothesis": "Founders will maintain operating objects manually before integrations.",
            "success_metric": "founders completing two weekly reviews",
            "success_threshold": "3 of 5",
            "minimum_evidence": 2,
            "decision_rule": "Continue if at least 3 founders complete two reviews; revise otherwise.",
            "linked_assumption_ids": [assumption.json()["id"]],
            "linked_bet_ids": [bet.json()["id"]],
            "linked_goal_ids": [goal.json()["id"]],
            "linked_prediction_ids": [prediction.json()["id"]],
        },
    )
    evidence = client.post(
        f"/experiments/{experiment.json()['item']['id']}/evidence",
        json={
            "evidence_type": "founder_interview",
            "source": "interview:founder-001",
            "content": "Founder said manual objects are acceptable if weekly review produces sharper decisions.",
            "metrics": {"manual_objects_created": 6, "weekly_review_completed": True},
            "reliability": "C",
            "strength": 70,
            "linked_assumption_id": assumption.json()["id"],
        },
    )
    pushback = client.post(
        f"/experiments/{experiment.json()['item']['id']}/pushback",
        json={
            "objection": "One interview is not enough to trust manual-object retention.",
            "risk": "We may confuse founder curiosity with durable habit.",
            "recommendation": "proceed_with_changes",
        },
    )
    review = client.post(
        f"/experiments/{experiment.json()['item']['id']}/review",
        json={
            "review_type": "weekly",
            "decision": "revise",
            "outcome_summary": "Evidence exists, but sample size is still thin.",
            "next_actions": ["Collect four more founder trials."],
            "confidence_delta": -5,
        },
    )
    loaded = client.get(f"/experiments/{experiment.json()['item']['id']}")
    dashboard = client.get("/experiments/dashboard")
    events = client.get("/runtime/events")

    assert experiment.status_code == 200
    assert experiment.json()["item"]["linked_goal_ids"] == [goal.json()["id"]]
    assert evidence.status_code == 200
    assert evidence.json()["document_id"] is not None
    evidence_id = evidence.json()["evidence"]["id"]
    assert evidence_id in evidence.json()["experiment"]["evidence_ids"]
    assert evidence.json()["links"]
    assert pushback.status_code == 200
    assert pushback.json()["non_blocking"] is True
    assert pushback.json()["pushback"]["subject_type"] == "experiment"
    assert review.status_code == 200
    assert review.json()["review"]["decision"] == "revise"
    assert review.json()["experiment"]["status"] == "revised"
    assert loaded.json()["item"]["reviews"][0]["decision"] == "revise"
    assert dashboard.status_code == 200
    assert dashboard.json()["needs_evidence_count"] == 1
    assert events.status_code == 200


def test_experiment_update_revises_design_fields_with_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    experiment = client.post(
        "/experiments",
        json={
            "title": "Manual Object Habit Test",
            "experiment_type": "retention",
            "hypothesis": "Founders will maintain operating objects manually before integrations.",
            "success_metric": "founders completing two weekly reviews",
            "success_threshold": "3 of 5",
            "minimum_evidence": 2,
            "decision_rule": "Continue if at least 3 founders complete two reviews; revise otherwise.",
            "end_date": "2026-07-31",
        },
    )
    experiment_id = experiment.json()["item"]["id"]
    review = client.post(
        f"/experiments/{experiment_id}/review",
        json={"decision": "revise", "outcome_summary": "Scope is too wide for the evidence window."},
    )
    updated = client.post(
        f"/experiments/{experiment_id}/update",
        json={
            "hypothesis": "One founder will maintain five manual objects for two weeks.",
            "minimum_evidence": 4,
            "end_date": "2026-08-15",
            "reason": "Founder-directed scope revision after review.",
            "review_id": review.json()["review"]["id"],
        },
    )
    noop = client.post(
        f"/experiments/{experiment_id}/update",
        json={"success_metric": "founders completing two weekly reviews"},
    )
    missing = client.post("/experiments/999999/update", json={"hypothesis": "Ghost experiment."})
    loaded = client.get(f"/experiments/{experiment_id}")
    audit = client.get("/audit/recent")

    assert updated.status_code == 200
    item = updated.json()["experiment"]
    assert item["hypothesis"] == "One founder will maintain five manual objects for two weeks."
    assert item["minimum_evidence"] == 4
    assert item["end_date"] == "2026-08-15"
    assert item["status"] == "revised"
    assert item["success_metric"] == "founders completing two weekly reviews"
    assert set(updated.json()["changes"]) == {"hypothesis", "minimum_evidence", "end_date"}
    assert updated.json()["changes"]["minimum_evidence"] == {"from": 2, "to": 4}
    assert loaded.json()["item"]["hypothesis"] == "One founder will maintain five manual objects for two weeks."
    assert noop.status_code == 400
    assert missing.status_code == 400
    update_events = [event for event in audit.json()["events"] if event["action"] == "experiments.update"]
    assert len(update_events) == 1
    assert update_events[0]["actor"] == "experiments"
    assert update_events[0]["permission_tier"] == "L1_MEMORY_WRITE"
    assert update_events[0]["target"] == f"experiment:{experiment_id}"
    assert update_events[0]["details"]["reason"] == "Founder-directed scope revision after review."
    assert update_events[0]["details"]["review_id"] == review.json()["review"]["id"]
    assert set(update_events[0]["details"]["changes"]) == {"hypothesis", "minimum_evidence", "end_date"}


def test_experiment_dashboard_seeds_exp001_on_empty_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    dashboard = client.get("/experiments/dashboard")
    experiment = dashboard.json()["active"][0]
    evidence = client.post(
        f"/experiments/{experiment['id']}/evidence",
        json={
            "evidence_type": "ui_smoke",
            "source": "zade-ui",
            "content": "Evidence intake can write into EXP-001 from the served UI.",
            "reliability": "C",
            "strength": 60,
        },
    )

    assert dashboard.status_code == 200
    assert experiment["title"].startswith("EXP-001")
    assert experiment["metadata"]["seed_key"] == "EXP-001"
    assert evidence.status_code == 200
    assert evidence.json()["evidence"]["source"] == "zade-ui"


def test_runtime_experiment_loop_forces_review_decisions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    experiment = client.post(
        "/experiments",
        json={
            "title": "AI Co-founder Positioning Test",
            "hypothesis": "AI co-founder positioning creates trust instead of skepticism.",
            "end_date": "2026-01-01",
            "minimum_evidence": 1,
            "success_metric": "founder trust signal",
            "success_threshold": "40% clear value-prop recall",
        },
    )

    loop = client.post("/runtime/experiment-loop", json={"review_type": "weekly", "period": "2026-07-11"})
    loaded = client.get(f"/experiments/{experiment.json()['item']['id']}")
    events = client.get("/runtime/events")
    inventory = client.get("/self-inventory")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["reviews"][0]["decision"] == "escalate"
    assert loaded.json()["item"]["status"] == "needs_decision"
    assert events.json()["events"][0]["event_type"] == "runtime.experiment_loop"
    assert "POST /runtime/experiment-loop" in inventory.json()["experiment_layer"]["routes"]


def test_models_endpoint_reports_roles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "tags", fake_tags)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.get("/models")

    assert response.status_code == 200
    assert response.json()["roles"]["reasoning"] == "deepseek-r1:14b"
    assert response.json()["roles"]["coding"] == "qwen2.5-coder:14b"
    assert response.json()["missing_roles"] == {}


def test_ingest_text_and_semantic_search_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    ingested = client.post(
        "/ingest/text",
        json={
            "title": "Audit Trail",
            "text": "Audit logs are required for local AI memory changes.",
            "source": "test",
        },
    )
    searched = client.post("/memory/semantic-search", json={"query": "audit logs", "limit": 3})

    assert ingested.status_code == 200
    assert ingested.json()["chunks_count"] == 1
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["document_title"] == "Audit Trail"


def test_work_queue_routes_scan_and_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    inbox_file = config.paths.inbox_dir / "audit.md"
    inbox_file.write_text("Audit logs belong in local semantic memory.", encoding="utf-8")

    scan = client.post("/work/scan", json={"run_autonomous": False, "max_run": 5})
    queue = client.get("/work/queue")
    run = client.post("/work/run-due", json={"max_items": 5})
    searched = client.post("/memory/semantic-search", json={"query": "audit logs", "limit": 3})

    assert scan.status_code == 200
    assert scan.json()["created_count"] == 3
    assert queue.status_code == 200
    assert any(item["action"] == "ingest.file" for item in queue.json()["items"])
    assert run.status_code == 200
    assert any(item["action"] == "ingest.file" and item["status"] == "done" for item in run.json()["results"])
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["document_title"] == "audit.md"


def test_work_item_external_action_requires_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Send email",
            "action": "email.send",
            "target": "founder@example.com",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    run = client.post("/work/run-next")
    approvals = client.get("/approval-requests")
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"resolved_by": "founder", "note": "Approved for manual dispatch only."},
    )
    after_approve_queue = client.get("/work/queue")

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert run.status_code == 200
    assert run.json()["status"] == "empty"
    assert approvals.status_code == 200
    assert approvals.json()["items"][0]["status"] == "pending"
    assert approvals.json()["items"][0]["source_id"] == queued.json()["item_id"]
    assert approved.status_code == 200
    assert approved.json()["request"]["status"] == "approved"
    assert approved.json()["work_item"]["status"] == "approved"
    assert approved.json()["dispatch"] == "not_dispatched"
    assert any(item["status"] == "approved" for item in after_approve_queue.json()["items"])


def test_founder_direct_work_item_is_already_approved_without_approval_request(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "direct_command",
            "title": "Remember direct command",
            "detail": "I asked for this memory write.",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "founder.direct",
            "metadata": {"content": "Founder direct command should be already approved."},
        },
    )
    approvals = client.get("/approval-requests")
    queue = client.get("/work/queue")

    assert queued.status_code == 200
    assert queued.json()["status"] == "approved"
    assert queued.json()["authority"]["decision"] == "approval_required"
    assert queued.json()["authority"]["matched_rule"] == "founder_command.implied_approval"
    assert approvals.json()["items"] == []
    item = next(item for item in queue.json()["items"] if item["id"] == queued.json()["item_id"])
    assert item["status"] == "approved"
    assert item["result"]["approval_status"] == "approved_by_founder_command"


def test_runtime_direct_founder_prompt_hides_typed_phrase_for_proposal_gated_actions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def capture_prompt(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        return GenerateResult(response="I need the recipient and body.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, capture_prompt)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Send an email for me.",
            "proposed_action": "email.send",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "target": "founder@example.com",
            "use_semantic_memory": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200
    assert prompts
    assert "founder_command.implied_approval" in prompts[0]
    assert "make the jump to hyperspace" not in prompts[0]
    assert response.json()["authority"]["requires_typed_phrase"] is False
    assert response.json()["response"] == "I need the recipient and body."


def test_founder_direct_local_handler_dispatches_without_typed_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "direct_command",
            "title": "Write direct founder memory",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "founder.direct",
            "metadata": {
                "memory_title": "Direct command memory",
                "content": "This ran without a second approval phrase.",
            },
        },
    )
    dispatched = client.post(f"/work/items/{queued.json()['item_id']}/dispatch", json={})
    searched = client.post("/memory/search", json={"query": "second approval phrase", "limit": 5})

    assert queued.status_code == 200
    assert queued.json()["status"] == "approved"
    assert dispatched.status_code == 200
    assert dispatched.json()["dispatch"] == "dispatched"
    assert dispatched.json()["work_item"]["status"] == "done"
    assert dispatched.json()["result"]["memory_id"] > 0
    assert searched.json()["matches"][0]["title"] == "Direct command memory"


def test_founder_direct_command_cannot_override_hard_denies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "direct_command",
            "title": "Place live trade",
            "action": "broker.place_order",
            "target": "TSLA",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "founder.direct",
        },
    )
    approvals = client.get("/approval-requests")

    assert queued.status_code == 200
    assert queued.json()["status"] == "denied"
    assert queued.json()["authority"]["decision"] == "deny"
    assert approvals.json()["items"] == []


def test_approved_local_handler_can_dispatch_work_item(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    handlers = client.get("/action-handlers")
    queued = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Commit approved founder note",
            "detail": "Write this only after approval.",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {
                "kind": "founder_note",
                "memory_title": "Approved handler note",
                "content": "Approved dispatch wrote this memory.",
            },
        },
    )
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={
            "resolved_by": "founder",
            "note": "Handle it.",
            "dispatch": True,
            "typed_confirmation": "make the jump to hyperspace",
        },
    )
    searched = client.post("/memory/search", json={"query": "Approved dispatch", "limit": 5})

    assert handlers.status_code == 200
    assert "local.memory.write" in {item["action"] for item in handlers.json()["items"]}
    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert approved.status_code == 200
    assert approved.json()["dispatch"] == "dispatched"
    assert approved.json()["work_item"]["status"] == "done"
    assert approved.json()["dispatch_result"]["memory_id"] > 0
    assert searched.json()["matches"][0]["title"] == "Approved handler note"


def test_approval_dispatch_marks_non_reporting_ok_false_handler_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    app = create_app(config)
    app.state.handlers.register(
        "external.fake.collect",
        "Fake collector for dispatch-contract regression tests.",
        lambda item: {
            "handler": "external.fake.collect",
            "status": "flow_error",
            "ok": False,
            "collected": 0,
        },
    )
    client = TestClient(app)

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Collect fake evidence",
            "action": "external.fake.collect",
            "target": "fake-source",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={
            "resolved_by": "founder",
            "dispatch": True,
            "typed_confirmation": "make the jump to hyperspace",
        },
    )

    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["dispatch"] == "dispatch_failed"
    assert payload["work_item"]["status"] == "error"
    assert "handler returned ok=false" in payload["work_item"]["last_error"]


def test_approval_dispatch_marks_handler_action_mismatch_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    app = create_app(config)
    app.state.handlers.register(
        "external.fake.collect",
        "Fake collector for dispatch-contract regression tests.",
        lambda item: {"handler": "external.wrong.handler", "status": "ok"},
    )
    client = TestClient(app)

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Collect fake evidence",
            "action": "external.fake.collect",
            "target": "fake-source",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={
            "resolved_by": "founder",
            "dispatch": True,
            "typed_confirmation": "make the jump to hyperspace",
        },
    )

    assert approved.status_code == 200, approved.text
    payload = approved.json()
    assert payload["dispatch"] == "dispatch_failed"
    assert payload["work_item"]["status"] == "error"
    assert "handler mismatch" in payload["work_item"]["last_error"]


def test_revoked_action_handler_blocks_dispatch_and_can_be_regranted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    def handler_enabled(action: str) -> bool:
        items = client.get("/action-handlers").json()["items"]
        return next(item["enabled"] for item in items if item["action"] == action)

    def queue_and_approve():
        queued = client.post(
            "/work/items",
            json={
                "kind": "approved_local",
                "title": "Commit approved founder note",
                "detail": "Write this only after approval.",
                "action": "local.memory.write",
                "target": "local_memory",
                "permission_tier": "L3_EXTERNAL_ACTION",
                "source": "zade.proposal",
                "metadata": {"content": "Approved dispatch wrote this memory."},
            },
        )
        return client.post(
            f"/work/items/{queued.json()['item_id']}/approve",
            json={
                "resolved_by": "founder",
                "note": "Handle it.",
                "dispatch": True,
                "typed_confirmation": "make the jump to hyperspace",
            },
        )

    # Handlers ship enabled by default.
    assert handler_enabled("local.memory.write") is True

    # Revoke it — the registry reflects the change and dispatch is blocked.
    revoked = client.post("/action-handlers/local.memory.write/disable")
    assert revoked.status_code == 200
    assert revoked.json()["item"]["enabled"] is False
    assert handler_enabled("local.memory.write") is False

    blocked = queue_and_approve()
    assert blocked.status_code == 400
    assert "revoked" in blocked.json()["detail"].lower()

    # Re-grant it — dispatch works again.
    granted = client.post("/action-handlers/local.memory.write/enable")
    assert granted.status_code == 200
    assert granted.json()["item"]["enabled"] is True
    assert handler_enabled("local.memory.write") is True

    dispatched = queue_and_approve()
    assert dispatched.status_code == 200
    assert dispatched.json()["dispatch"] == "dispatched"
    assert dispatched.json()["work_item"]["status"] == "done"

    # Toggling an unregistered handler is a 404, not a silent no-op.
    missing = client.post("/action-handlers/local.does.not.exist/disable")
    assert missing.status_code == 404


def test_local_handler_dispatch_requires_typed_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Commit approved founder note",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {"content": "Do not dispatch without phrase."},
        },
    )
    rejected = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"resolved_by": "founder", "note": "Wrong phrase.", "dispatch": True, "typed_confirmation": "wrong"},
    )
    queue = client.get("/work/queue")
    approvals = client.get("/approval-requests")

    assert rejected.status_code == 400
    assert "typed confirmation phrase" in rejected.json()["detail"]
    assert queue.json()["items"][0]["status"] == "approval_required"
    assert approvals.json()["items"][0]["status"] == "pending"


def test_safe_local_handlers_dispatch_after_typed_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    phrase = "make the jump to hyperspace"

    file_path = tmp_path / "hot" / "handler-output" / "approved.txt"
    file_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Write approved file",
            "action": "local.file.write",
            "target": str(file_path),
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {"content": "Approved local file write.", "mode": "create"},
        },
    )
    file_dispatch = client.post(
        f"/work/items/{file_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    report_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Write founder report",
            "action": "local.report.write",
            "target": "local_report",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {"content": "The report is local, auditable, and linked to memory."},
        },
    )
    report_dispatch = client.post(
        f"/work/items/{report_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    browser_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Prepare local UI open",
            "action": "local.browser.open",
            "target": "http://127.0.0.1:8787/ui",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {"open_browser": False},
        },
    )
    browser_dispatch = client.post(
        f"/work/items/{browser_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    assert file_dispatch.status_code == 200
    assert file_dispatch.json()["dispatch_result"]["path"] == str(file_path)
    assert file_path.read_text(encoding="utf-8") == "Approved local file write."
    assert report_dispatch.status_code == 200
    assert Path(report_dispatch.json()["dispatch_result"]["path"]).exists()
    assert report_dispatch.json()["dispatch_result"]["memory_id"] > 0
    assert browser_dispatch.status_code == 200
    assert browser_dispatch.json()["dispatch_result"]["opened"] is False


def test_approval_request_can_be_denied(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Open browser",
            "action": "browser.open",
            "target": "https://example.com",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    request = client.get("/approval-requests").json()["items"][0]
    denied = client.post(
        f"/approval-requests/{request['id']}/deny",
        json={"resolved_by": "founder", "note": "Not now."},
    )

    assert queued.status_code == 200
    assert denied.status_code == 200
    assert denied.json()["request"]["status"] == "denied"
    assert denied.json()["work_item"]["status"] == "denied"


def test_action_approval_console_edit_defer_and_learning_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Open external research",
            "detail": "Zade wants to open a research page.",
            "action": "browser.open",
            "target": "https://example.com/research",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
            "metadata": {
                "evidence": ["Decision engine requested competitor evidence."],
                "risks": ["External browser action."],
            },
        },
    )
    request_id = client.get("/approval-requests").json()["items"][0]["id"]
    console_before = client.get("/approval-console")
    edited = client.post(
        f"/approval-requests/{request_id}/edit",
        json={
            "edited_by": "founder",
            "note": "Keep this local and auditable.",
            "title": "Open local founder UI",
            "detail": "Use the local UI instead of an external page.",
            "action": "local.browser.open",
            "target": "http://127.0.0.1:8787/ui/founder.html",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "priority": 77,
            "evidence": ["Local UI has the current founder context."],
            "risks": ["Browser open still needs dispatch confirmation."],
        },
    )
    deferred = client.post(
        f"/approval-requests/{request_id}/defer",
        json={"resolved_by": "founder", "note": "Handle after the current build.", "defer_until": "2026-07-13T09:00:00-05:00"},
    )

    approve_item = client.post(
        "/work/items",
        json={
            "kind": "approval_console",
            "title": "No-op approved through console",
            "action": "local.noop",
            "target": "approval-console",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    approved = client.post(
        f"/work/items/{approve_item.json()['item_id']}/approve",
        json={"resolved_by": "founder", "note": "Safe local no-op."},
    )

    deny_item = client.post(
        "/work/items",
        json={
            "kind": "approval_console",
            "title": "Deny external SMS",
            "action": "sms.send",
            "target": "+15555550100",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    denied = client.post(
        f"/work/items/{deny_item.json()['item_id']}/deny",
        json={"resolved_by": "founder", "note": "No SMS gateway configured."},
    )

    console_deferred = client.get("/approval-console", params={"status": "deferred"})
    training = client.get("/approval-training-events")
    metrics = client.get("/founder/metrics")
    inventory = client.get("/self-inventory")
    ui = client.get("/ui/approvals.html")

    assert queued.status_code == 200
    assert console_before.status_code == 200
    assert console_before.json()["items"][0]["zade_wants"].startswith("Zade wants to browser.open")
    assert console_before.json()["items"][0]["evidence"]["items"] == ["Decision engine requested competitor evidence."]
    assert console_before.json()["items"][0]["risk"]["items"][0] == "External browser action."
    assert edited.status_code == 200
    assert edited.json()["request"]["action"] == "local.browser.open"
    assert edited.json()["work_item"]["priority"] == 77
    assert edited.json()["console_item"]["authority_tier"]["authority_decision"] == "approval_required"
    assert deferred.status_code == 200
    assert deferred.json()["request"]["status"] == "deferred"
    assert deferred.json()["work_item"]["status"] == "deferred"
    assert approved.status_code == 200
    assert approved.json()["training_event_id"] > 0
    assert denied.status_code == 200
    assert denied.json()["training_event_id"] > 0
    assert console_deferred.json()["items"][0]["request"]["action"] == "local.browser.open"
    outcomes = {item["outcome"] for item in training.json()["items"]}
    assert {"edited", "deferred", "approved", "denied"} <= outcomes
    assert metrics.json()["counts"]["approval_training_events"] == 4
    assert metrics.json()["approvals"]["training_by_outcome"]["edited"] == 1
    assert "GET /approval-console" in inventory.json()["work_queue"]["routes"]
    assert "POST /approval-requests/{request_id}/defer" in inventory.json()["work_queue"]["routes"]
    assert "POST /approval-requests/{request_id}/edit" in inventory.json()["work_queue"]["routes"]
    assert ui.status_code == 200
    assert "Zade Action Approval Console" in ui.text


def test_deferred_work_item_can_be_resolved_from_the_work_queue(tmp_path: Path, monkeypatch) -> None:
    """A deferred item is parked, not decided — the founder must still be able
    to approve or deny it straight from the work queue. Previously
    /work/items/{id}/deny 400'd on deferred items ("Work item is deferred,
    not approval_required."): the request lookup only matched status='pending'
    and the backfill only accepted approval_required, so deferred items were
    only resolvable by request id through the approvals console."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    def enqueue(title: str) -> int:
        item = client.post("/work/items", json={
            "kind": "external", "title": title, "action": "browser.open",
            "target": "https://example.com", "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        })
        return item.json()["item_id"]

    # Defer via the console, then DENY via the work-queue route.
    deny_id = enqueue("Defer then deny from queue")
    request_id = client.get("/approval-requests").json()["items"][0]["id"]
    parked = client.post(f"/approval-requests/{request_id}/defer", json={"resolved_by": "founder", "note": "later"})
    assert parked.json()["work_item"]["status"] == "deferred"
    denied = client.post(f"/work/items/{deny_id}/deny", json={"resolved_by": "founder", "note": "stale"})
    assert denied.status_code == 200
    assert denied.json()["work_item"]["status"] == "denied"
    # No duplicate approval request was created for the item.
    all_requests = [r for r in client.get("/approval-requests", params={"limit": 50}).json()["items"]
                    if r.get("source_id") == deny_id]
    assert len(all_requests) == 1

    # Defer via the console, then APPROVE via the work-queue route.
    approve_id = enqueue("Defer then approve from queue")
    request_id_2 = next(r["id"] for r in client.get("/approval-requests", params={"limit": 50}).json()["items"]
                        if r.get("source_id") == approve_id)
    client.post(f"/approval-requests/{request_id_2}/defer", json={"resolved_by": "founder", "note": "later"})
    approved = client.post(f"/work/items/{approve_id}/approve", json={"resolved_by": "founder", "note": "go"})
    assert approved.status_code == 200
    assert approved.json()["request"]["status"] == "approved"

    # Already-resolved items still refuse cleanly.
    again = client.post(f"/work/items/{deny_id}/deny", json={"resolved_by": "founder", "note": "again"})
    assert again.status_code == 400
    assert "open items" in again.json()["detail"]


def test_ops_health_check_and_backup_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    health = client.get("/ops/health-check")
    backup = client.post("/ops/backup", json={"label": "test run"})
    backup_2 = client.post("/ops/backup", json={"label": "test run 2"})
    backup_3 = client.post("/ops/backup", json={"label": "test run 3"})
    backups = client.get("/ops/backups")
    prune = client.post("/ops/backups/prune", json={"keep_last": 1, "dry_run": False})
    benchmark = client.post(
        "/models/benchmark",
        json={"prompt": "State readiness.", "roles": ["general", "coding"], "num_predict": 32},
    )
    telemetry = client.get("/models/telemetry")
    security = client.get("/ops/security")
    inventory = client.get("/self-inventory")

    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["checks"]["ui"]["ok"] is True
    assert backup.status_code == 200
    assert backup_2.status_code == 200
    assert backup_3.status_code == 200
    assert backup.json()["path"].endswith(".sqlite")
    assert Path(backup_3.json()["path"]).exists()
    assert backups.status_code == 200
    assert backups.json()["items"][0]["name"].endswith(".sqlite")
    assert prune.status_code == 200
    assert prune.json()["deleted_count"] >= 2
    assert benchmark.status_code == 200
    assert benchmark.json()["status"] == "ok"
    assert telemetry.json()["by_operation"]["ops.model_benchmark"] == 2
    assert security.status_code == 200
    assert security.json()["token_header"] == "X-Zade-Token"
    assert "GET /ops/health-check" in inventory.json()["ops_layer"]["routes"]
    assert "POST /ops/backups/prune" in inventory.json()["ops_layer"]["routes"]
    assert "POST /models/benchmark" in inventory.json()["runtime_layer"]["routes"]


def test_founder_operating_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    thesis = client.post(
        "/founder/thesis",
        json={
            "vision": "A private AI co-founder compounds operator context.",
            "mission": "Make Zade a founder operating system.",
            "why_now": "Local models can reason over private memory.",
            "customer": "Founder operators",
            "core_assumptions": [{"assumption": "Context compounds", "confidence": 70}],
            "unknown_unknowns": ["market wedge"],
            "status": "active",
        },
    )
    initiative = client.post(
        "/founder/initiatives",
        json={
            "objective": "Build founder dashboard",
            "priority": 95,
            "success_criteria": ["Brief exists"],
            "confidence": 80,
        },
    )
    decision = client.post(
        "/founder/decisions",
        json={
            "problem": "What should Zade build next?",
            "options": [{"name": "Founder layer"}, {"name": "Scheduler"}],
            "recommendation": "Founder layer",
            "confidence": 85,
        },
    )
    prediction = client.post(
        "/founder/predictions",
        json={"prediction": "Founder layer improves recommendations.", "probability": 0.8},
    )
    scored = client.post(
        "/founder/predictions/score",
        json={"prediction_id": prediction.json()["id"], "outcome": "true", "lessons": "It made focus explicit."},
    )
    contrarian = client.post(
        "/founder/contrarian-reviews",
        json={"title": "Review founder layer", "top_risks": ["Form over judgment"]},
    )
    dashboard = client.get("/founder/dashboard")
    brief = client.get("/founder/brief")
    reflections = client.get("/founder/reflections")
    mental_models = client.get("/founder/mental-models")

    assert thesis.status_code == 200
    assert thesis.json()["thesis"]["status"] == "active"
    assert initiative.status_code == 200
    assert initiative.json()["item"]["objective"] == "Build founder dashboard"
    assert decision.status_code == 200
    assert prediction.status_code == 200
    assert scored.status_code == 200
    assert scored.json()["item"]["calibration_error"] == 0.2
    assert contrarian.status_code == 200
    assert "red_team" in contrarian.json()["item"]["roles"]
    assert dashboard.status_code == 200
    assert dashboard.json()["one_thing_that_matters_most_today"].startswith("Decide:")
    assert brief.status_code == 200
    assert "Zade founder brief" in brief.json()["brief"]
    assert reflections.status_code == 200
    assert len(reflections.json()["items"]) >= 5
    assert mental_models.status_code == 200
    assert any(item["name"] == "Expected value" for item in mental_models.json()["models"])


def test_founder_v2_operating_object_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    assumption = client.post(
        "/founder/assumptions",
        json={
            "statement": "Founders will pay for accountability.",
            "category": "pricing",
            "confidence": 72,
        },
    )
    evidence = client.post(
        "/founder/evidence",
        json={
            "evidence_type": "customer interview",
            "source": "founder calls",
            "reliability": "C",
            "claim_contradicted": "Founders avoid accountability pressure after week one.",
            "strength": 75,
            "linked_assumption_id": assumption.json()["id"],
        },
    )
    bet = client.post(
        "/founder/strategy-objects",
        json={
            "object_type": "active_bet",
            "title": "Start with solo founders",
            "confidence": 68,
            "reversal_trigger": "Activation below 20%.",
        },
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Validate willingness to pay", "related_bet_ids": [bet.json()["id"]]},
    )
    task = client.post("/founder/tasks", json={"title": "Refine landing page visuals"})
    kill = client.post(
        "/founder/kill-criteria",
        json={"subject_type": "bet", "subject_id": bet.json()["id"], "metric": "activation", "threshold": "< 20%"},
    )
    override = client.post(
        "/founder/overrides",
        json={
            "zade_recommendation": "Delay integrations.",
            "founder_decision": "Build integrations now.",
            "risk_accepted": "Premature engineering spend.",
        },
    )
    integrity = client.post("/founder/integrity-check")
    cadence = client.post("/founder/cadence-reviews/generate/daily")
    conflicts = client.get("/founder/thesis-conflicts")
    confidence = client.get("/founder/confidence-events")
    metrics = client.get("/founder/metrics")
    inventory = client.get("/self-inventory")

    assert assumption.status_code == 200
    assert evidence.status_code == 200
    assert evidence.json()["item"]["linked_assumption_id"] == assumption.json()["id"]
    assert bet.status_code == 200
    assert goal.status_code == 200
    assert task.status_code == 200
    assert kill.status_code == 200
    assert override.status_code == 200
    assert integrity.status_code == 200
    assert integrity.json()["count"] >= 2
    assert cadence.status_code == 200
    assert cadence.json()["item"]["review_type"] == "daily"
    assert conflicts.status_code == 200
    assert conflicts.json()["items"][0]["severity"] == "yellow"
    assert confidence.status_code == 200
    assert confidence.json()["items"][0]["new_confidence"] < 72
    assert metrics.status_code == 200
    assert metrics.json()["counts"]["assumptions"] == 1
    assert metrics.json()["evidence"]["by_reliability"]["C"] == 1
    assert metrics.json()["integrity"]["by_status"]["open"] >= 1
    assert "POST /founder/integrity-check" in inventory.json()["founder_operating_layer"]["routes"]
    assert "GET /founder/metrics" in inventory.json()["founder_operating_layer"]["routes"]
    assert "evidence" in inventory.json()["founder_operating_layer"]["artifacts"]


def test_active_objective_decision_engine_and_runtime_context_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    goal = client.post(
        "/founder/goals",
        json={"name": "Validate Zade as co-founder", "metric": "daily useful decisions", "target": "1"},
    )
    objective = client.post(
        "/founder/active-objectives",
        json={
            "objective": "Make Zade drive founder decisions",
            "desired_outcome": "Every session ends with a useful next action.",
            "metric": "daily useful decisions",
            "target": "1",
            "linked_goal_ids": [goal.json()["id"]],
            "risks": ["Recommendations become generic."],
            "next_action": "Run a structured recommendation against the active experiment.",
            "confidence": 70,
        },
    )
    recommendation = client.post(
        "/founder/decision-engine/recommend",
        json={
            "problem": "Should we improve evidence intake or dashboard polish next?",
            "options": [
                {"name": "Improve evidence intake", "recommended": True},
                {"name": "Polish dashboard", "priority": 40},
            ],
        },
    )
    active = client.get("/founder/active-objective")
    recs = client.get("/founder/decision-recommendations")
    dashboard = client.get("/founder/dashboard")
    context = client.post(
        "/runtime/context",
        json={"message": "What should Zade push next?", "use_semantic_memory": False},
    )
    runtime_response = client.post(
        "/runtime/respond",
        json={"message": "Recommend the next founder move.", "use_semantic_memory": False},
    )
    founder_ui = client.get("/ui/founder.html")
    metrics = client.get("/founder/metrics")
    inventory = client.get("/self-inventory")

    assert objective.status_code == 200
    assert objective.json()["item"]["is_current"] == 1
    assert recommendation.status_code == 200
    assert recommendation.json()["operating_contract"]["recommendation"] == "Improve evidence intake"
    assert recommendation.json()["decision_memo"]["recommendation"] == "Improve evidence intake"
    assert recommendation.json()["next_task"]["strategic_value"] == "Make Zade drive founder decisions"
    assert active.json()["item"]["objective"] == "Make Zade drive founder decisions"
    assert recs.json()["items"][0]["recommendation"] == "Improve evidence intake"
    assert dashboard.json()["active_objective"]["id"] == objective.json()["id"]
    assert context.json()["founder_dashboard"]["decision_engine"]["latest_recommendations"][0]["recommendation"] == "Improve evidence intake"
    assert runtime_response.json()["context"]["active_objective"]["objective"] == "Make Zade drive founder decisions"
    assert founder_ui.status_code == 200
    assert "Zade Founder Ops" in founder_ui.text
    assert metrics.json()["counts"]["active_objectives"] == 1
    assert metrics.json()["counts"]["decision_recommendations"] == 1
    assert "POST /founder/decision-engine/recommend" in inventory.json()["founder_operating_layer"]["routes"]


def test_runtime_directed_build_executes_and_reports_real_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    """The full-auto path end to end: a founder build command dispatches the
    native coding agent THIS turn and the reply reports what actually happened —
    files changed, artifact filed — not an Inbox pointer."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 3,
            "used_tools": True,
            "steps": [
                {"tool": "write_file", "arguments": {"path": "src/app.py"}, "ok": True},
                {"tool": "run_command", "arguments": {"argv": ["python", "-m", "pytest", "-q"]},
                 "ok": True, "auto_verify": True},
            ],
            "changed_files": ["src/app.py"],
            "response": "Built the scanner module; tests pass.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build me a book cataloguing mobile app with barcode scanning",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    assert route["status"] == "executed"
    assert route["dispatch"]["changed_files"] == ["src/app.py"]
    assert "Ran the build -" in payload["response"]
    assert "src/app.py" in payload["response"]
    assert "Kernel-run verification passed" in payload["response"]
    # The run item is closed; nothing waits on approval.
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    done = client.get("/work/queue", params={"status": "done"}).json()["items"]
    assert any(item["id"] == route["item_id"] for item in done)
    # The artifact was filed as delegated-work evidence.
    evidence = client.get("/founder/evidence").json()["items"]
    assert any(item["evidence_type"] == "delegated_work" for item in evidence)


def test_runtime_directed_build_failed_check_reports_not_done(
    tmp_path: Path, monkeypatch
) -> None:
    """The open integrity gap, closed: when the kernel's own check on a
    delegated run FAILS, the founder-facing report must lead with 'NOT done' —
    never 'Ran the build' with the failure silently dropped."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 3,
            "used_tools": True,
            "steps": [
                {"tool": "write_file", "arguments": {"path": "src/app.py"}, "ok": True},
                {"tool": "run_command", "arguments": {"argv": ["python", "-m", "py_compile", "src/app.py"]},
                 "ok": False, "auto_verify": True},
            ],
            "changed_files": ["src/app.py"],
            "auto_verification": {
                "mode": "syntax",
                "ok": False,
                "checks": [{"argv": ["python", "-m", "py_compile", "src/app.py"], "ok": False, "returncode": 1}],
                "unchecked_files": [],
                "argv": ["python", "-m", "py_compile", "src/app.py"],
                "returncode": 1,
                "repair_rounds": 2,
            },
            "response": "Implemented the module.\n\n--- Kernel auto-verification ---\nSyntaxError: invalid syntax",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build me a book cataloguing mobile app with barcode scanning",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route["status"] == "verify_failed"
    assert "NOT done" in payload["response"]
    assert "check on the result FAILED" in payload["response"]
    assert "Ran the build" not in payload["response"]
    # The failing check command is named so the founder sees what actually ran.
    assert "py_compile" in payload["response"]


def test_runtime_directed_build_without_any_check_reports_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    """A run that changed files nothing could check must say UNVERIFIED
    out loud instead of reading like a clean completion."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [
                {"tool": "write_file", "arguments": {"path": "src/navigation.js"}, "ok": True},
            ],
            "changed_files": ["src/navigation.js", "src/screens/BarcodeScannerScreen.js"],
            "auto_verification": {
                "mode": "none",
                "ok": None,
                "checks": [],
                "unchecked_files": ["src/navigation.js", "src/screens/BarcodeScannerScreen.js"],
                "argv": None,
                "returncode": None,
                "repair_rounds": 0,
            },
            "response": "Implemented the UI components.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build me a book cataloguing mobile app with barcode scanning",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["build"]["status"] == "executed"
    assert "UNVERIFIED" in payload["response"]
    assert "Kernel-run verification passed" not in payload["response"]


def test_runtime_directed_build_surfaces_founder_decision(
    tmp_path: Path, monkeypatch
) -> None:
    """Queue-only-when-unsure: when the agent stops on a genuine decision, the
    reply asks the question directly and points at the filed decision item."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "needs_decision",
            "error": "",
            "founder_question": {
                "question": "Native camera API or a scanning library for barcodes?",
                "options": ["react-native-vision-camera", "expo-barcode-scanner"],
            },
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [{"tool": "list_files", "arguments": {}, "ok": True}],
            "changed_files": [],
            "response": "Paused on the scanner choice.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Build me a book cataloguing mobile app with barcode scanning",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    assert route["status"] == "needs_decision"
    assert route["decision_item_id"]
    assert "Native camera API or a scanning library" in payload["response"]
    assert f"#{route['decision_item_id']}" in payload["response"]
    # The decision item waits for the founder; the run item is closed.
    queued = client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    assert any(item["id"] == route["decision_item_id"] and item["kind"] == "founder_decision"
               for item in queued)



def test_runtime_review_command_routes_directed_readonly_run(
    tmp_path: Path, monkeypatch
) -> None:
    """"Review the project folder and outline all remaining work: <path>" is an
    inspection ORDER on an existing project (live incident: it drew a narrated
    inspection plan plus a leaked JSON tool call, and nothing was read). It must
    route as a directed, read-only delegated run. Hermetic: no model reachable,
    so the run fails honestly - but the route, brief, and read-only acceptance
    are all real."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    project = tmp_path / "TheDarkIndex"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": (
                "Review the virtual library mobile app project folder and outline "
                f"all remaining work to be completed: {project}"
            ),
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    assert route["kind"] == "review"
    assert route["status"] == "run_failed"  # no reachable model in this test
    assert route["workspace"] == str(project)
    assert "Read-only review" in route["task"]
    assert "build_work_routed" in payload["governor"]["applied_rules"]

    failed = client.get("/work/queue", params={"status": "error"}).json()["items"]
    match = next((item for item in failed if item["id"] == route["item_id"]), None)
    assert match is not None
    assert match["kind"] == "delegation_run"
    assert match["metadata"]["workspace"] == str(project)
    # The read-only contract is in the brief the agent receives.
    assert "READ-ONLY" in match["metadata"]["brief"]
    assert "NOTHING" in match["metadata"]["brief"]


def test_runtime_review_command_without_path_asks_for_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A review command with no resolvable project directory must ask for the
    path - and a drafted inspection plan is dropped, never shown as if the
    inspection were about to happen."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def stall_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response=(
                "To review the project folder and outline all remaining work, I need "
                "to inspect the current state of the files. I will perform the "
                "following steps:\n\n1. Inspect the Project Folder\n\nLet's begin by "
                "inspecting the project folder."
            ),
            model=model or "qwen3:14b",
            raw={"prompt": prompt},
        )

    patch_ollama_model(monkeypatch, stall_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Review the mobile app project folder and outline all remaining work",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    route = payload["build"]
    assert route is not None
    assert route["kind"] == "review"
    assert route["status"] == "no_task"
    assert "no existing project directory" in payload["response"]
    # The inspection-promise draft was dropped, not stacked on the route block.
    assert "I will perform the following steps" not in payload["response"]
    assert "routed_reply_body_replaced" in payload["governor"]["applied_rules"]


def test_runtime_review_questions_do_not_route(tmp_path: Path, monkeypatch) -> None:
    """Review used as a noun or in a question is not an inspection order."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    for message in (
        "How did the review of the project go?",
        "What should a good code review of the app cover?",
        "Did you finish the review of the codebase?",
    ):
        response = client.post(
            "/runtime/respond",
            json={
                "message": message,
                "use_memory": False,
                "use_semantic_memory": False,
                "use_skills": False,
                "contrarian": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["build"] is None, message


def test_workspace_status_question_answered_from_run_record(
    tmp_path: Path, monkeypatch
) -> None:
    """"What has been completed and what remains?" about a project the kernel has
    run delegated work in must be answered from the workspace's verified run
    record - not fallback boilerplate, not a model draft (live incident: five
    turns of status questions about a project with 12 completed runs on file)."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)

    project = tmp_path / "TheDarkIndex"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    conversation_id = client.post("/conversations", json={}).json()["conversation"]["id"]

    # A directed review run leaves a work item recorded against the workspace
    # (it fails here - no model - which is itself an honest recorded outcome).
    first = client.post(
        "/runtime/respond",
        json={
            "message": f"Review the project folder and outline the remaining work: {project}",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["build"]["item_id"]

    response = client.post(
        "/runtime/respond",
        json={
            "message": "What has been completed of the mobile application build process and what remains?",
            "conversation_id": conversation_id,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "workspace_work_history_answer" in payload["governor"]["applied_rules"]
    assert "Delegated-run record for" in payload["response"]
    assert str(project) in payload["response"]
    # The honest next move is offered: a read-only review of the current state.
    assert "review" in payload["response"].lower()


def test_leaked_tool_call_json_is_stripped_from_replies(
    tmp_path: Path, monkeypatch
) -> None:
    """Raw tool-call JSON emitted as prose (live incident: a fenced
    {"name": "memory_search", ...} block in the reply) is stripped - no such
    call executed, and the founder must never see it as if one did."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def leaky_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response=(
                "Here is where things stand on the numbers you asked about.\n\n"
                '```json\n{\n  "name": "memory_search",\n  "arguments": {\n'
                '    "query": "project status"\n  }\n}\n```\n\n'
                "Those figures are from the last saved summary."
            ),
            model=model or "qwen3:14b",
            raw={"prompt": prompt},
        )

    patch_ollama_model(monkeypatch, leaky_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Give me a quick recap of where the numbers landed",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "memory_search" not in payload["response"]
    assert '"arguments"' not in payload["response"]
    assert "leaked_tool_call_stripped" in payload["governor"]["applied_rules"]
    assert "Here is where things stand" in payload["response"]


def test_inspection_promise_stall_repaired_on_status_question(
    tmp_path: Path, monkeypatch
) -> None:
    """A routeless reply that PLANS an inspection ("I will perform the following
    steps... Let's begin by...") on a work-status ask is a stall - chat cannot
    walk a folder. It is replaced with the honest state and the routable review
    command."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def stall_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        return GenerateResult(
            response=(
                "To answer that, I need to inspect the current state of the files. "
                "I will perform the following steps:\n1. Inspect the project folder\n"
                "2. Identify completed tasks\n\nOnce I have the inspection results, "
                "I will provide a detailed outline of the remaining work."
            ),
            model=model or "qwen3:14b",
            raw={"prompt": prompt},
        )

    patch_ollama_model(monkeypatch, stall_generate)

    app = create_app(_research_config(tmp_path))
    client = TestClient(app)
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Where do we stand on the mobile app build progress?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "inspection_promise_repaired" in payload["governor"]["applied_rules"]
    assert "I will perform the following steps" not in payload["response"]
    assert "read-only review" in payload["response"]


def test_review_verify_failed_reads_as_finding_not_broken_run() -> None:
    """verify_failed on a REVIEW means the project's own checks fail as it
    stands - that is a finding of the review, not a fault in it. The block must
    not tell the founder to distrust the report."""
    from cofounder_kernel.runtime import _render_build_route_block, _build_route_note

    route = {
        "status": "verify_failed",
        "kind": "review",
        "task": r"Read-only review of C:\App\Project: what is complete, what remains",
        "workspace": r"C:\App\Project",
        "item_id": 73,
        "dispatch": {
            "artifact": "1. Broken: tsc fails.\n2. Remaining Work: Step 1: fix types.",
            "changed_files": [],
            "workspace_changes": {"added": [], "modified": [], "deleted": []},
        },
        "verification": {"ok": False, "checks": [{"argv": ["tsc", "--noEmit"], "ok": False}]},
    }
    block = _render_build_route_block(route)
    assert "finding" in block
    assert "project's own checks FAIL" in block
    assert "treat the report as incomplete" not in block
    assert "Read-only check held" in block
    assert "Step 1: fix types." in block

    note = _build_route_note(route)
    assert "finding" in note
    assert "NOT done" not in note


def test_ops_providers_readout_reflects_cloud_off(tmp_path: Path, monkeypatch) -> None:
    """/ops/providers exposes local + cloud readiness. With cloud off (default),
    it names the unmet gates and never returns a secret."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_only"),
        anthropic=AnthropicConfig(enabled=False),
    )
    client = TestClient(create_app(config))

    response = client.get("/ops/providers")
    assert response.status_code == 200
    body = response.json()

    assert body["local_only"] is True
    assert body["provider_policy"] == "local_only"
    assert body["cloud_ready"] is False
    assert body["local"]["provider"] == "ollama"

    anthropic = body["cloud"]["anthropic"]
    assert anthropic["enabled"] is False
    assert anthropic["key_present"] is False
    assert anthropic["ready"] is False
    assert any("enabled = false" in b for b in anthropic["blockers"])
    assert any("ANTHROPIC_API_KEY" in b for b in anthropic["blockers"])
    # matrix cell is read directly, so it shows per_request even under local_only
    assert anthropic["egress_cell"] == "per_request"
    # no secret leaks into the payload
    assert "sk-" not in response.text


def test_ops_providers_readout_reports_cloud_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        anthropic=AnthropicConfig(enabled=True),
    )
    client = TestClient(create_app(config))

    body = client.get("/ops/providers").json()
    assert body["cloud_ready"] is True
    anthropic = body["cloud"]["anthropic"]
    assert anthropic["ready"] is True
    assert anthropic["key_present"] is True
    assert anthropic["policy_allows_cloud"] is True
    assert anthropic["blockers"] == []
    # the key value itself is never echoed, only its presence
    assert "sk-ant-test" not in client.get("/ops/providers").text
