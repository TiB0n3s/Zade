"""Microsoft OAuth2 device-code flow (msoauth) and the XOAUTH2 IMAP connector
path. All Microsoft endpoints are faked — no network, no real client_id — and
the token cache is exercised for real on tmp_path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import cofounder_kernel.msoauth as msoauth
from cofounder_kernel.msoauth import (
    OAuthError,
    TokenCache,
    access_token_from_cache,
    build_xoauth2,
)


def fake_post_form(responses: list[dict]):
    """A _post_form stub that pops canned responses and records calls."""
    calls: list[tuple[str, dict]] = []

    def _fake(url: str, fields: dict) -> dict:
        calls.append((url, dict(fields)))
        return responses.pop(0)

    return _fake, calls


# ---- SASL string ------------------------------------------------------------
def test_build_xoauth2_sasl_shape() -> None:
    raw = build_xoauth2("z@example.com", "tok123")
    assert raw == b"user=z@example.com\x01auth=Bearer tok123\x01\x01"


# ---- device flow ------------------------------------------------------------
def test_begin_device_flow_returns_codes_and_hits_consumers_tenant(monkeypatch) -> None:
    fake, calls = fake_post_form(
        [{"device_code": "dc", "user_code": "ABC-123", "verification_uri": "https://microsoft.com/devicelogin", "interval": 5, "expires_in": 900}]
    )
    monkeypatch.setattr(msoauth, "_post_form", fake)
    flow = msoauth.begin_device_flow("client-1")
    assert flow["user_code"] == "ABC-123"
    url, fields = calls[0]
    assert url == "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
    assert fields["client_id"] == "client-1"
    assert "IMAP.AccessAsUser.All" in fields["scope"] and "offline_access" in fields["scope"]


def test_begin_device_flow_failure_raises(monkeypatch) -> None:
    fake, _ = fake_post_form([{"error": "invalid_client", "error_description": "bad app"}])
    monkeypatch.setattr(msoauth, "_post_form", fake)
    with pytest.raises(OAuthError, match="invalid_client"):
        msoauth.begin_device_flow("client-1")


def test_poll_token_states(monkeypatch) -> None:
    fake, _ = fake_post_form(
        [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"error": "authorization_declined", "error_description": "user said no"},
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        ]
    )
    monkeypatch.setattr(msoauth, "_post_form", fake)
    assert msoauth.poll_token("c", "dc")[0] == "pending"
    assert msoauth.poll_token("c", "dc")[0] == "slow_down"
    state, payload = msoauth.poll_token("c", "dc")
    assert state == "failed" and payload["error"] == "authorization_declined"
    state, payload = msoauth.poll_token("c", "dc")
    assert state == "ok" and payload["access_token"] == "at"


# ---- token cache + refresh --------------------------------------------------
def test_cache_roundtrip_and_fresh_token_skips_refresh(tmp_path: Path, monkeypatch) -> None:
    cache = TokenCache(tmp_path / "oauth" / "c.json")
    cache.save({"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600})

    def boom(*a, **k):
        raise AssertionError("refresh must not run for a fresh token")

    monkeypatch.setattr(msoauth, "refresh_token_grant", boom)
    assert access_token_from_cache(cache, "client-1") == "at1"


def test_expired_token_refreshes_and_persists_rotated_refresh_token(tmp_path: Path, monkeypatch) -> None:
    cache = TokenCache(tmp_path / "c.json")
    cache.save({"access_token": "old", "refresh_token": "rt1", "expires_in": 0})
    fake, calls = fake_post_form([{"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}])
    monkeypatch.setattr(msoauth, "_post_form", fake)

    assert access_token_from_cache(cache, "client-1") == "new"
    _url, fields = calls[0]
    assert fields["grant_type"] == "refresh_token" and fields["refresh_token"] == "rt1"
    stored = json.loads((tmp_path / "c.json").read_text())
    assert stored["refresh_token"] == "rt2"  # rotation persisted
    assert stored["expires_at"] > time.time()


def test_refresh_keeps_old_refresh_token_when_ms_omits_it(tmp_path: Path, monkeypatch) -> None:
    cache = TokenCache(tmp_path / "c.json")
    cache.save({"access_token": "old", "refresh_token": "rt1", "expires_in": 0})
    fake, _ = fake_post_form([{"access_token": "new", "expires_in": 3600}])
    monkeypatch.setattr(msoauth, "_post_form", fake)
    access_token_from_cache(cache, "client-1")
    assert json.loads((tmp_path / "c.json").read_text())["refresh_token"] == "rt1"


def test_no_cache_and_failed_refresh_raise_actionable_errors(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(OAuthError, match="oauth/begin"):
        access_token_from_cache(TokenCache(tmp_path / "missing.json"), "client-1")

    cache = TokenCache(tmp_path / "c.json")
    cache.save({"access_token": "old", "refresh_token": "rt-revoked", "expires_in": 0})
    fake, _ = fake_post_form([{"error": "invalid_grant", "error_description": "revoked"}])
    monkeypatch.setattr(msoauth, "_post_form", fake)
    with pytest.raises(OAuthError, match="re-run OAuth enrollment"):
        access_token_from_cache(cache, "client-1")


# ---- endpoint guardrails ----------------------------------------------------
def test_post_form_only_talks_to_microsoft_login(monkeypatch) -> None:
    with pytest.raises(Exception):
        msoauth._post_form("https://evil.example.com/oauth2/v2.0/token", {})
