"""Stage 6 — Synthetic-Intelligence-Engine primitives.

These test the reasoning core that makes Zade more than a stateless LLM: how
beliefs update from evidence.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.founder import _bayesian_confidence, _evidence_llr
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_evidence_llr_sign_and_grade_weighting() -> None:
    strong_support = _evidence_llr({"reliability": "A", "strength": 100, "claim_supported": "x"})
    strong_against = _evidence_llr({"reliability": "A", "strength": 100, "claim_contradicted": "x"})
    weak_support = _evidence_llr({"reliability": "C", "strength": 50, "claim_supported": "x"})

    assert strong_support > 0 and strong_against < 0
    assert strong_support == -strong_against            # symmetric in magnitude
    assert strong_support > weak_support                # grade/strength scale the signal
    # Junk evidence carries no signal.
    assert _evidence_llr({"reliability": "F", "strength": 100, "claim_contradicted": "x"}) == 0.0
    assert _evidence_llr({"reliability": "A", "strength": 0, "claim_supported": "x"}) == 0.0


def test_bayesian_update_has_no_forced_floor() -> None:
    # The old model forced a >=1 move even for worthless evidence; the new one does not.
    assert _bayesian_confidence(70, {"reliability": "F", "strength": 100, "claim_contradicted": "x"}) == 70
    assert _bayesian_confidence(70, {"reliability": "A", "strength": 0, "claim_supported": "x"}) == 70
    # Evidence with no support/contradiction claim moves nothing.
    assert _bayesian_confidence(70, {"reliability": "A", "strength": 100}) == 70


def test_bayesian_update_moves_belief_in_the_right_direction() -> None:
    contradicted = {"reliability": "A", "strength": 100, "claim_contradicted": "x"}
    supported = {"reliability": "A", "strength": 100, "claim_supported": "x"}
    assert _bayesian_confidence(70, contradicted) < 70
    assert _bayesian_confidence(30, supported) > 30
    # Bounded to [0, 100] even under repeated strong evidence.
    v = 50
    for _ in range(20):
        v = _bayesian_confidence(v, supported)
    assert 0 <= v <= 100


def test_bayesian_update_is_prior_weighted() -> None:
    """A confident belief (90%) should resist the same evidence more than an
    uncertain one (50%) — the hallmark of Bayesian, not linear, updating."""
    ev = {"reliability": "A", "strength": 100, "claim_contradicted": "x"}
    drop_from_50 = 50 - _bayesian_confidence(50, ev)
    drop_from_90 = 90 - _bayesian_confidence(90, ev)
    assert drop_from_50 > drop_from_90


def test_contrarian_review_moves_the_belief_it_targets(tmp_path: Path, monkeypatch) -> None:
    """Self-critique must be consequential: a red-team review of an assumption
    actually lowers that assumption's confidence and logs a confidence event."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    assumption = client.post("/founder/assumptions", json={"statement": "Solo founders pay $99.", "confidence": 70})
    aid = assumption.json()["id"]
    review = client.post("/founder/contrarian-reviews", json={
        "subject_type": "assumption", "subject_id": aid,
        "title": "Skeptic: pricing is unproven", "confidence_adjustment": -15,
    })
    listed = client.get("/founder/assumptions")
    events = client.get("/founder/confidence-events")

    assert review.status_code == 200
    moved = next(a for a in listed.json()["items"] if a["id"] == aid)
    assert moved["confidence"] == 55  # 70 - 15, the loop is closed
    assert any(
        e["subject_type"] == "assumption" and e["subject_id"] == aid and e["new_confidence"] == 55
        for e in events.json()["items"]
    )

    # A review of a non-confidence subject (a runtime event) moves nothing and does not error.
    ok = client.post("/founder/contrarian-reviews", json={
        "subject_type": "runtime_event", "subject_id": 999,
        "title": "Auto pass", "confidence_adjustment": -10,
    })
    assert ok.status_code == 200


