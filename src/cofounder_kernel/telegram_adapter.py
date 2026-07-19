"""Direct Telegram Bot API adapter — Zade's own channel transport.

This replaces the OpenClaw bridge for Telegram. OpenClaw turned out to be a full
assistant that fights to own the conversation (its own agent, model, and auth
layers); Zade only needs a dumb pipe, and the Telegram Bot API is one of the
simplest there is. So Zade talks to Telegram directly:

  inbound  : long-poll getUpdates -> project each message -> route through the
             SAME governed /channels/message flow (channel auth + capped
             authority + optional HMAC) -> reply
  outbound : sendMessage

Dependency-free (urllib), matching the kernel's dep-light, local-first design and
the anthropic_client / voice HTTP clients. No gateway, no second auth layer, no
competing agent — Zade is the transport AND the brain.

Governance
----------
- The bot token authenticates Zade to Telegram; read from ``token_env``, never
  stored in config or the DB.
- Every outbound HTTP call passes ``netguard`` (require_https + host allowlist).
- Zade's reply is REPLY_TEXT leaving to a cloud channel, so it passes the
  data-class egress gate: a ``reply_text:telegram`` standing grant is required.
  Without it the adapter still processes inbound (binds still happen) but sends
  NO reply — fail-closed, audited. The founder opts in by adding the grant.
- Replies go ONLY to bound founder peers and to bind confirmations. An unbound
  peer's ordinary message is answered with silence, not a reply — Zade does not
  egress text to a stranger who merely found the bot.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import netguard
from .config import KernelConfig, TelegramConfig
from .egress import DataClass, EgressPolicy, EgressRequest

# Statuses from the governed channel flow that warrant an outbound reply. A plain
# "unauthenticated" (non-bind message from an unbound peer) is answered with
# silence so Zade never egresses to a stranger who merely found the bot.
_REPLYING_STATUSES = frozenset({"bound", "ok", "bind_failed"})


class TelegramError(RuntimeError):
    pass


def token_from_env(name: str) -> str:
    """Read the bot token from the process env, falling back to the User-scope
    registry on Windows. A long-lived launcher (the universe shell) hands its
    children a snapshot of the environment from BEFORE the token was set, so a
    respawned kernel silently loses the token; the registry is the authoritative
    store for User env vars and never goes stale. Same secrecy posture: the
    value is still an env var, never config or DB."""
    import os
    import sys

    value = os.getenv(name, "")
    if value or sys.platform != "win32":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _ = winreg.QueryValueEx(key, name)
            return str(raw)
    except OSError:
        return ""


@dataclass(frozen=True)
class InboundTelegram:
    """A projected inbound Telegram message: what the governed flow needs, plus
    the chat id to reply into."""

    external_id: str   # the SENDER's user id — the stable per-peer binding identity
    chat_id: int       # where the reply is sent (== sender id for a 1:1 chat)
    text: str


@dataclass(frozen=True)
class TelegramDeliveryResult:
    """Aggregate result for one bus-controlled proactive Telegram delivery."""

    status: str
    recipient_count: int
    delivered_count: int
    failed_count: int
    detail: str


def _allowed_hosts(api_base: str) -> frozenset[str]:
    host = (urllib.parse.urlparse(api_base).hostname or "").lower()
    return frozenset({host}) if host else frozenset()


def project_update(update: dict[str, Any]) -> InboundTelegram | None:
    """A Telegram ``update`` -> InboundTelegram, or None to skip.

    Only text messages from a real user are routed: edits, channel posts,
    callbacks, joins, and bot/anonymous senders are ignored. ``from.id`` is the
    binding identity; ``chat.id`` is the reply target."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None  # edited_message / channel_post / callback_query / etc.
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None  # non-text (photo/sticker/...) — nothing to route
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if sender.get("is_bot"):
        return None  # never route another bot (or our own echoes)
    sender_id = sender.get("id")
    chat_id = chat.get("id")
    if sender_id is None or chat_id is None:
        return None
    return InboundTelegram(external_id=str(sender_id), chat_id=int(chat_id), text=text.strip())


