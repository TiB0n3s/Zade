"""Tests for cross-channel founder authentication.

Pins the security properties: bind only via challenge-response, fail-closed for
unbound identities, the sender handle is never trusted (only the bound
external_id), capped authority, hashed codes, expiry, and revocation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cofounder_kernel.channel_auth import ChannelAuth, ChannelAuthError
from cofounder_kernel.db import KernelDatabase


def _auth(tmp_path: Path) -> tuple[ChannelAuth, KernelDatabase]:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return ChannelAuth(db), db


def test_enroll_confirm_authenticate_happy_path(tmp_path: Path) -> None:
    auth, db = _auth(tmp_path)
    enr = auth.begin_enrollment("telegram", label="Ellie phone")
    assert enr["code"] and enr["enrollment_id"]
    # only the HASH is stored, never the raw code
    with db.connect() as c:
        row = c.execute("SELECT code_hash FROM channel_enrollments WHERE id=?", (enr["enrollment_id"],)).fetchone()
    assert row["code_hash"] != enr["code"] and len(row["code_hash"]) == 64

    ident = auth.confirm_enrollment("telegram", "chat-12345", enr["code"])
    assert ident.authenticated and ident.is_founder and ident.max_tier == "L0_READ"

    again = auth.authenticate("telegram", "chat-12345")
    assert again.authenticated and again.binding_id == ident.binding_id and again.max_tier == "L0_READ"


def test_unbound_identity_is_fail_closed(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    a = auth.authenticate("telegram", "chat-999")
    assert a.authenticated is False and a.is_founder is False and a.max_tier is None


def test_sender_handle_is_never_trusted_only_the_bound_id(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    enr = auth.begin_enrollment("telegram")
    auth.confirm_enrollment("telegram", "chat-real", enr["code"])
    # a different account on the same channel — spoofed handle, different id — gets nothing
    assert auth.authenticate("telegram", "chat-attacker").authenticated is False
    assert auth.authenticate("telegram", "chat-real").authenticated is True
    # and the same id on a DIFFERENT channel is separate (not authenticated)
    assert auth.authenticate("slack", "chat-real").authenticated is False


def test_wrong_code_is_refused_and_binds_nothing(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    auth.begin_enrollment("telegram")
    with pytest.raises(ChannelAuthError):
        auth.confirm_enrollment("telegram", "chat-1", "deadbeef")
    assert auth.list_bindings() == []


def test_expired_enrollment_is_refused(tmp_path: Path) -> None:
    auth, db = _auth(tmp_path)
    enr = auth.begin_enrollment("telegram")
    with db.connect() as c:
        c.execute("UPDATE channel_enrollments SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (enr["enrollment_id"],))
    with pytest.raises(ChannelAuthError):
        auth.confirm_enrollment("telegram", "chat-1", enr["code"])
    assert auth.list_bindings() == []


def test_revoke_removes_authority(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    enr = auth.begin_enrollment("telegram")
    ident = auth.confirm_enrollment("telegram", "chat-1", enr["code"])
    auth.revoke(ident.binding_id)
    assert auth.authenticate("telegram", "chat-1").authenticated is False
    assert auth.list_bindings() == []  # active-only view
    assert auth.list_bindings(include_revoked=True)  # still present, revoked


def test_capped_authority_default_and_deliberate_raise(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    enr = auth.begin_enrollment("slack")
    ident = auth.confirm_enrollment("slack", "U123", enr["code"])
    # default ceiling L0_READ: can read, cannot even memory-write autonomously
    assert auth.caps(ident, "L0_READ") is True
    assert auth.caps(ident, "L1_MEMORY_WRITE") is False
    assert auth.caps(ident, "L3_EXTERNAL_ACTION") is False
    # founder raises the ceiling deliberately
    auth.set_max_tier(ident.binding_id, "L1_MEMORY_WRITE")
    raised = auth.authenticate("slack", "U123")
    assert auth.caps(raised, "L1_MEMORY_WRITE") is True
    assert auth.caps(raised, "L3_EXTERNAL_ACTION") is False
    # an unauthenticated identity caps to nothing
    assert auth.caps(auth.authenticate("slack", "nobody"), "L0_READ") is False


def test_reconfirm_refreshes_a_single_binding(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    e1 = auth.begin_enrollment("telegram")
    auth.confirm_enrollment("telegram", "chat-1", e1["code"])
    e2 = auth.begin_enrollment("telegram")
    auth.confirm_enrollment("telegram", "chat-1", e2["code"])
    # re-enrollment upserts — exactly one active binding for that identity
    assert len([b for b in auth.list_bindings() if b["external_id"] == "chat-1"]) == 1


def test_invalid_tier_rejected(tmp_path: Path) -> None:
    auth, _ = _auth(tmp_path)
    enr = auth.begin_enrollment("telegram")
    ident = auth.confirm_enrollment("telegram", "chat-1", enr["code"])
    with pytest.raises(ChannelAuthError):
        auth.set_max_tier(ident.binding_id, "L9_ROOT")