def test_surfacing_dedupes_multiple_signals_for_one_subject(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    commitment = client.post("/commitments", json={"title": "Ship the thing", "due_at": "2020-01-01"})
    cid = commitment.json()["item"]["id"]
    # Renegotiate twice (still in the past) -> the commitment is BOTH overdue and drifting.
    client.post(f"/commitments/{cid}/renegotiate", json={"due_at": "2020-02-01"})
    client.post(f"/commitments/{cid}/renegotiate", json={"due_at": "2020-03-01"})

    attention = client.get("/surface/attention")
    rows = [i for i in attention.json()["items"] if i["subject_type"] == "commitment" and i["subject_id"] == cid]

    assert len(rows) == 1  # deduped to a single row, not one overdue + one drifting
    assert rows[0]["kind"] == "commitment_overdue"  # the higher-severity signal wins


def test_prediction_calibration_reports_brier_and_direction(tmp_path: Path, monkeypatch) -> None:
    """Metacognition: Zade should know not just its error, but whether it is
    systematically over- or under-confident."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    # Two high-confidence predictions that both turned out false => overconfident.
    for prob in (0.9, 0.8):
        pred = client.post("/founder/predictions", json={"prediction": f"bet@{prob}", "probability": prob})
        client.post("/founder/predictions/score", json={"prediction_id": pred.json()["id"], "outcome": "false"})

    acc = client.get("/founder/dashboard").json()["prediction_accuracy"]
    assert acc["scored_count"] == 2
    assert acc["brier_score"] == round((0.81 + 0.64) / 2, 4)  # 0.725
    assert acc["directional_bias"] > 0  # predicted higher than reality
    assert acc["calibration_note"] == "overconfident"


def test_evidence_moves_every_linked_belief_not_just_assumptions(tmp_path: Path, monkeypatch) -> None:
    """A single experiment result should update the confidence of every belief it
    bears on — the goal and the strategic bet, not only the assumption that the
    evidence happens to name. This is what makes the belief network coherent."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    goal = client.post("/founder/goals", json={"name": "Reach $10k MRR", "confidence": 60})
    gid = goal.json()["id"]
    bet = client.post("/founder/strategy-objects", json={
        "object_type": "bet", "title": "Self-serve beats sales-led", "confidence": 55,
    })
    bid = bet.json()["id"]
    assumption = client.post("/founder/assumptions", json={"statement": "Users will self-onboard.", "confidence": 50})
    aid = assumption.json()["id"]

    experiment = client.post("/experiments", json={
        "title": "Onboarding funnel test",
        "linked_goal_ids": [gid], "linked_bet_ids": [bid], "linked_assumption_ids": [aid],
    })
    eid = experiment.json()["item"]["id"]

    # Strong, high-reliability evidence that CONTRADICTS the thesis.
    added = client.post(f"/experiments/{eid}/evidence", json={
        "content": "Self-onboarding completion was 4%.",
        "reliability": "A", "strength": 100,
        "claim_contradicted": "Users will self-onboard.",
        "ingest_document": False,
    })
    assert added.status_code == 200

    goals = {g["id"]: g for g in client.get("/founder/goals").json()["items"]}
    bets = {b["id"]: b for b in client.get("/founder/strategy-objects").json()["items"]}
    assumptions = {a["id"]: a for a in client.get("/founder/assumptions").json()["items"]}

    # All three beliefs dropped from strong contradicting evidence.
    assert goals[gid]["confidence"] < 60
    assert bets[bid]["confidence"] < 55
    assert assumptions[aid]["confidence"] < 50

    # And each move is auditable as its own confidence event.
    events = client.get("/founder/confidence-events").json()["items"]
    subjects = {(e["subject_type"], e["subject_id"]) for e in events}
    assert ("goal", gid) in subjects
    assert ("bet", bid) in subjects
    assert ("assumption", aid) in subjects
