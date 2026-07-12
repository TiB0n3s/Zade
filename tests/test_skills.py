from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, SkillConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_generate(
    self: OllamaClient,
    *,
    prompt: str,
    model: str | None = None,
    think: bool | None = None,
    temperature: float | None = None,
    num_predict: int = 512,
) -> GenerateResult:
    assert "Selected operating skills:" in prompt
    return GenerateResult(response="Use the selected operating procedure.", model=model or "qwen3:14b", raw={})


def make_config(tmp_path: Path, skills_dir: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        skills=SkillConfig(source_dir=skills_dir, lock_file=tmp_path / "skills-lock.json"),
    )


def write_skill(root: Path, name: str, description: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_skill_registry_scan_route_and_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "verification-before-completion",
        "Requires verification commands before claiming completion.",
        "Run tests, read output, and only then claim the work is complete.",
    )
    write_skill(
        skills_dir,
        "cold-email",
        "Write B2B cold emails and follow-up sequences.",
        "Use for cold outreach and prospecting emails. Sending email remains approval gated.",
    )
    config = make_config(tmp_path, skills_dir)
    client = TestClient(create_app(config))

    page = client.get("/ui/skills.html")
    scan = client.post("/skills/scan")
    listed = client.get("/skills")
    routed = client.post(
        "/skills/route",
        json={"query": "verify tests before saying the build is complete", "task_type": "coding", "limit": 3},
    )
    disabled = client.post("/skills/verification-before-completion/disable")
    routed_after_disable = client.post(
        "/skills/route",
        json={"query": "verify tests before saying the build is complete", "task_type": "coding", "limit": 3},
    )
    enabled = client.post("/skills/verification-before-completion/enable")
    detail = client.get("/skills/verification-before-completion")

    assert page.status_code == 200
    assert "Zade Skill Registry" in page.text
    assert scan.status_code == 200
    assert scan.json()["scanned"] == 2
    assert listed.status_code == 200
    assert listed.json()["summary"]["total"] == 2
    assert any(item["name"] == "verification-before-completion" and item["enabled"] for item in listed.json()["items"])
    assert routed.status_code == 200
    assert routed.json()["selected"][0]["name"] == "verification-before-completion"
    assert disabled.status_code == 200
    assert disabled.json()["item"]["enabled"] is False
    assert routed_after_disable.status_code == 200
    assert all(item["name"] != "verification-before-completion" for item in routed_after_disable.json()["selected"])
    assert enabled.status_code == 200
    assert enabled.json()["item"]["enabled"] is True
    assert detail.status_code == 200
    assert detail.json()["item"]["risk_tier"] == "read_only"


def test_runtime_response_logs_skill_invocation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    skills_dir = tmp_path / "skills"
    write_skill(
        skills_dir,
        "systematic-debugging",
        "Debug issues through reproduction, hypothesis, and verification.",
        "Use when fixing a bug. Reproduce, inspect evidence, change one thing, and verify.",
    )
    config = make_config(tmp_path, skills_dir)
    client = TestClient(create_app(config))

    scan = client.post("/skills/scan")
    response = client.post(
        "/runtime/respond",
        json={"message": "Debug this bug and verify the fix.", "task_type": "coding", "use_semantic_memory": False},
    )
    invocations = client.get("/skills/invocations")
    events = client.get("/runtime/events")
    inventory = client.get("/self-inventory")

    assert scan.status_code == 200
    assert response.status_code == 200
    assert response.json()["context"]["skill_state"]["selected_count"] == 1
    assert response.json()["context"]["skill_state"]["selected"][0]["name"] == "systematic-debugging"
    assert "skill_router_scoped_context" in response.json()["governor"]["applied_rules"]
    assert response.json()["skill_invocation_ids"]
    assert invocations.status_code == 200
    assert invocations.json()["items"][0]["name"] == "systematic-debugging"
    assert events.json()["events"][0]["details"]["context_summary"]["skill_state"]["selected_count"] == 1
    assert "POST /skills/scan" in inventory.json()["skill_layer"]["routes"]
