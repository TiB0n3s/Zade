from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, SkillConfig
from cofounder_kernel.ollama import OllamaClient, OllamaError


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    lowered = text.lower()
    if "tamper" in lowered or "monitoring" in lowered:
        return [1.0, 0.0]
    return [0.0, 1.0]


def raising_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    raise OllamaError("embedding model offline")


def _config(tmp_path: Path, skills_dir: Path | None = None) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        skills=SkillConfig(source_dir=skills_dir or tmp_path / "skills", lock_file=tmp_path / "skills-lock.json"),
    )


def _seed_documents(client: TestClient) -> None:
    keyword_doc = client.post(
        "/ingest/text",
        json={
            "title": "Audit Policy",
            "text": "The quarterly audit trail policy requires signatures from both founders.",
            "source": "test:keyword",
        },
    )
    vector_doc = client.post(
        "/ingest/text",
        json={
            "title": "Ledger Watch",
            "text": "Zade watches ledger history for tampering and silent edits.",
            "source": "test:vector",
        },
    )
    assert keyword_doc.status_code == 200
    assert vector_doc.status_code == 200


def test_hybrid_fuses_keyword_and_vector_rankings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    client = TestClient(create_app(_config(tmp_path)))
    _seed_documents(client)

    # Query embeds toward "Ledger Watch" but shares keywords only with "Audit Policy".
    hybrid = client.post("/memory/semantic-search", json={"query": "audit trail monitoring", "mode": "hybrid"})
    vector = client.post("/memory/semantic-search", json={"query": "audit trail monitoring", "mode": "vector"})
    keyword = client.post("/memory/semantic-search", json={"query": "audit trail monitoring", "mode": "keyword"})
    bad_mode = client.post("/memory/semantic-search", json={"query": "audit trail", "mode": "psychic"})

    assert hybrid.status_code == 200
    hybrid_titles = {match["document_title"] for match in hybrid.json()["matches"]}
    assert hybrid_titles == {"Audit Policy", "Ledger Watch"}
    by_title = {match["document_title"]: match for match in hybrid.json()["matches"]}
    assert by_title["Ledger Watch"]["retrieval"]["mode"] == "hybrid"
    assert by_title["Ledger Watch"]["retrieval"]["vector_rank"] == 1
    assert by_title["Audit Policy"]["retrieval"]["keyword_rank"] == 1
    assert by_title["Audit Policy"]["retrieval"]["rrf_score"] > 0

    # Vector-only ranks the semantic match first; keyword-only finds only the lexical match.
    assert vector.status_code == 200
    assert vector.json()["matches"][0]["document_title"] == "Ledger Watch"
    assert vector.json()["matches"][0]["retrieval"]["mode"] == "vector"
    assert keyword.status_code == 200
    keyword_titles = [match["document_title"] for match in keyword.json()["matches"]]
    assert keyword_titles == ["Audit Policy"]
    assert bad_mode.status_code == 400
    assert "Retrieval mode" in bad_mode.json()["detail"]


def test_hybrid_degrades_to_keyword_when_embedder_is_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    client = TestClient(create_app(_config(tmp_path)))
    _seed_documents(client)

    monkeypatch.setattr(OllamaClient, "embed", raising_embed)
    degraded = client.post("/memory/semantic-search", json={"query": "audit trail policy", "mode": "hybrid"})
    vector_fails = client.post("/memory/semantic-search", json={"query": "audit trail policy", "mode": "vector"})

    assert degraded.status_code == 200
    matches = degraded.json()["matches"]
    assert matches[0]["document_title"] == "Audit Policy"
    assert matches[0]["retrieval"]["degraded_to_keyword"] is True
    assert matches[0]["retrieval"]["vector_rank"] is None
    # Explicit vector mode still surfaces the failure instead of hiding it.
    assert vector_fails.status_code == 503


def write_skill(root: Path, name: str, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_semantic_skill_routing_finds_skill_without_keyword_overlap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def skill_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
        lowered = text.lower()
        if "browser" in lowered or "flaky" in lowered:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(OllamaClient, "embed", skill_embed)
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "webapp-testing",
        "Drive a browser to exercise web applications end to end.",
        "Launch the app, drive the browser through critical journeys, and capture failures.",
    )
    write_skill(
        skills_dir,
        "copywriting",
        "Write marketing copy for landing pages.",
        "Headlines, subheads, and calls to action for conversion.",
    )
    client = TestClient(create_app(_config(tmp_path, skills_dir)))

    scan = client.post("/skills/scan")
    # No keyword overlap with the skill text; only the embedding space connects them.
    routed = client.post("/skills/route", json={"query": "my checkout journeys keep coming up flaky", "limit": 3})

    assert scan.status_code == 200
    assert scan.json()["scanned"] == 2
    assert scan.json()["embedded"] == 2
    assert routed.status_code == 200
    selected = routed.json()["selected"]
    assert selected
    assert selected[0]["name"] == "webapp-testing"
    assert selected[0]["semantic_score"] > 0
    assert selected[0]["semantic_similarity"] == 1.0
    assert all(item["name"] != "copywriting" for item in selected)


def test_skill_routing_falls_back_to_keywords_without_embeddings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", raising_embed)
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "systematic-debugging",
        "Debug issues through reproduction, hypothesis, and verification.",
        "Use when fixing a bug. Reproduce, inspect evidence, change one thing, and verify.",
    )
    client = TestClient(create_app(_config(tmp_path, skills_dir)))

    scan = client.post("/skills/scan")
    routed = client.post("/skills/route", json={"query": "debug this bug and verify the fix", "limit": 3})

    assert scan.status_code == 200
    assert scan.json()["embedded"] == 0
    assert scan.json()["embedding_errors"] == 1
    assert routed.status_code == 200
    assert routed.json()["selected"][0]["name"] == "systematic-debugging"
    assert routed.json()["selected"][0]["semantic_score"] == 0.0
