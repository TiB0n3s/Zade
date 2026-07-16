from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, SkillConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient
from cofounder_kernel.skills import CODING_HINT_SKILLS, DEFAULT_ENABLED_SKILLS, parse_frontmatter


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
    assert "Skill: systematic-debugging" in prompt
    return GenerateResult(response="Use the selected operating procedure.", model=model or "qwen3:14b", raw={})


def _messages_to_prompt(messages: object) -> str:
    return "\n\n".join(str(getattr(message, "content", "")) for message in messages)


def _chat_from_generate(generate_func):
    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
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


BUNDLED_CODE_SKILLS = {
    "artifact-design",
    "batch",
    "claude-api",
    "code-review",
    "compact",
    "debug",
    "fewer-permission-prompts",
    "init",
    "keybindings-help",
    "loop",
    "review",
    "run",
    "schedule",
    "security-review",
    "simplify",
    "update-config",
    "verify",
}

TOOL_PROFILE_SKILLS = {
    "prompt-automation-context": "prompt-automation-context.md",
    "prompt-image-safety-policies": "prompt-image-safety-policies.md",
    "study-and-learn": "study-and-learn.md",
    "tool-advanced-memory": "tool-advanced-memory.md",
    "tool-canvas-canmore": "tool-canvas-canmore.md",
    "tool-create-image-image-gen": "tool-create-image-image_gen.md",
    "tool-deep-research": "tool-deep-research.md",
    "tool-file-search": "tool-file_search.md",
    "tool-memory-bio": "tool-memory-bio.md",
    "tool-python": "tool-python.md",
    "tool-python-code": "tool-python-code.md",
    "tool-web-search": "tool-web-search.md",
}

BUNDLED_SKILLS = BUNDLED_CODE_SKILLS | {"deep-research"} | set(TOOL_PROFILE_SKILLS)


def test_bundled_code_model_skills_are_packaged_for_skill_scanner() -> None:
    skill_root = Path(__file__).resolve().parents[1] / ".agents" / "skills"

    for name in sorted(BUNDLED_SKILLS):
        skill_path = skill_root / name / "SKILL.md"
        assert skill_path.exists(), name
        frontmatter, body = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        assert frontmatter.get("name") == name
        assert str(frontmatter.get("description") or "").strip()
        assert body.strip()
        assert name in DEFAULT_ENABLED_SKILLS

    assert (skill_root / "init" / "references" / "legacy-init.md").exists()
    for name in {
        "artifact-design",
        "batch",
        "claude-api",
        "code-review",
        "debug",
        "init",
        "review",
        "run",
        "security-review",
        "simplify",
        "update-config",
        "verify",
    }:
        assert name in CODING_HINT_SKILLS


def test_code_review_skill_uses_ultimate_effort_profile() -> None:
    skill_path = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "code-review" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")

    assert "`max effort -> 5+5 angles x 8 candidates -> 1-vote verify -> sweep -> <=15 findings`" in text
    assert "Run **10 independent finder angles**" in text
    assert "## Phase 3 - Sweep for gaps" in text
    assert "Return findings as a JSON array of at most 15 objects" in text
    assert "`high effort -> 3+5 angles x 6 candidates" not in text


def test_deep_research_skill_packages_workflow_script() -> None:
    skill_root = Path(__file__).resolve().parents[1] / ".agents" / "skills"
    skill_path = skill_root / "deep-research" / "SKILL.md"
    script_path = skill_root / "deep-research" / "scripts" / "workflow-script.js"

    assert skill_path.exists()
    frontmatter, body = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
    description = str(frontmatter.get("description") or "")

    assert frontmatter.get("name") == "deep-research"
    assert "Deep research harness" in description
    assert "deep, multi-source, fact-checked research report" in description
    assert "Workflow({ name: \"deep-research\" })" in body
    assert "5 parallel WebSearch agents" in body
    assert script_path.exists()
    script = script_path.read_text(encoding="utf-8")
    assert "name: 'deep-research'" in script
    assert "const VOTES_PER_CLAIM = 3" in script
    assert "const MAX_FETCH = 15" in script


def test_requested_tool_profiles_are_packaged_for_skill_scanner() -> None:
    skill_root = Path(__file__).resolve().parents[1] / ".agents" / "skills"

    for name, source_filename in sorted(TOOL_PROFILE_SKILLS.items()):
        skill_dir = skill_root / name
        skill_path = skill_dir / "SKILL.md"
        source_path = skill_dir / "references" / "source.md"

        assert skill_path.exists(), name
        assert source_path.exists(), source_filename
        frontmatter, body = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        assert frontmatter.get("name") == name
        assert str(frontmatter.get("description") or "").strip()
        assert source_filename in body
        assert "Tool profile boundary" in body
        assert name in DEFAULT_ENABLED_SKILLS


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

    page = client.get("/ui/system.html")
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
    assert "Test Skill Routing" in page.text
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
    patch_ollama_model(monkeypatch, fake_generate)
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
