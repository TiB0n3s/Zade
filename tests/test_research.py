from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.research as research_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import (
    AppConfig,
    EgressConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
    ResearchConfig,
)
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.research import _html_to_text, _salience


PHRASE = "make the jump to hyperspace"
# Public IP literal: passes https + public-host checks without a DNS lookup, so
# validation is hermetic. The fetch itself is monkeypatched, never real.
PUBLIC = "https://93.184.216.34/pricing"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    return [1.0, 0.0]


def _config(tmp_path: Path) -> KernelConfig:
    # Research egress now passes through the data-class gate: raise off local_only
    # and grant the STANDING public_derived:public_web lane, the founder's config
    # opt-in for open-web research (the per-run L3 approval is still the real gate).
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        egress=EgressConfig(standing_grants=("public_derived:public_web",)),
    )


def _approve_and_dispatch(client: TestClient, item_id: int) -> dict:
    response = client.post(
        f"/work/items/{item_id}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_derive_topics_from_evidence_gaps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    app = create_app(_config(tmp_path))
    client = TestClient(app)

    # A gap: an assumption with no evidence and modest confidence.
    app.state.founder.create_assumption(
        {"statement": "Solo founders will pay $99/month for onboarding", "confidence": 40}
    )
    # Not a gap: high-confidence assumption (should be excluded).
    app.state.founder.create_assumption({"statement": "We are a software company", "confidence": 95})

    topics = client.get("/research/topics").json()["topics"]

    questions = [t["question"] for t in topics]
    assert any("Solo founders will pay $99/month" in q for q in questions)
    assert not any("We are a software company" in q for q in questions)
    assert topics[0]["score"] > 0


def test_daydream_surfaces_topics_as_notifications(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    app = create_app(_config(tmp_path))
    client = TestClient(app)
    app.state.founder.create_assumption({"statement": "Churn is driven by weak onboarding", "confidence": 35})

    result = client.post("/research/daydream", json={"limit": 3}).json()
    notifications = client.get("/notifications").json()["items"]

    assert result["notified"] >= 1
    assert any(n["topic"] == "research" for n in notifications)


def test_research_run_fetches_scores_and_files_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    app = create_app(_config(tmp_path))
    client = TestClient(app)

    captured = {}

    def fake_fetch(url, *, timeout=20.0, max_bytes=2_000_000, allowed_hosts=None):
        captured["url"] = url
        return (
            "<html><head><title>Pricing</title><style>.x{}</style></head>"
            "<body><h1>Pricing</h1><p>Solo founders will pay $99 per month.</p>"
            "<script>track()</script></body></html>"
        )

    monkeypatch.setattr(research_module, "fetch_url", fake_fetch)

    queued = client.post(
        "/research/run",
        json={"topic": "Will solo founders pay $99 per month for onboarding", "urls": [PUBLIC]},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "approval_required"
    assert queued.json()["url_count"] == 1

    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    assert result["handler"] == "external.research.run"
    assert result["ok"] is True
    assert result["fetched"] == 1
    assert result["filed"] == 1
    finding = result["findings"][0]
    assert finding["status"] == "ok"
    assert finding["salience"] > 0
    # HTML was stripped to readable text (no tags, no script body).
    assert "$99 per month" in finding["excerpt"]
    assert "<script>" not in finding["excerpt"]
    assert captured["url"] == PUBLIC

    evidence = client.get("/founder/evidence").json()["items"]
    assert any(item["evidence_type"] == "web_research" for item in evidence)


def test_research_refused_by_egress_gate_under_local_only(tmp_path: Path, monkeypatch) -> None:
    """Research egress is now unified under the data-class gate: under
    provider_policy=local_only an APPROVED run is still refused before any fetch —
    local-only truly means nothing leaves, research included."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_only"),
    )
    app = create_app(config)
    client = TestClient(app)

    called = {"n": 0}

    def fake_fetch(url, *, timeout=20.0, max_bytes=2_000_000, allowed_hosts=None):
        called["n"] += 1
        return "<html><body>should never run</body></html>"

    monkeypatch.setattr(research_module, "fetch_url", fake_fetch)

    queued = client.post("/research/run", json={"topic": "pricing", "urls": [PUBLIC]})
    assert queued.status_code == 200, queued.text
    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    assert result["status"] == "refused"
    assert result["matched_rule"] == "policy.local_only"
    assert result["fetched"] == 0
    assert called["n"] == 0  # the gate stopped it before the network was touched
    # the refusal is in the egress ledger
    audit = {e["action"] for e in client.get("/audit/recent").json()["events"]}
    assert "egress.decision" in audit


def test_research_dispatch_marks_work_item_error_when_all_fetches_fail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    app = create_app(_config(tmp_path))
    client = TestClient(app)

    def fake_fetch(url, *, timeout=20.0, max_bytes=2_000_000, allowed_hosts=None):
        raise ValueError("Research fetch failed (HTTP 403).")

    monkeypatch.setattr(research_module, "fetch_url", fake_fetch)

    queued = client.post("/research/run", json={"topic": "blocked source", "urls": [PUBLIC]})
    approved = _approve_and_dispatch(client, queued.json()["item_id"])

    assert approved["dispatch"] == "dispatch_failed"
    assert approved["work_item"]["status"] == "error"
    assert approved["dispatch_result"]["ok"] is False
    assert "handler returned ok=false" in approved["work_item"]["last_error"]
    console = client.get("/approval-console").json()["items"][0]
    assert console["work_item"]["status"] == "error"


def test_research_run_rejects_unsafe_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    not_https = client.post("/research/run", json={"topic": "t", "urls": ["http://93.184.216.34/"]})
    private = client.post("/research/run", json={"topic": "t", "urls": ["https://127.0.0.1/secret"]})

    assert not_https.status_code == 400 and "Refused research URL" in not_https.json()["detail"]
    assert private.status_code == 400 and "private" in private.json()["detail"].lower()


def test_research_disabled_blocks_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        research=ResearchConfig(enabled=False),
    )
    client = TestClient(create_app(config))

    handlers = client.get("/action-handlers").json()["items"]
    disabled = client.post("/research/run", json={"topic": "t", "urls": [PUBLIC]})

    assert "external.research.run" not in {h["action"] for h in handlers}
    assert disabled.status_code == 400 and "disabled" in disabled.json()["detail"]


def test_research_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    inventory = client.get("/self-inventory").json()

    assert "POST /research/run" in inventory["research_layer"]["routes"]
    assert "external.research.run" in inventory["work_queue"]["approved_local_dispatch_handlers"]


def test_html_to_text_and_salience_units() -> None:
    text = _html_to_text("<p>Hello <b>pricing</b></p><script>evil()</script>")
    assert "Hello pricing" in text
    assert "evil" not in text

    assert _salience("pricing willingness ninetynine", "customers accept the pricing at ninetynine") > 0
    assert _salience("pricing", "totally unrelated content") == 0
