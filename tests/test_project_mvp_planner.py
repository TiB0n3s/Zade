import json
from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel.config import KernelConfig, OllamaConfig, PathConfig, load_config
from cofounder_kernel.ollama import GenerateResult
from cofounder_kernel.project_mvp_planner import MVP_PLAN_SCHEMA, ProjectMvpPlanner


MVP_DOC = """# Same Ground MVP

The first release must let responders search trusted resources.
It must also keep crisis contact information available without a network.
"""


class FakeOllama:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> GenerateResult:
        self.calls.append(kwargs)
        return GenerateResult(
            response=json.dumps(self.payload),
            model=str(kwargs.get("model") or "local-coder"),
            raw={},
        )


def make_documented_project(tmp_path: Path, name: str = "Same Ground") -> Path:
    root = tmp_path / "brain" / "project-intake" / name
    root.mkdir(parents=True)
    (root / "MVP.md").write_text(MVP_DOC, encoding="utf-8")
    (root / "project.md").write_text(
        "# Project\n\nMobile application for Google Play, then Apple App Store.\n",
        encoding="utf-8",
    )
    return root


def config_for(root: Path) -> KernelConfig:
    hot_root = root.parents[1]
    return KernelConfig(
        paths=PathConfig(
            hot_root=hot_root,
            cold_root=hot_root.parent / "cold",
            data_dir=hot_root.parent / "data",
        ),
        ollama=OllamaConfig(
            base_url="http://127.0.0.1:1",
            coding_agent_model="local-coder:latest",
        ),
    )


def project_record(root: Path) -> dict[str, Any]:
    return {
        "id": 1,
        "name": root.name,
        "canonical_path": str(root),
        "product_type": "mobile_application",
        "distribution_targets": ["google_play", "apple_app_store_eventual"],
    }


def valid_payload() -> dict[str, Any]:
    return {
        "criteria": [
            {
                "id": "Resource Search",
                "title": "Search trusted resources",
                "description": "A responder can find a trusted resource.",
                "source": "MVP.md",
                "acceptance_checks": ["A query returns a matching resource."],
                "verification_commands": ["npm test -- --runInBand"],
                "depends_on": [],
            },
            {
                "id": "MVP_crisis access",
                "title": "Offline crisis access",
                "description": "Crisis contacts remain available offline.",
                "source": "MVP.md",
                "acceptance_checks": ["Airplane mode still shows crisis contacts."],
                "verification_commands": ["npm test -- --runInBand"],
                "depends_on": ["Resource Search"],
            },
        ],
        "external_boundaries": ["publishing_deployment"],
        "needs_decision": None,
    }


