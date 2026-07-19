"""Tests for the data-class egress gate.

These pin the design invariants: local-only is inert, credentials never leave,
no automatic fallback, per-request auth is founder-bound and single-purpose, and
the whole thing fails closed.
"""
from __future__ import annotations

import pytest

from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.egress import (
    DataClass,
    Disposition,
    EgressAuthorization,
    EgressPolicy,
    EgressRequest,
    VendorTier,
    Verdict,
    active_grant_for,
    approve_egress_grant,
    authorize_egress,
    consume_grant,
    deny_egress_grant,
    list_pending_grants,
    parse_standing_grants,
)

PHRASE = "make the jump to hyperspace"


def _req(data_class: DataClass, vendor: str, request_id: str = "r1") -> EgressRequest:
    return EgressRequest(request_id=request_id, data_class=data_class, vendor=vendor, purpose="test")


def _db(tmp_path) -> KernelDatabase:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return db


# ---- Invariant 1: local-only is inert-by-default --------------------------
def test_local_only_denies_every_non_local_vendor() -> None:
    gate = EgressPolicy(provider_policy="local_only")
    for vendor in ("openai", "anthropic", "deepgram", "elevenlabs", "public_web", "openclaw", "sms_gateway"):
        decision = gate.decide(_req(DataClass.PUBLIC_DERIVED, vendor))
        assert decision.verdict is Verdict.DENY, vendor
        assert decision.matched_rule == "policy.local_only"


def test_local_only_even_denies_with_a_valid_authorization() -> None:
    """A per-request grant cannot override local_only — the policy overlay runs first."""
    gate = EgressPolicy(provider_policy="local_only")
    req = _req(DataClass.SOURCE_CODE, "anthropic")
    auth = EgressAuthorization(request_id="r1", data_class=DataClass.SOURCE_CODE, vendor="anthropic")
    decision = gate.decide(req, auth)
    assert decision.verdict is Verdict.DENY
    assert decision.matched_rule == "policy.local_only"


def test_local_loopback_always_allowed_even_under_local_only() -> None:
    gate = EgressPolicy(provider_policy="local_only")
    decision = gate.decide(_req(DataClass.FOUNDER_STATE, "local_ollama"))
    assert decision.verdict is Verdict.ALLOW
    assert decision.matched_rule == "local.loopback"


# ---- Invariant 2: credentials never leave ---------------------------------
def test_credentials_forbidden_everywhere_under_any_policy() -> None:
    for policy in ("local_only", "local_preferred", "cloud_allowed"):
        gate = EgressPolicy(provider_policy=policy)
        decision = gate.decide(_req(DataClass.CREDENTIALS, "anthropic"))
        assert decision.verdict is Verdict.DENY, policy
    # even with a (bogus) matching authorization it stays denied
    gate = EgressPolicy(provider_policy="cloud_allowed")
    auth = EgressAuthorization(request_id="r1", data_class=DataClass.CREDENTIALS, vendor="anthropic")
    decision = gate.decide(_req(DataClass.CREDENTIALS, "anthropic"), auth)
    assert decision.verdict is Verdict.DENY
    assert decision.matched_rule == "deny.credentials"


# ---- The matrix comes alive only when the policy is raised ----------------
def test_per_request_cell_asks_for_authorization_when_policy_raised() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    decision = gate.decide(_req(DataClass.FOUNDER_BRIEF, "anthropic"))
    assert decision.verdict is Verdict.AUTH_REQUIRED
    assert decision.matched_rule == "matrix.per_request_needed"


def test_per_request_cell_allows_with_matching_founder_authorization() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    req = _req(DataClass.FOUNDER_BRIEF, "anthropic")
    auth = EgressAuthorization(
        request_id="r1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic", typed_phrase_ok=True
    )
    decision = gate.decide(req, auth)
    assert decision.verdict is Verdict.ALLOW
    assert decision.matched_rule == "matrix.per_request_granted"


