"""Microsoft OAuth2 device-code flow for outlook.com IMAP (XOAUTH2).

Outlook.com personal mailboxes reject every form of password IMAP login
(basic auth retired late 2024), so the IMAP connector needs modern auth. The
device-code flow fits Zade's shape exactly: the KERNEL never sees the founder's
Microsoft password — it shows a short code, the founder types it at
https://microsoft.com/devicelogin in their own browser, and Microsoft hands the
kernel scoped, revocable tokens for IMAP only.

Governance
----------
- Dep-free urllib, matching anthropic_client / telegram_adapter / voice.
- Every call is netguard-checked: https-only, host allowlisted to
  ``login.microsoftonline.com``. Nothing else is ever contacted.
- ``client_id`` is a PUBLIC identifier (an Azure "public client" app
  registration has no secret), so it may live in connector config.
- Tokens are credentials: they live in a JSON cache file under the kernel's
  data dir — never in config, never in the DB, never logged. The refresh token
  Microsoft returns on each redemption replaces the stored one (they rotate).
- Scope is IMAP read access plus ``offline_access`` (the refresh token); the
  token cannot send mail, delete mail, or touch anything but IMAP.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import netguard

AUTHORITY = "https://login.microsoftonline.com"
# Personal Microsoft accounts live in the "consumers" tenant.
DEFAULT_TENANT = "consumers"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
DEFAULT_SCOPES = f"{IMAP_SCOPE} offline_access"
_ALLOWED_HOSTS = frozenset({"login.microsoftonline.com"})
# Treat a token as expired this many seconds early so an in-flight IMAP login
# never races the real expiry.
EXPIRY_SLACK_SECONDS = 300


class OAuthError(ValueError):
    pass


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """POST form-encoded fields; return the JSON body even on HTTP 400 — the
    token endpoint reports flow states (authorization_pending, etc.) as 400s
    with an ``error`` field, which are protocol answers, not transport errors."""
    netguard.assert_allowed(url, require_https=True, allowed_hosts=_ALLOWED_HOSTS)
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - allowlisted https
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise OAuthError(f"Microsoft login endpoint returned HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Could not reach Microsoft login endpoint: {exc.reason}") from exc


def begin_device_flow(client_id: str, *, tenant: str = DEFAULT_TENANT, scopes: str = DEFAULT_SCOPES) -> dict[str, Any]:
    """Start the device-code flow. Returns user_code / verification_uri /
    device_code / interval / expires_in."""
    result = _post_form(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": scopes},
    )
    if "device_code" not in result:
        raise OAuthError(
            f"Device flow start failed: {result.get('error', 'unknown')} — {result.get('error_description', '')[:200]}"
        )
    return result


def poll_token(client_id: str, device_code: str, *, tenant: str = DEFAULT_TENANT) -> tuple[str, dict[str, Any]]:
    """One poll of the token endpoint. Returns (state, payload) where state is
    'ok' (payload = tokens), 'pending', 'slow_down', or 'failed' (payload has
    'error'/'error_description')."""
    result = _post_form(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    if "access_token" in result:
        return "ok", result
    error = str(result.get("error", ""))
    if error == "authorization_pending":
        return "pending", result
    if error == "slow_down":
        return "slow_down", result
    return "failed", result


def refresh_token_grant(client_id: str, refresh_token: str, *, tenant: str = DEFAULT_TENANT) -> dict[str, Any]:
    result = _post_form(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": DEFAULT_SCOPES,
        },
    )
    if "access_token" not in result:
        raise OAuthError(
            "Token refresh failed "
            f"({result.get('error', 'unknown')}): {result.get('error_description', '')[:200]} "
            "— re-run OAuth enrollment."
        )
    return result


def build_xoauth2(username: str, access_token: str) -> bytes:
    """The SASL XOAUTH2 initial response (imaplib base64-encodes it)."""
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


class TokenCache:
    """Credential file for one connector: tokens on disk, outside config and DB.

    The stored refresh token is replaced every time Microsoft rotates it. The
    file holds nothing but what Microsoft issued plus an absolute expiry.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, tokens: dict[str, Any]) -> dict[str, Any]:
        record = {
            "access_token": str(tokens.get("access_token", "")),
            "refresh_token": str(tokens.get("refresh_token", "")),
            "scope": str(tokens.get("scope", "")),
            "expires_at": time.time() + float(tokens.get("expires_in", 0) or 0),
            "saved_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record), encoding="utf-8")
        return record

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def access_token_from_cache(cache: TokenCache, client_id: str, *, tenant: str = DEFAULT_TENANT) -> str:
    """A currently-valid access token, refreshing (and persisting the rotated
    refresh token) when the cached one is stale."""
    record = cache.load()
    if not record or not record.get("refresh_token"):
        raise OAuthError(
            "No OAuth tokens are enrolled for this connector. "
            "Run POST /connectors/{name}/oauth/begin and finish the device login."
        )
    if record.get("access_token") and time.time() < float(record.get("expires_at", 0)) - EXPIRY_SLACK_SECONDS:
        return str(record["access_token"])
    tokens = refresh_token_grant(client_id, str(record["refresh_token"]), tenant=tenant)
    if not tokens.get("refresh_token"):
        tokens["refresh_token"] = record["refresh_token"]  # MS may omit when not rotating
    saved = cache.save(tokens)
    return str(saved["access_token"])