def test_planner_returns_stable_source_cited_criteria(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    fake = FakeOllama(valid_payload())
    planner = ProjectMvpPlanner(config=config_for(root), ollama=fake)

    result = planner.plan(project_record(root))

    assert result.needs_decision is None
    assert [item["id"] for item in result.criteria] == [
        "mvp-resource-search",
        "mvp-crisis-access",
    ]
    assert result.criteria[1]["depends_on"] == ["mvp-resource-search"]
    assert all(item["source"] == "MVP.md" for item in result.criteria)
    assert result.source_hash
    assert result.plan_revision
    call = fake.calls[0]
    assert call["model"] == "local-coder:latest"
    assert call["temperature"] == 0
    assert call["think"] is False
    assert call["format"] == MVP_PLAN_SCHEMA
    assert call["messages"][0]["role"] == "system"


def test_planner_prompt_treats_founder_answers_as_binding_constraints(
    tmp_path: Path,
) -> None:
    root = make_documented_project(tmp_path)
    project = project_record(root)
    project["metadata"] = {"planner_founder_answers": ["Stick to current ABIs."]}
    fake = FakeOllama(valid_payload())

    ProjectMvpPlanner(config=config_for(root), ollama=fake).plan(project)

    system_prompt = fake.calls[0]["messages"][0]["content"]
    assert "accepted founder answers are binding founder constraints" in system_prompt.lower()
    assert "must not return needs_decision for a choice already answered there" in system_prompt.lower()


def test_planner_correction_call_forbids_another_decision_request(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    project = project_record(root)
    project["metadata"] = {
        "planner_founder_answers": ["Stick to current ABIs."],
        "planner_rejected_duplicate_decision": '{"question": "Which ABIs?"}',
    }
    fake = FakeOllama(valid_payload())

    ProjectMvpPlanner(config=config_for(root), ollama=fake).plan(project)

    assert fake.calls[0]["format"]["properties"]["needs_decision"] == {"type": "null"}


def test_planner_prompt_excludes_code_generated_and_legacy_material(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path, "The Dark Index")
    (root / "app.py").write_text("SECRET_SOURCE_CODE", encoding="utf-8")
    (root / "legacy").mkdir()
    (root / "legacy" / "old-spec.md").write_text("POOR_OLD_SPEC", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "generated.md").write_text("GENERATED_SPEC", encoding="utf-8")
    outside = root.parent / "outside.md"
    outside.write_text("OUTSIDE_PROJECT", encoding="utf-8")
    fake = FakeOllama(valid_payload())

    ProjectMvpPlanner(config=config_for(root), ollama=fake).plan(project_record(root))

    prompt = "\n".join(
        str(message["content"]) for message in fake.calls[0]["messages"]
    )
    assert MVP_DOC.strip() in prompt
    assert "SECRET_SOURCE_CODE" not in prompt
    assert "POOR_OLD_SPEC" not in prompt
    assert "GENERATED_SPEC" not in prompt
    assert "OUTSIDE_PROJECT" not in prompt


def test_planner_prompt_excludes_native_build_artifacts(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    generated = root / "android" / "app" / ".cxx" / "Debug" / "CMakeCache.txt"
    generated.parent.mkdir(parents=True)
    generated.write_text("GENERATED_NATIVE_BUILD_ARTIFACT", encoding="utf-8")
    fake = FakeOllama(valid_payload())

    ProjectMvpPlanner(config=config_for(root), ollama=fake).plan(project_record(root))

    prompt = "\n".join(str(message["content"]) for message in fake.calls[0]["messages"])
    assert "GENERATED_NATIVE_BUILD_ARTIFACT" not in prompt


def test_planner_rejects_source_input_that_would_exhaust_model_context(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    (root / "long-spec.md").write_text("x" * 100_000, encoding="utf-8")

    with pytest.raises(ValueError, match="bounded planning prompt"):
        ProjectMvpPlanner(config=config_for(root), ollama=FakeOllama(valid_payload())).plan(
            project_record(root)
        )


def test_planner_rejects_project_outside_project_intake(tmp_path: Path) -> None:
    root = tmp_path / "unregistered" / "Same Ground"
    root.mkdir(parents=True)
    (root / "MVP.md").write_text(MVP_DOC, encoding="utf-8")
    config = KernelConfig(
        paths=PathConfig(hot_root=tmp_path / "brain"),
        ollama=OllamaConfig(coding_agent_model="local-coder:latest"),
    )

    with pytest.raises(ValueError, match="registered project-intake root"):
        ProjectMvpPlanner(config=config, ollama=FakeOllama(valid_payload())).plan(
            project_record(root)
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["criteria"].__setitem__(
                1, {**payload["criteria"][1], "id": "resource_search"}
            ),
            "duplicate",
        ),
        (
            lambda payload: payload["criteria"][0].__setitem__("source", "missing.md"),
            "document source",
        ),
        (
            lambda payload: payload["criteria"][0].__setitem__("source", "../outside.md"),
            "document source",
        ),
    ],
)
def test_planner_rejects_invalid_or_uncited_criteria(
    tmp_path: Path, mutate, message: str
) -> None:
    root = make_documented_project(tmp_path)
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        ProjectMvpPlanner(config=config_for(root), ollama=FakeOllama(payload)).plan(
            project_record(root)
        )


def test_ambiguous_documentation_returns_bounded_decision_request(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    payload = {
        "criteria": [],
        "external_boundaries": [],
        "needs_decision": {
            "question": "Which responder identity model belongs in the MVP?",
            "recommendation": "Start with device-local profiles.",
            "options": [
                {"option": "Device-local profiles", "impact": "No account backend."},
                {"option": "Email accounts", "impact": "Adds identity infrastructure."},
            ],
        },
    }

    result = ProjectMvpPlanner(
        config=config_for(root), ollama=FakeOllama(payload)
    ).plan(project_record(root))

    assert result.criteria == []
    assert result.needs_decision["recommendation"] == "Start with device-local profiles."
    assert len(result.needs_decision["options"]) == 2


def test_planner_rejects_unbounded_decision_request(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    payload = {
        "criteria": [],
        "external_boundaries": [],
        "needs_decision": {
            "question": "Which approach?",
            "recommendation": "A",
            "options": [{"option": "A", "impact": "One option is not a decision."}],
        },
    }

    with pytest.raises(ValueError, match="2-3"):
        ProjectMvpPlanner(config=config_for(root), ollama=FakeOllama(payload)).plan(
            project_record(root)
        )


def test_unchanged_documents_produce_stable_hash_and_plan_revision(tmp_path: Path) -> None:
    root = make_documented_project(tmp_path)
    fake = FakeOllama(valid_payload())
    planner = ProjectMvpPlanner(config=config_for(root), ollama=fake)

    first = planner.plan(project_record(root))
    second = planner.plan(project_record(root))

    assert first.source_hash == second.source_hash
    assert first.plan_revision == second.plan_revision
    (root / "MVP.md").write_text(MVP_DOC + "\nAdd a favorites view.\n", encoding="utf-8")
    third = planner.plan(project_record(root))
    assert third.source_hash != first.source_hash
    assert third.plan_revision != first.plan_revision


def test_project_intake_autonomy_config_loads_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[project_intake]
autonomy_enabled = true
autonomy_max_workers = 2
autonomy_lease_seconds = 901
autonomy_repair_attempts = 3
autonomy_reconcile_seconds = 61
""",
        encoding="utf-8",
    )

    config = load_config(config_path).project_intake

    assert config.autonomy_enabled is True
    assert config.autonomy_max_workers == 2
    assert config.autonomy_lease_seconds == 901
    assert config.autonomy_repair_attempts == 3
    assert config.autonomy_reconcile_seconds == 61