def test_raw_founder_state_is_forbidden_to_cloud_even_with_authorization() -> None:
    """Decision #2: raw founder state never leaves — only a curated founder_brief
    may reach a cloud model, and only per request."""
    gate = EgressPolicy(provider_policy="cloud_allowed")
    for vendor in ("anthropic", "openai", "public_web", "sms_gateway", "openclaw"):
        req = _req(DataClass.FOUNDER_STATE, vendor)
        auth = EgressAuthorization(request_id="r1", data_class=DataClass.FOUNDER_STATE, vendor=vendor)
        decision = gate.decide(req, auth)
        assert decision.verdict is Verdict.DENY, vendor
        assert decision.matched_rule == "matrix.forbidden"


def test_operational_to_channel_needs_per_request_grant() -> None:
    """Decision #3: no standing channel egress until cross-channel founder auth ships."""
    gate = EgressPolicy(provider_policy="local_preferred")
    decision = gate.decide(_req(DataClass.OPERATIONAL, "openclaw"))
    assert decision.verdict is Verdict.AUTH_REQUIRED
    assert decision.matched_rule == "matrix.per_request_needed"


# ---- Invariant 3: authorization is founder-bound and single-purpose -------
def test_authorization_from_non_founder_is_ignored() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    req = _req(DataClass.SOURCE_CODE, "openai")
    # a grant "authored" by a channel message / tool result must not unlock egress
    auth = EgressAuthorization(
        request_id="r1", data_class=DataClass.SOURCE_CODE, vendor="openai", granted_by="channel_message"
    )
    decision = gate.decide(req, auth)
    assert decision.verdict is Verdict.AUTH_REQUIRED


def test_authorization_does_not_leak_across_requests() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    auth = EgressAuthorization(request_id="r1", data_class=DataClass.SOURCE_CODE, vendor="openai")
    # same class+vendor, different request id -> must NOT be honored
    other = EgressRequest(request_id="r2", data_class=DataClass.SOURCE_CODE, vendor="openai")
    decision = gate.decide(other, auth)
    assert decision.verdict is Verdict.AUTH_REQUIRED


def test_authorization_does_not_leak_across_vendor() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    auth = EgressAuthorization(request_id="r1", data_class=DataClass.SOURCE_CODE, vendor="openai")
    decision = gate.decide(_req(DataClass.SOURCE_CODE, "anthropic"), auth)
    assert decision.verdict is Verdict.AUTH_REQUIRED


# ---- Standing grants (the research lane) -----------------------------------
def test_standing_grant_required_for_standing_cell() -> None:
    # (PUBLIC_DERIVED, public_web) is a STANDING cell; without the grant enabled it denies.
    gate = EgressPolicy(provider_policy="local_preferred")
    decision = gate.decide(_req(DataClass.PUBLIC_DERIVED, "public_web"))
    assert decision.verdict is Verdict.DENY
    assert decision.matched_rule == "matrix.standing_missing"


def test_standing_grant_allows_when_enabled() -> None:
    gate = EgressPolicy(
        provider_policy="local_preferred",
        standing_grants=frozenset({(DataClass.PUBLIC_DERIVED, "public_web")}),
    )
    decision = gate.decide(_req(DataClass.PUBLIC_DERIVED, "public_web"))
    assert decision.verdict is Verdict.ALLOW
    assert decision.matched_rule == "matrix.standing_grant"


def test_cloud_voice_cells_are_forbidden_even_with_standing_grants() -> None:
    """Voice went actually-local 2026-07-17; the dead cloud lanes are now
    FORBIDDEN at the matrix, so a leftover (or re-added) standing grant in
    config can no longer resurrect Deepgram STT / ElevenLabs TTS. Reverting is
    a deliberate two-step: flip the cell AND re-grant."""
    gate = EgressPolicy(
        provider_policy="cloud_allowed",
        standing_grants=frozenset(
            {(DataClass.FOUNDER_AUDIO, "deepgram"), (DataClass.REPLY_TEXT, "elevenlabs")}
        ),
    )
    for data_class, vendor in ((DataClass.FOUNDER_AUDIO, "deepgram"), (DataClass.REPLY_TEXT, "elevenlabs")):
        decision = gate.decide(_req(data_class, vendor))
        assert decision.verdict is Verdict.DENY, vendor
        assert decision.matched_rule == "matrix.forbidden"


