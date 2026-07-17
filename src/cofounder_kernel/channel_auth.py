"""Cross-channel founder authentication — the prerequisite for any channel ingress.

A messaging channel (Telegram, Slack, Discord, WhatsApp) would route inbound
messages into ``/runtime/respond``, which has founder-command-as-authorization
semantics and real authority. So before a channel message can carry founder
authority, Zade must *know the human on the far end is the founder* — and the
sender handle/username is NOT proof (it is spoofable, and forwarded messages
carry the original sender). This module binds a channel identity to the founder
the only way that's sound: a challenge-response that proves control of BOTH the
trusted local kernel AND the channel account.

Threat model & honest limits
-----------------------------
- **Enrollment** proves that, at bind time, one party controlled the local kernel
  (the endpoint is mutation-token gated) AND the channel account (they echoed a
  one-time code back through it). That binds ``(channel, external_id) -> founder``.
- **Ongoing trust is only as strong as the channel account.** If the founder's
  Telegram is later compromised, the attacker inherits the binding. This is
  inherent to any channel-based auth and cannot be engineered away here.
- Therefore authority is **capped**. A binding carries a ``max_tier`` ceiling,
  default ``L0_READ`` (converse + read). L2+/L3 actions must still route to LOCAL
  approval (the mutation-token'd console) regardless of the ceiling — the design
  principle is **channels propose, only the local surface approves**. A
  compromised channel can spam proposals; it cannot execute a destructive action.
- **Fail-closed.** An unbound (channel, external_id) authenticates to *nothing*:
  no identity, no authority. It is untrusted input, like a web page.

This is the identity primitive a channel adapter (e.g. OpenClaw) consumes; there
is no adapter wired yet. Only the code's hash is stored, never the raw code.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import KernelDatabase, utc_now

_BIND_RE = re.compile(r"^\s*/?bind\s+(\S+)\s*$", re.IGNORECASE)


def parse_bind_command(text: str) -> str | None:
    """Extract the enrollment code from a ``/bind <code>`` (or ``bind <code>``)
    message, else None. Enrollment is explicit so an ordinary message can never
    accidentally attempt a binding."""
    match = _BIND_RE.match(str(text or ""))
    return match.group(1) if match else None

# Authority ceiling vocabulary — mirrors tools.PermissionTier, kept as strings so
# this module stays dependency-light. A channel identity never autonomously
# exceeds its ceiling.
_TIER_ORDER = {"L0_READ": 0, "L1_MEMORY_WRITE": 1, "L2_FILE_WRITE": 2, "L3_EXTERNAL_ACTION": 3}
DEFAULT_MAX_TIER = "L0_READ"
ENROLLMENT_TTL_SECONDS = 600  # 10 minutes to echo the code back through the channel


class ChannelAuthError(ValueError):
    """A channel enrollment/binding operation was refused."""


@dataclass(frozen=True)
class ChannelIdentity:
    """The result of authenticating an inbound (channel, external_id)."""

    authenticated: bool
    channel: str
    external_id: str
    binding_id: int | None = None
    max_tier: str | None = None  # None when not authenticated (untrusted input)
    label: str = ""

    @property
    def is_founder(self) -> bool:
        return self.authenticated


def _hash_code(code: str) -> str:
    return hashlib.sha256(str(code).strip().encode("utf-8")).hexdigest()


def _valid_tier(tier: str) -> str:
    t = str(tier or "").strip().upper()
    if t not in _TIER_ORDER:
        raise ChannelAuthError(f"Invalid max_tier {tier!r}; must be one of {sorted(_TIER_ORDER)}.")
    return t


def _clean(value: str) -> str:
    return str(value or "").strip()


class ChannelAuth:
    def __init__(self, db: KernelDatabase, *, default_max_tier: str = DEFAULT_MAX_TIER):
        self.db = db
        self.default_max_tier = _valid_tier(default_max_tier)

    # -- enrollment: founder-initiated from the trusted local side --------
    def begin_enrollment(self, channel: str, *, label: str = "", ttl_seconds: int = ENROLLMENT_TTL_SECONDS) -> dict[str, Any]:
        """Start a binding and return a ONE-TIME code the founder echoes through
        the target channel. Only the code's hash is stored. The endpoint that
        calls this is mutation-token gated (trusted-local)."""
        channel = _clean(channel)
        if not channel:
            raise ChannelAuthError("A channel is required to enroll.")
        code = secrets.token_hex(4)  # 8 hex chars — human-typeable into a chat
        expires_at = (datetime.now(UTC) + timedelta(seconds=max(60, ttl_seconds))).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO channel_enrollments (created_at, channel, label, code_hash, status, expires_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (utc_now(), channel, _clean(label), _hash_code(code), expires_at),
            )
            enrollment_id = int(cur.lastrowid or 0)
        self.db.audit(
            actor="founder", action="channel.enroll.begin", target=channel,
            permission_tier="L1_MEMORY_WRITE", status="pending",
            details={"enrollment_id": enrollment_id, "label": _clean(label)},  # never the code
        )
        return {
            "enrollment_id": enrollment_id,
            "channel": channel,
            "code": code,
            "expires_at": expires_at,
            "instructions": f"From your {channel} account, send this code to the bot to bind it: {code}",
        }

    def confirm_enrollment(self, channel: str, external_id: str, code: str) -> ChannelIdentity:
        """Complete a binding — the channel adapter calls this when an inbound
        message from external_id carries the code. Fail-closed on any mismatch or
        expiry. Proves control of BOTH the local kernel (who saw the code) and the
        channel account (who echoed it)."""
        channel, external_id = _clean(channel), _clean(external_id)
        if not external_id:
            raise ChannelAuthError("An external_id is required to confirm a binding.")
        now = utc_now()
        # Do all writes inside one connection, then audit/raise AFTER it commits —
        # calling self.db.audit (a second connection) while this one holds an
        # uncommitted write would deadlock on SQLite's single-writer lock.
        denied: str | None = None
        binding: dict[str, Any] | None = None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_enrollments WHERE channel = ? AND code_hash = ? AND status = 'pending' "
                "ORDER BY id DESC LIMIT 1",
                (channel, _hash_code(code)),
            ).fetchone()
            if row is None:
                denied = "no_matching_enrollment"
            elif row["expires_at"] < now:
                conn.execute("UPDATE channel_enrollments SET status='expired' WHERE id=?", (row["id"],))
                denied = "enrollment_expired"
            else:
                conn.execute("UPDATE channel_enrollments SET status='consumed', consumed_at=? WHERE id=?", (now, row["id"]))
                conn.execute(
                    "INSERT INTO channel_bindings (created_at, channel, external_id, label, status, max_tier) "
                    "VALUES (?, ?, ?, ?, 'active', ?) "
                    "ON CONFLICT(channel, external_id) DO UPDATE SET status='active', revoked_at=NULL, label=excluded.label",
                    (now, channel, external_id, row["label"], self.default_max_tier),
                )
                binding = dict(
                    conn.execute("SELECT * FROM channel_bindings WHERE channel=? AND external_id=?", (channel, external_id)).fetchone()
                )
        if denied:
            self._audit_denied(channel, external_id, denied)
            raise ChannelAuthError(
                "No matching pending enrollment for that code."
                if denied == "no_matching_enrollment"
                else "Enrollment code has expired."
            )
        assert binding is not None
        self.db.audit(
            actor="founder", action="channel.bind", target=f"{channel}:{external_id}",
            permission_tier="L1_MEMORY_WRITE", status="ok",
            details={"binding_id": binding["id"], "max_tier": binding["max_tier"]},
        )
        return ChannelIdentity(
            authenticated=True, channel=channel, external_id=external_id,
            binding_id=binding["id"], max_tier=binding["max_tier"], label=binding["label"],
        )

    # -- authentication: adapter, per inbound message ---------------------
    def authenticate(self, channel: str, external_id: str) -> ChannelIdentity:
        """Is this (channel, external_id) an active founder binding? Fail-closed:
        no active binding -> authenticated=False, no authority. The handle is never
        consulted — only the bound external_id."""
        channel, external_id = _clean(channel), _clean(external_id)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_bindings WHERE channel=? AND external_id=? AND status='active'",
                (channel, external_id),
            ).fetchone()
        if row is None:
            return ChannelIdentity(authenticated=False, channel=channel, external_id=external_id)
        return self._identity(row)

    def caps(self, identity: ChannelIdentity, requested_tier: str) -> bool:
        """Would this identity be permitted to act AUTONOMOUSLY at requested_tier?
        False if unauthenticated or above its ceiling. (L2+/L3 route to local
        approval regardless — this only governs autonomous action.)"""
        if not identity.authenticated or identity.max_tier is None:
            return False
        return _TIER_ORDER.get(_valid_tier(requested_tier), 99) <= _TIER_ORDER[identity.max_tier]

    # -- management: founder ----------------------------------------------
    def list_bindings(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM channel_bindings ORDER BY id DESC").fetchall()
        return [_binding_dict(r) for r in rows if include_revoked or r["status"] == "active"]

    def revoke(self, binding_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM channel_bindings WHERE id=?", (binding_id,)).fetchone()
            if row is None:
                raise ChannelAuthError(f"Channel binding not found: {binding_id}")
            conn.execute("UPDATE channel_bindings SET status='revoked', revoked_at=? WHERE id=?", (utc_now(), binding_id))
        self.db.audit(
            actor="founder", action="channel.revoke", target=f"{row['channel']}:{row['external_id']}",
            permission_tier="L1_MEMORY_WRITE", status="ok", details={"binding_id": binding_id},
        )
        return {"binding_id": binding_id, "status": "revoked"}

    def set_max_tier(self, binding_id: int, max_tier: str) -> dict[str, Any]:
        tier = _valid_tier(max_tier)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM channel_bindings WHERE id=?", (binding_id,)).fetchone()
            if row is None:
                raise ChannelAuthError(f"Channel binding not found: {binding_id}")
            conn.execute("UPDATE channel_bindings SET max_tier=? WHERE id=?", (tier, binding_id))
        self.db.audit(
            actor="founder", action="channel.set_tier", target=f"{row['channel']}:{row['external_id']}",
            permission_tier="L1_MEMORY_WRITE", status="ok", details={"binding_id": binding_id, "max_tier": tier},
        )
        return {"binding_id": binding_id, "max_tier": tier}

    # -- helpers ----------------------------------------------------------
    def _identity(self, row: Any) -> ChannelIdentity:
        return ChannelIdentity(
            authenticated=True, channel=row["channel"], external_id=row["external_id"],
            binding_id=row["id"], max_tier=row["max_tier"], label=row["label"],
        )

    def _audit_denied(self, channel: str, external_id: str, reason: str) -> None:
        self.db.audit(
            actor="channel", action="channel.confirm.denied", target=f"{channel}:{external_id}",
            permission_tier="L1_MEMORY_WRITE", status="denied", details={"reason": reason},
        )


def _binding_dict(r: Any) -> dict[str, Any]:
    return {
        "binding_id": r["id"], "channel": r["channel"], "external_id": r["external_id"],
        "label": r["label"], "status": r["status"], "max_tier": r["max_tier"],
        "created_at": r["created_at"], "revoked_at": r["revoked_at"],
    }
