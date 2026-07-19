from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runner_images_are_digest_pinned_and_match_runtime_tags() -> None:
    python = (ROOT / "docker" / "build-runners" / "python.Dockerfile").read_text(
        encoding="utf-8"
    )
    node = (ROOT / "docker" / "build-runners" / "node.Dockerfile").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "scripts" / "build-runner-images.ps1").read_text(
        encoding="utf-8"
    )

    assert "FROM python:3.12-slim@sha256:" in python
    assert "pytest==9.1.1" in python
    assert "USER 10001:10001" in python
    assert "FROM node:22-bookworm-slim@sha256:" in node
    assert "USER node" in node
    assert 'python:3.12-local' in script
    assert 'node:22-local' in script
    assert '"--network", "none"' in script
    assert '"--read-only"' in script


def test_openai_setup_reads_key_securely_and_never_writes_it_to_repo() -> None:
    script = (ROOT / "scripts" / "configure-openai-review.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Read-Host "OpenAI API key" -AsSecureString' in script
    assert 'SetEnvironmentVariable("OPENAI_API_KEY", $plainKey, "User")' in script
    assert "ZeroFreeBSTR" in script
    assert "Set-Content" not in script
    assert "WriteAllText" not in script