def test_forbidden_cell_cannot_be_unlocked() -> None:
    # SOURCE_CODE -> CLOUD_SERVICE is FORBIDDEN; even a matching auth stays denied.
    gate = EgressPolicy(provider_policy="cloud_allowed")
    req = _req(DataClass.SOURCE_CODE, "elevenlabs")
    auth = EgressAuthorization(request_id="r1", data_class=DataClass.SOURCE_CODE, vendor="elevenlabs")
    decision = gate.decide(req, auth)
    assert decision.verdict is Verdict.DENY
    assert decision.matched_rule == "matrix.forbidden"


# ---- Invariant 4: fail closed ---------------------------------------------
def test_unknown_vendor_denied() -> None:
    gate = EgressPolicy(provider_policy="cloud_allowed")
    decision = gate.decide(_req(DataClass.PUBLIC_DERIVED, "some_new_saas"))
    assert decision.verdict is Verdict.DENY
    assert decision.matched_rule == "vendor.unknown"


# ---- Invariant 5: redacted audit ------------------------------------------
def test_audit_record_is_redacted() -> None:
    gate = EgressPolicy(provider_policy="local_preferred")
    decision = gate.decide(_req(DataClass.FOUNDER_BRIEF, "anthropic"))
    record = decision.audit_record()
    assert record["data_class"] == "founder_brief"
    assert record["vendor"] == "anthropic"
    assert record["verdict"] == "auth_required"
    # no payload, no purpose text, no secret material in the audit row
    assert "payload" not in record
    assert "purpose" not in record


# ---- Matrix shape sanity --------------------------------------------------
def test_every_data_class_has_a_full_matrix_row() -> None:
    from cofounder_kernel.egress import DEFAULT_MATRIX

    non_local_tiers = {t for t in VendorTier if t is not VendorTier.LOCAL}
    for data_class in DataClass:
        row = DEFAULT_MATRIX.get(data_class, {})
        missing = non_local_tiers - set(row)
        assert not missing, f"{data_class} missing tiers {missing}"
        assert all(isinstance(v, Disposition) for v in row.values())


# ---- Config parsing / from_config -----------------------------------------
def test_parse_standing_grants_valid() -> None:
    grants = parse_standing_grants(["founder_audio:deepgram", "reply_text:elevenlabs", ""])
    assert grants == frozenset({
        (DataClass.FOUNDER_AUDIO, "deepgram"),
        (DataClass.REPLY_TEXT, "elevenlabs"),
    })


def test_parse_standing_grants_rejects_malformed_and_unknown() -> None:
    for bad in ["no_colon", "not_a_class:deepgram", "founder_audio:not_a_vendor"]:
        with pytest.raises(ValueError):
            parse_standing_grants([bad])


def test_from_config_reads_policy_and_grants() -> None:
    from cofounder_kernel.config import EgressConfig, KernelConfig, OllamaConfig

    config = KernelConfig(
        ollama=OllamaConfig(provider_policy="local_preferred"),
        egress=EgressConfig(standing_grants=("public_derived:public_web", "founder_audio:deepgram")),
    )
    gate = EgressPolicy.from_config(config)
    # the standing grant is live...
    assert gate.decide(_req(DataClass.PUBLIC_DERIVED, "public_web")).verdict is Verdict.ALLOW
    # ...a per-request cell is untouched by standing grants...
    assert gate.decide(_req(DataClass.OPERATIONAL, "public_web")).verdict is Verdict.AUTH_REQUIRED
    # ...and a grant naming a FORBIDDEN cell (the dead cloud-voice lane) is inert
    assert gate.decide(_req(DataClass.FOUNDER_AUDIO, "deepgram")).verdict is Verdict.DENY


def test_from_config_default_is_local_only_and_inert() -> None:
    from cofounder_kernel.config import KernelConfig

    gate = EgressPolicy.from_config(KernelConfig())
    assert gate.provider_policy == "local_only"
    # default posture: cloud voice refused
    assert gate.decide(_req(DataClass.FOUNDER_AUDIO, "deepgram")).verdict is Verdict.DENY


