"""Stage 3 (P1) regression tests — each pins a correctness bug the audit found."""
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.commitments import _is_overdue
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.critic import _parse_critique
from cofounder_kernel.devtools import _validate_args
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_failed_step_evidence_does_not_manufacture_thesis_conflict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    # Contradicting evidence with no linked assumption (like a failed action step).
    unlinked = client.post("/founder/evidence", json={
        "evidence_type": "action_step_outcome", "source": "plan:1:step:1",
        "reliability": "A", "strength": 80, "claim_contradicted": "Step 'try the thing' failed.",
    })
    conflicts_after_unlinked = client.get("/founder/thesis-conflicts")

    assert unlinked.status_code == 200
    assert conflicts_after_unlinked.json()["items"] == []  # no phantom conflict

    # Contradicting evidence *linked* to a real assumption still creates a conflict.
    assumption = client.post("/founder/assumptions", json={"statement": "Solo founders pay $99.", "confidence": 70})
    linked = client.post("/founder/evidence", json={
        "evidence_type": "customer interview", "source": "calls",
        "reliability": "C", "strength": 80,
        "claim_contradicted": "Willingness clusters at $29.",
        "linked_assumption_id": assumption.json()["id"],
    })
    conflicts = client.get("/founder/thesis-conflicts")

    assert linked.status_code == 200
    assert len(conflicts.json()["items"]) == 1


def test_date_only_commitment_is_not_overdue_until_end_of_day() -> None:
    open_today = {"due_at": "2026-07-12", "status": "open"}
    assert _is_overdue(open_today, "2026-07-12T00:00:01+00:00") is False  # midnight of due date
    assert _is_overdue(open_today, "2026-07-12T14:30:00+00:00") is False  # afternoon of due date
    assert _is_overdue(open_today, "2026-07-13T00:00:01+00:00") is True   # next day
    assert _is_overdue({"due_at": "2026-07-11", "status": "open"}, "2026-07-12T09:00:00+00:00") is True
    # Full timestamps still compare precisely.
    assert _is_overdue({"due_at": "2020-01-01T00:00:00+00:00", "status": "open"}, "2026-07-12T00:00:00+00:00") is True


def test_contrarian_confidence_adjustment_cannot_be_positive() -> None:
    raised = _parse_critique('{"verdict": "proceed", "confidence_adjustment": 8}')
    assert raised["confidence_adjustment"] == 0  # a red team may never raise confidence
    clamped_low = _parse_critique('{"verdict": "do_not_proceed", "confidence_adjustment": -400}')
    assert clamped_low["confidence_adjustment"] == -50
    ordinary = _parse_critique('{"verdict": "proceed_with_changes", "confidence_adjustment": -15}')
    assert ordinary["confidence_adjustment"] == -15


def test_empty_check_eval_case_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    no_checks = client.post("/evals/cases", json={
        "name": "always-fails", "executor": "generate", "prompt": "hi", "checks": [],
    })
    with_checks = client.post("/evals/cases", json={
        "name": "ok-case", "executor": "generate", "prompt": "hi",
        "checks": [{"type": "contains", "value": "x"}],
    })

    assert no_checks.status_code == 400
    assert "at least one check" in no_checks.json()["detail"]
    assert with_checks.status_code == 200


def test_connector_secret_detection_catches_lookalike_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    for leaky in ("app_password", "client_secret_value", "imap_pwd", "access_key"):
        resp = client.post("/connectors", json={
            "name": f"c-{leaky}".replace("_", "-"), "connector_type": "imap",
            "config": {"host": "h", "username": "u", "password_env": "X", leaky: "sekret"},
        })
        assert resp.status_code == 400, f"{leaky} should be rejected"
        assert "must not contain secrets" in resp.json()["detail"]

    # The sanctioned *_env reference is allowed.
    ok = client.post("/connectors", json={
        "name": "clean", "connector_type": "imap",
        "config": {"host": "h", "username": "u", "password_env": "ZADE_PW"},
    })
    assert ok.status_code == 200


def test_devtools_rejects_file_writing_flags() -> None:
    for bad in ("--junit-xml=out.xml", "--output=report.txt", "--basetemp=/tmp/x", "--cov-report=html"):
        try:
            _validate_args([bad])
            assert False, f"expected rejection of {bad}"
        except ValueError as exc:
            assert "file-writing flag" in str(exc)
    # Ordinary read-only flags are fine.
    assert _validate_args(["-q", "-k", "smoke"]) == ["-q", "-k", "smoke"]


def test_sms_gateway_url_must_be_http(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    client.post("/notify/channels/sms", json={
        "enabled": True, "min_severity": "info", "quiet_start": "", "quiet_end": "",
        "recipients": ["+15550000000"],
        "config": {"gateway_url": "ftp://evil.example/send", "to": "+15550000000"},
    })
    sent = client.post("/notify", json={"topic": "t", "title": "hi"})
    sms = [d for d in sent.json()["item"]["deliveries"] if d["channel"] == "sms"][0]
    assert sms["status"] == "failed"
    assert "valid http(s) URL" in sms["detail"]
