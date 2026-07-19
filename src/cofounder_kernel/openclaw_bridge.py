"""OpenClaw channel-gateway bridge — the external transport for channel ingress.

This is the last integration prerequisite from the cloud-integration arc: the
kernel side of channel messaging was complete (`POST /channels/message`,
authenticated, capped, HMAC-hardened); only the transport to a real channel
gateway was missing. OpenClaw's gateway speaks a WebSocket protocol (protocol 4),
NOT a webhook, so this module is a small dependency-free WebSocket client
(RFC 6455 over a raw socket — the kernel is deliberately dep-light and does not
pull in `websockets`).

Trust posture
-------------
- Zade connects as an **operator** client (observe + reply), never a registered
  agent. It does not run the channel's model; it governs and answers.
- The gateway is a LOCAL process. `assert_local_gateway` refuses a non-loopback
  ``ws_url`` unless the founder explicitly set ``allow_remote_gateway`` — a
  channel bridge to a remote host would route the founder's messages off-machine.
- Inbound messages are NOT trusted here. Every one is POSTed to the SAME governed
  ``/channels/message`` endpoint an in-process adapter would use, so channel auth,
  the capped authority ceiling, and (if configured) per-binding HMAC all apply
  unchanged. This module is pure transport: it never authenticates a sender, never
  decides authority, never touches the DB. An unbound sender gets the standard
  "not authorized" reply, exactly as through any other channel adapter.
- The gateway token authenticates Zade TO the gateway. It is read from the
  environment (``token_env``) and never stored or logged.

Protocol (OpenClaw 2026.6.8, gateway PROTOCOL_VERSION 4), verified against the
installed dist:
  server → `{type:"event", event:"connect.challenge", payload:{nonce, ts}}`
  client → `{type:"req", id, method:"connect", params:{minProtocol, maxProtocol,
             client:{id, version, platform, mode}, role:"operator",
             scopes:[...], auth:{token}}}`
  server → `{type:"res", id, ok:true, payload:{type:"hello-ok", protocol, ...}}`
  inbound message → `{type:"event", event:"session.message",
             payload:{sessionKey, message:{role, content, timestamp}, messageId}}`
  reply → `{type:"req", id, method:"chat.send",
             params:{sessionKey, message, idempotencyKey, deliver:true, ...}}`
A token-only operator on loopback does NOT need a signed `device` block
(`role==="operator" && sharedAuthOk` skips device identity), so none is sent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .config import OpenClawConfig

PROTOCOL_VERSION = 4
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 accept-key magic

# WebSocket opcodes.
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class OpenClawBridgeError(RuntimeError):
    pass


def assert_local_gateway(ws_url: str, *, allow_remote: bool) -> tuple[str, int, str]:
    """Parse and vet the gateway URL. Returns (host, port, path). Refuses a
    non-loopback host unless the founder explicitly allowed a remote gateway —
    the default posture keeps channel traffic on-machine."""
    parsed = urllib.parse.urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"}:
        raise OpenClawBridgeError(f"OpenClaw ws_url must be ws:// or wss://, got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    if not host:
        raise OpenClawBridgeError("OpenClaw ws_url has no host")
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not is_loopback and not allow_remote:
        raise OpenClawBridgeError(
            f"Refusing non-loopback OpenClaw gateway {host!r}: set [openclaw] allow_remote_gateway "
            "to bridge a remote gateway (this routes founder messages off-machine)."
        )
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path


# ---- minimal RFC 6455 client (text frames only) ---------------------------
class _WebSocket:
    """A tiny synchronous WebSocket client — connect, send text, recv text.

    Enough of RFC 6455 to speak to a local gateway: client-masked text frames
    out, unmasked frames in, ping/pong handled, close handled. TLS (wss) is
    supported via ssl.wrap. Not a general-purpose implementation — no
    continuation-frame fragmentation on send, no permessage-deflate.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, host: str, port: int, path: str, *, secure: bool, timeout: float) -> "_WebSocket":
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            import ssl

            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(handshake.encode("ascii"))
        ws = cls(raw)
        response = ws._read_http_response()
        if " 101 " not in response.split("\r\n", 1)[0]:
            raise OpenClawBridgeError(f"Gateway did not accept WebSocket upgrade: {response.splitlines()[:1]}")
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if expected.lower() not in response.lower():
            raise OpenClawBridgeError("Gateway upgrade handshake accept-key mismatch")
        return ws

    def _read_http_response(self) -> str:
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OpenClawBridgeError("Gateway closed during upgrade handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        return head.decode("latin-1")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OpenClawBridgeError("Gateway closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x80 | _OP_TEXT])  # FIN + text
        length = len(payload)
        mask_bit = 0x80  # client frames MUST be masked
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_text(self, *, timeout: float | None = None) -> str | None:
        """Next text message, or None on a control-only wakeup (ping/pong/close
        handled internally; close raises). ``timeout`` bounds the socket read."""
        if timeout is not None:
            self._sock.settimeout(timeout)
        first = self._recv_exact(2)
        fin = first[0] & 0x80
        opcode = first[0] & 0x0F
        masked = first[1] & 0x80
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length) if length else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == _OP_CLOSE:
            raise OpenClawBridgeError("Gateway sent a close frame")
        if opcode == _OP_PING:
            self._send_frame(_OP_PONG, data)
            return None
        if opcode == _OP_PONG:
            return None
        if opcode == _OP_TEXT and fin:
            return data.decode("utf-8", errors="replace")
        # Fragmented/continuation or binary: not expected from this gateway.
        return None

    def close(self) -> None:
        try:
            self._send_frame(_OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---- inbound message extraction -------------------------------------------
@dataclass(frozen=True)
class InboundMessage:
    """A channel message projected off the gateway wire into what the governed
    ``/channels/message`` endpoint needs."""

    session_key: str
    external_id: str
    channel: str
    text: str


def _extract_text(message: Any) -> str:
    """Pull display text out of a projected chat message. ``content`` is either a
    string or a list of content blocks ({type:text, text:...})."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return ""


def parse_session_key(session_key: str) -> tuple[str, str]:
    """(channel, peer_id) from a per-channel-peer OpenClaw session key.

    Keys look like ``agent:<agentId>:<channel>:direct:<peerId>`` (and variants).
    The channel is the token before ``direct``/``group``; the peer id is the last
    token. Falls back to ('openclaw', <whole key>) when the shape is unfamiliar —
    the key is still a stable per-peer identifier, which is what binding needs."""
    parts = [p for p in str(session_key).split(":") if p]
    channel = "openclaw"
    peer = session_key
    for marker in ("direct", "group", "channel"):
        if marker in parts:
            idx = parts.index(marker)
            if idx >= 1:
                channel = parts[idx - 1]
            if idx + 1 < len(parts):
                peer = parts[idx + 1]
            break
    else:
        if parts:
            peer = parts[-1]
    return channel, peer


def inbound_from_event(payload: dict[str, Any], *, channel_prefix: str) -> InboundMessage | None:
    """Project a ``session.message`` event payload into an InboundMessage, or None
    if it carries no user text (e.g. an assistant echo or an empty projection).

    The projected transcript strips the raw channel envelope, so the sender's
    platform handle is not on the wire — the session key IS the durable per-peer
    identity, and it is what we bind. Only inbound *user* turns are routed;
    Zade's own replies come back as role=assistant and must not loop."""
    session_key = str(payload.get("sessionKey") or "")
    if not session_key:
        return None
    message = payload.get("message")
    if isinstance(message, dict) and str(message.get("role") or "user") != "user":
        return None
    text = _extract_text(message)
    if not text.strip():
        return None
    channel, peer = parse_session_key(session_key)
    # The peer id alone can collide across channels; namespace the external_id by
    # channel so a binding is unique per (channel, peer). The channel label
    # prefers the gateway's own channel token, falling back to the config prefix.
    channel_label = channel if channel and channel != "openclaw" else channel_prefix
    external_id = f"{channel}:{peer}"
    return InboundMessage(session_key=session_key, external_id=external_id, channel=channel_label, text=text)


class OpenClawBridge:
    """Background operator client: connect → observe channel messages → route each
    through the governed kernel endpoint → send the reply back via the gateway.

    Runs on its own thread with reconnect/backoff. Fully off by default; started
    only when ``[openclaw] enabled`` and a token are present.
    """

    def __init__(
        self,
        config: OpenClawConfig,
        *,
        route_message: Callable[[InboundMessage], dict[str, Any]],
        token: str | None = None,
    ):
        self.config = config
        self._token = token if token is not None else os.getenv(config.token_env, "")
        # route_message runs the SAME governed channel-message logic the HTTP
        # endpoint runs, in-process (so it bypasses the mutation-token gate — which
        # exists to keep *external* callers out, not the kernel's own bridge — while
        # still applying channel auth + capped authority + HMAC). api.py injects it;
        # tests inject a fake to assert routing without a live gateway.
        self._route = route_message
        self._ws: _WebSocket | None = None
        self._req_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if not self.config.enabled:
            return
        if not self._token:
            raise OpenClawBridgeError(
                f"OpenClaw enabled but no token in ${self.config.token_env}; refusing to connect unauthenticated."
            )
        assert_local_gateway(self.config.ws_url, allow_remote=self.config.allow_remote_gateway)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="openclaw-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run_forever(self) -> None:
        delay = self.config.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                self._session()
                delay = self.config.reconnect_min_seconds  # clean exit resets backoff
            except Exception:
                if self._stop.is_set():
                    break
                time.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)

    # -- one connected session -------------------------------------------
    def _session(self) -> None:
        host, port, path = assert_local_gateway(
            self.config.ws_url, allow_remote=self.config.allow_remote_gateway
        )
        secure = urllib.parse.urlparse(self.config.ws_url).scheme == "wss"
        ws = _WebSocket.connect(host, port, path, secure=secure, timeout=10.0)
        self._ws = ws
        try:
            self._handshake(ws)
            self._subscribe(ws)
            while not self._stop.is_set():
                try:
                    raw = ws.recv_text(timeout=30.0)
                except socket.timeout:
                    continue
                if raw is None:
                    continue
                self._on_frame(ws, raw)
        finally:
            ws.close()
            self._ws = None

    def _next_id(self) -> str:
        self._req_id += 1
        return f"zade-{self._req_id}"

    def _handshake(self, ws: _WebSocket) -> None:
        """Wait for connect.challenge, send the operator connect, require hello-ok.

        A token operator on loopback skips the signed device block. The challenge
        nonce is only needed for a signed device identity, so it is read but not
        used."""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            raw = ws.recv_text(timeout=10.0)
            if raw is None:
                continue
            frame = json.loads(raw)
            if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
                break
        else:
            raise OpenClawBridgeError("Did not receive connect.challenge from gateway")

        connect_id = self._next_id()
        ws.send_text(
            json.dumps(
                {
                    "type": "req",
                    "id": connect_id,
                    "method": "connect",
                    "params": {
                        "minProtocol": PROTOCOL_VERSION,
                        "maxProtocol": PROTOCOL_VERSION,
                        # client.id and client.mode are CLOSED enums the gateway
                        # validates (GATEWAY_CLIENT_IDS / _MODES). "gateway-client"
                        # + "backend" is the headless-operator fit, verified
                        # accepted against the live gateway; arbitrary values
                        # (e.g. "zade"/"service") are rejected INVALID_REQUEST.
                        "client": {
                            "id": "gateway-client",
                            "version": "0.1.0",
                            "platform": "cofounder-kernel",
                            "mode": "backend",
                            "displayName": "Zade",
                        },
                        "role": "operator",
                        "scopes": ["operator.read", "operator.write"],
                        "caps": [],
                        "auth": {"token": self._token},
                    },
                }
            )
        )
        # await the correlated hello-ok response
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            raw = ws.recv_text(timeout=10.0)
            if raw is None:
                continue
            frame = json.loads(raw)
            if frame.get("type") == "res" and frame.get("id") == connect_id:
                if not frame.get("ok"):
                    raise OpenClawBridgeError(f"Gateway rejected connect: {frame.get('error')}")
                return
        raise OpenClawBridgeError("No hello-ok response to connect")

    def _subscribe(self, ws: _WebSocket) -> None:
        """Subscribe to all session events after hello-ok.

        WITHOUT this the operator connects but hears nothing: the gateway
        delivers `session.message` only to `sessions.subscribe` subscribers (the
        broad, all-sessions subscription — empty params), so a bridge that skips
        it is deaf. Any `session.message` that races in before the subscribe
        response is still handled, so an inbound message on the boundary is not
        dropped."""
        sub_id = self._next_id()
        ws.send_text(json.dumps({"type": "req", "id": sub_id, "method": "sessions.subscribe", "params": {}}))
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                raw = ws.recv_text(timeout=10.0)
            except socket.timeout:
                continue
            if raw is None:
                continue
            frame = json.loads(raw)
            if frame.get("type") == "res" and frame.get("id") == sub_id:
                if not frame.get("ok"):
                    raise OpenClawBridgeError(f"Gateway rejected sessions.subscribe: {frame.get('error')}")
                return
            # a message may arrive between subscribe and its ack — don't drop it
            if frame.get("type") == "event":
                self._on_frame(ws, raw)
        raise OpenClawBridgeError("No response to sessions.subscribe")

    def _on_frame(self, ws: _WebSocket, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if frame.get("type") != "event" or frame.get("event") != "session.message":
            return
        payload = frame.get("payload") or {}
        inbound = inbound_from_event(payload, channel_prefix=self.config.channel_prefix)
        if inbound is None:
            return
        try:
            result = self._route(inbound)
        except Exception:
            # A routing failure for one message must not tear down the session;
            # the founder simply gets no reply to that message.
            return
        reply = str((result or {}).get("reply") or "").strip()
        if reply:
            self._send_reply(ws, inbound.session_key, reply, payload)

    def _send_reply(self, ws: _WebSocket, session_key: str, text: str, source_payload: dict[str, Any]) -> None:
        """chat.send with deliver:true so the reply routes back to the originating
        channel/peer. idempotencyKey is required and must be unique per send."""
        params: dict[str, Any] = {
            "sessionKey": session_key,
            "message": text,
            "idempotencyKey": secrets.token_hex(16),
            "deliver": True,
        }
        # Preserve the originating routing hints when the gateway supplied them.
        for wire_key, param_key in (
            ("channel", "originatingChannel"),
            ("originatingChannel", "originatingChannel"),
            ("originatingTo", "originatingTo"),
            ("originatingAccountId", "originatingAccountId"),
            ("originatingThreadId", "originatingThreadId"),
        ):
            value = source_payload.get(wire_key)
            if value:
                params[param_key] = value
        ws.send_text(json.dumps({"type": "req", "id": self._next_id(), "method": "chat.send", "params": params}))