class TelegramClient:
    """Minimal Telegram Bot API client over urllib. Netguard-checked, https-only."""

    def __init__(self, config: TelegramConfig, token: str):
        self.config = config
        self._token = token
        self._hosts = _allowed_hosts(config.api_base)

    def _call(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        url = f"{self.config.api_base}/bot{self._token}/{method}"
        netguard.assert_allowed(url, require_https=True, allowed_hosts=self._hosts)
        data = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - allowlisted https
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise TelegramError(f"Telegram {method} HTTP {exc.code}: {detail}") from exc
        if not body.get("ok"):
            raise TelegramError(f"Telegram {method} rejected: {body.get('description')}")
        return body.get("result")

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", {}, timeout=15.0)

    def get_updates(self, offset: int | None, *, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        # HTTP read timeout must outlast the long-poll's server-side hold.
        result = self._call("getUpdates", params, timeout=timeout + 15.0)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=30.0)


class TelegramAdapter:
    """Background long-poll loop: getUpdates -> governed flow -> gated reply.

    Off unless ``[telegram] enabled`` and a token are present. Runs on its own
    daemon thread with reconnect/backoff.
    """

    def __init__(
        self,
        config: KernelConfig,
        *,
        route_message: Callable[[InboundTelegram], dict[str, Any]],
        db: Any,
        token: str | None = None,
    ):
        self.config = config
        self.tg = config.telegram
        self._token = token if token is not None else token_from_env(self.tg.token_env)
        self._route = route_message
        self.db = db
        self._client = TelegramClient(self.tg, self._token) if self._token else None
        self._offset: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if not self.tg.enabled:
            return
        if not self._token:
            raise TelegramError(
                f"Telegram enabled but no token in ${self.tg.token_env}; refusing to connect."
            )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="telegram-adapter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._running

    def _run_forever(self) -> None:
        delay = self.tg.reconnect_min_seconds
        # Skip backlog: start from the latest update so a restart does not replay
        # (and re-answer, re-bind) stale messages. A successful getUpdates here
        # also confirms the token + connectivity, so report running immediately
        # rather than only after the first (up to poll_timeout-long) poll returns.
        try:
            self._offset = self._latest_offset()
            self._running = True
        except Exception:
            self._offset = None
        while not self._stop.is_set():
            try:
                self._poll_once()
                self._running = True
                delay = self.tg.reconnect_min_seconds
            except Exception:
                self._running = False
                if self._stop.is_set():
                    break
                time.sleep(delay)
                delay = min(delay * 2, self.tg.reconnect_max_seconds)
        self._running = False

    def _latest_offset(self) -> int | None:
        assert self._client is not None
        updates = self._client.get_updates(-1, timeout=0)
        if not updates:
            return None
        return int(updates[-1]["update_id"]) + 1

    def _poll_once(self) -> None:
        assert self._client is not None
        updates = self._client.get_updates(self._offset, timeout=self.tg.poll_timeout_seconds)
        for update in updates:
            self._offset = int(update["update_id"]) + 1  # confirm on next poll
            inbound = project_update(update)
            if inbound is None:
                continue
            self._handle(inbound)

    def _handle(self, inbound: InboundTelegram) -> None:
        try:
            result = self._route(inbound)
        except Exception:
            # One message's routing failure must not tear down the loop.
            return
        status = str((result or {}).get("status") or "")
        reply = str((result or {}).get("reply") or "").strip()
        if status not in _REPLYING_STATUSES or not reply:
            return
        if not self._egress_allows_reply():
            return
        assert self._client is not None
        try:
            self._client.send_message(inbound.chat_id, reply[: self.tg.max_reply_chars])
        except Exception:
            return

    def send_bound_founders(self, text: str) -> TelegramDeliveryResult:
        """Send one proactive message only to active Telegram founder bindings.

        Callers are expected to reach this through ``NotificationBus`` so
        severity, quiet hours, dedupe, and rate limits are applied first. This
        adapter still independently enforces the transport switch, the existing
        ``reply_text:telegram`` egress grant, Telegram's text bound, and the
        active founder-binding allowlist.
        """
        if not self.tg.enabled:
            self._audit_proactive(status="suppressed", detail="telegram_disabled")
            return TelegramDeliveryResult("suppressed", 0, 0, 0, "telegram_disabled")

        chat_ids = self._bound_founder_chat_ids()
        if not chat_ids:
            self._audit_proactive(status="suppressed", detail="no_bound_founder_chats")
            return TelegramDeliveryResult("suppressed", 0, 0, 0, "no_bound_founder_chats")

        if not self._egress_allows_reply(purpose="telegram.notification"):
            for chat_id in chat_ids:
                self._audit_proactive(
                    status="suppressed", detail="egress_denied", chat_id=chat_id
                )
            return TelegramDeliveryResult("suppressed", len(chat_ids), 0, 0, "egress_denied")

        if self._client is None:
            for chat_id in chat_ids:
                self._audit_proactive(
                    status="failed", detail="telegram_client_not_configured", chat_id=chat_id
                )
            return TelegramDeliveryResult(
                "failed", len(chat_ids), 0, len(chat_ids), "telegram_client_not_configured"
            )

        message = str(text or "").strip()[: self.tg.max_reply_chars]
        if not message:
            for chat_id in chat_ids:
                self._audit_proactive(status="suppressed", detail="empty_message", chat_id=chat_id)
            return TelegramDeliveryResult("suppressed", len(chat_ids), 0, 0, "empty_message")

        delivered = 0
        failed = 0
        for chat_id in chat_ids:
            try:
                self._client.send_message(chat_id, message)
            except Exception as exc:
                failed += 1
                self._audit_proactive(status="failed", detail=str(exc)[:200], chat_id=chat_id)
            else:
                delivered += 1
                self._audit_proactive(status="delivered", detail="sendMessage", chat_id=chat_id)

        status = "delivered" if delivered else "failed"
        detail = f"delivered={delivered} failed={failed} recipients={len(chat_ids)}"
        return TelegramDeliveryResult(status, len(chat_ids), delivered, failed, detail)

    def _bound_founder_chat_ids(self) -> list[int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT external_id FROM channel_bindings "
                "WHERE channel = 'telegram' AND status = 'active' ORDER BY id ASC"
            ).fetchall()
        chats: list[int] = []
        for row in rows:
            try:
                chats.append(int(row["external_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return chats

    def _audit_proactive(self, *, status: str, detail: str, chat_id: int | None = None) -> None:
        try:
            self.db.audit(
                actor="telegram",
                action="telegram.proactive.delivery",
                target=f"telegram:{chat_id}" if chat_id is not None else "telegram",
                permission_tier="L3_EXTERNAL_ACTION",
                status=status,
                details={"detail": detail},
            )
        except Exception:
            pass

    def _egress_allows_reply(self, *, purpose: str = "telegram.reply") -> bool:
        """Gate the outbound reply through the data-class egress matrix, exactly
        like the voice lane. REPLY_TEXT -> telegram (CHANNEL tier) is STANDING, so
        a ``reply_text:telegram`` grant authorizes it; otherwise fail-closed. Every
        decision is audited (redacted — never the reply text)."""
        decision = EgressPolicy.from_config(self.config).decide(
            EgressRequest(
                request_id=secrets.token_hex(8),
                data_class=DataClass.REPLY_TEXT,
                vendor="telegram",
                purpose=purpose,
            )
        )
        try:
            self.db.audit(
                actor="telegram",
                action="egress.decision",
                target="telegram",
                permission_tier="L3_EXTERNAL_ACTION",
                status="ok" if decision.allowed else "refused",
                details=decision.audit_record(),
            )
        except Exception:
            pass
        return decision.allowed
