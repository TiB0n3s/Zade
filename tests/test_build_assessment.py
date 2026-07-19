from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cofounder_kernel.build_assessment import BuildAssessmentService
from cofounder_kernel.build_types import BuildTier
from cofounder_kernel.ollama import OllamaError


class FakeLocalClient:
    def __init__(self, reply: dict[str, Any] | str | Exception):
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        response = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return SimpleNamespace(response=response)


def test_greenfield_saas_and_mobile_requires_large(tmp_path: Path) -> None:
    result = BuildAssessmentService().assess(
        task="Build a SaaS backend with auth, billing, iOS and Android clients",
        workspace=tmp_path,
    )

    assert result.recommended_tier is BuildTier.LARGE
    assert "greenfield_saas_plus_mobile" in result.floor_rules
    assert set(result.dimensions) == {
        "product_surfaces",
        "external_integrations",
        "change_breadth",
        "data_and_security",
        "platform_and_release",
        "verification_burden",
        "novelty_and_ambiguity",
    }


def test_local_adjustment_cannot_lower_auth_and_billing_floor(tmp_path: Path) -> None:
    local = FakeLocalClient(
        {
            "score_adjustment": -40,
            "confidence": 0.9,
            "reasons": ["Looks familiar"],
            "unknowns": [],
        }
    )

    result = BuildAssessmentService(local_client=local).assess(
        task="Add Stripe billing and production authentication",
        workspace=tmp_path,
    )

    assert result.recommended_tier in {BuildTier.MEDIUM, BuildTier.LARGE}
    assert result.local_adjustment == 0
    assert result.final_score >= result.deterministic_score


def test_simple_change_without_local_model_stays_small_and_never_uses_cloud(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL")

    def reject_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("assessment attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", reject_network)
    result = BuildAssessmentService().assess(task="Rename a label", workspace=tmp_path)

    assert result.recommended_tier is BuildTier.SMALL
    assert result.local_adjustment == 0


def test_local_pass_uses_strict_schema_and_can_only_raise_score(tmp_path: Path) -> None:
    local = FakeLocalClient(
        {
            "score_adjustment": 12,
            "confidence": 0.88,
            "reasons": ["Release behavior is underspecified"],
            "unknowns": ["Rollback expectations"],
        }
    )

    result = BuildAssessmentService(local_client=local).assess(
        task="Add a customer export",
        acceptance="CSV export passes integration tests",
        workspace=tmp_path,
    )

    assert result.local_adjustment == 12
    assert result.final_score == result.deterministic_score + 12
    assert result.unknowns == ("Rollback expectations",)
    assert len(local.calls) == 1
    assert local.calls[0]["format"]["type"] == "object"
    assert local.calls[0]["format"]["additionalProperties"] is False
    prompt = local.calls[0]["messages"][1]["content"]
    assert str(tmp_path) not in prompt


def test_invalid_or_unavailable_local_output_is_conservative(tmp_path: Path) -> None:
    invalid = BuildAssessmentService(local_client=FakeLocalClient("not-json")).assess(
        task="Rename a label", workspace=tmp_path
    )
    unavailable = BuildAssessmentService(
        local_client=FakeLocalClient(OllamaError("offline"))
    ).assess(task="Rename a label", workspace=tmp_path)

    assert invalid.local_adjustment == unavailable.local_adjustment == 0
    assert invalid.confidence < 0.65
    assert unavailable.confidence < 0.65
    assert invalid.recommended_tier is BuildTier.MEDIUM
    assert unavailable.recommended_tier is BuildTier.MEDIUM


def test_repository_scan_is_bounded_to_safe_metadata_and_manifests(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"stripe": "1", "react": "1"}}), encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["fastapi", "sqlalchemy"]\n', encoding="utf-8"
    )
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=secret", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    (tmp_path / "asset.png").write_bytes(b"\x89PNG\r\n")

    result = BuildAssessmentService().assess(task="Extend the API", workspace=tmp_path)

    assert result.evidence["file_count"] == 3
    assert result.evidence["manifests"]["package.json"]["dependencies"] == [
        "react",
        "stripe",
    ]
    assert result.evidence["manifests"]["pyproject.toml"]["dependencies"] == [
        "fastapi",
        "sqlalchemy",
    ]
    scanned = result.evidence["scanned_paths"]
    assert ".env" not in scanned
    assert not any(path.startswith(".git/") for path in scanned)
    assert not any(path.startswith("node_modules/") for path in scanned)
    assert "asset.png" not in scanned
    assert "secret" not in json.dumps(result.evidence)