# ---- Per-request grant flow ------------------------------------------------
def _pending_grants(db: KernelDatabase) -> list:
    from cofounder_kernel.egress import GRANT_SOURCE_TYPE

    return [r for r in db.list_approval_requests(status="pending", limit=100) if r.source_type == GRANT_SOURCE_TYPE]


def test_authorize_files_a_pending_grant_on_auth_required(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic", purpose="strategy review")

    decision = authorize_egress(db, gate, req, preview="Q3 strategy brief (curated)")
    assert decision.verdict is Verdict.AUTH_REQUIRED
    # a pending founder grant now exists, carrying the preview but not a payload
    pend = list_pending_grants(db)
    assert len(pend) == 1
    assert pend[0]["data_class"] == "founder_brief" and pend[0]["vendor"] == "anthropic"
    assert pend[0]["preview"] == "Q3 strategy brief (curated)"


def test_authorize_is_idempotent_no_duplicate_grants(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    authorize_egress(db, gate, req)
    authorize_egress(db, gate, req)
    assert len(_pending_grants(db)) == 1


def test_approve_then_allow(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.SOURCE_CODE, vendor="anthropic", purpose="review diff")

    assert authorize_egress(db, gate, req).verdict is Verdict.AUTH_REQUIRED
    grant_id = _pending_grants(db)[0].id
    result = approve_egress_grant(db, grant_id, typed_phrase=PHRASE)
    assert result["granted"] is True
    # now the same request is ALLOWed by the stored grant
    decision = authorize_egress(db, gate, req)
    assert decision.verdict is Verdict.ALLOW
    assert decision.matched_rule == "matrix.per_request_granted"


def test_grant_requires_typed_phrase(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    authorize_egress(db, gate, req)
    grant_id = _pending_grants(db)[0].id
    with pytest.raises(ValueError):
        approve_egress_grant(db, grant_id, typed_phrase="wrong words")
    # still not authorized
    assert authorize_egress(db, gate, req).verdict is Verdict.AUTH_REQUIRED


def test_grant_is_single_use(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    authorize_egress(db, gate, req)
    approve_egress_grant(db, _pending_grants(db)[0].id, typed_phrase=PHRASE)
    assert authorize_egress(db, gate, req).verdict is Verdict.ALLOW
    # the caller performs the send, then consumes the grant
    assert consume_grant(db, req) is True
    # a replay is no longer authorized — it files a fresh pending grant
    assert authorize_egress(db, gate, req).verdict is Verdict.AUTH_REQUIRED
    assert consume_grant(db, req) is False  # nothing left to consume


def test_grant_does_not_match_a_different_request(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    granted = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    authorize_egress(db, gate, granted)
    approve_egress_grant(db, _pending_grants(db)[0].id, typed_phrase=PHRASE)
    # a different operation id, and a different vendor, each get nothing
    other_op = EgressRequest(request_id="op-2", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    other_vendor = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="openai")
    assert active_grant_for(db, other_op) is None
    assert active_grant_for(db, other_vendor) is None
    assert active_grant_for(db, granted) is not None


def test_deny_leaves_it_unauthorized(tmp_path) -> None:
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="local_preferred")
    req = EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    authorize_egress(db, gate, req)
    deny_egress_grant(db, _pending_grants(db)[0].id)
    # denied: authorize files a NEW pending request (deny doesn't grant anything)
    assert authorize_egress(db, gate, req).verdict is Verdict.AUTH_REQUIRED


def test_forbidden_and_local_only_never_file_a_grant(tmp_path) -> None:
    # raw founder_state is FORBIDDEN even under a raised policy -> DENY, no grant
    db = _db(tmp_path)
    gate = EgressPolicy(provider_policy="cloud_allowed")
    d1 = authorize_egress(db, gate, EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_STATE, vendor="anthropic"))
    assert d1.verdict is Verdict.DENY
    assert _pending_grants(db) == []
    # and under local_only, a per-request cell DENYs (inert) -> no grant
    db2 = _db(tmp_path / "b")
    gate2 = EgressPolicy(provider_policy="local_only")
    d2 = authorize_egress(db2, gate2, EgressRequest(request_id="op-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic"))
    assert d2.verdict is Verdict.DENY
    assert _pending_grants(db2) == []
