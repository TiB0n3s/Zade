"""OpenClaw channel-gateway bridge — transport, message projection, and the
loopback safety guard. The WebSocket wire itself is exercised against an
in-process fake gateway so no real OpenClaw process is needed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

import pytest

from cofounder_kernel.config import OpenClawConfig
from cofounder_kernel.openclaw_bridge import (
    OpenClawBridge,
    OpenClawBridgeError,
    assert_local_gateway,
    inbound_from_event,
    parse_session_key,
)


# ---- pure projection --------------------------------------------------------
def test_parse_session_key_extracts_channel_and_peer() -> None:
    assert parse_session_key("agent:zade:telegram:direct:12345") == ("telegram", "12345")
    assert parse_session_key("agent:zade:whatsapp:group:room7") == ("whatsapp", "room7")
    # unfamiliar shape still yields a stable per-peer id
    channel, peer = parse_session_key("weird-key")
    assert peer == "weird-key"


def test_inbound_from_event_projects_user_text_and_skips_assistant() -> None:
    user = inbound_from_event(
        {
            "sessionKey": "agent:zade:telegram:direct:999",
            "message": {"role": "user", "content": [{"type": "text", "text": "hello there"}]},
        },
        channel_prefix="openclaw",
    )
    assert user is not None
    assert user.text == "hello there"
    assert user.channel == "telegram"
    assert user.external_id == "telegram:999"

    # Zade's own reply comes back as role=assistant and must not re-route (loop)
    assert (
        inbound_from_event(
            {"sessionKey": "agent:zade:telegram:direct:999", "message": {"role": "assistant", "content": "hi"}},
            channel_prefix="openclaw",
        )
        is None
    )
    # empty projection is ignored
    assert (
        inbound_from_event(
            {"sessionKey": "agent:zade:telegram:direct:999", "message": {"role": "user", "content": ""}},
            channel_prefix="openclaw",
        )
        is None
    )


# ---- loopback safety guard --------------------------------------------------
def test_gateway_guard_refuses_remote_without_optin() -> None:
    assert assert_local_gateway("ws://127.0.0.1:18789", allow_remote=False) == ("127.0.0.1", 18789, "/")
    with pytest.raises(OpenClawBridgeError):
        assert_local_gateway("ws://example.com:18789", allow_remote=False)
    # explicit opt-in permits a remote gateway
    assert assert_local_gateway("ws://example.com:18789", allow_remote=True)[0] == "example.com"


def test_bridge_refuses_to_start_without_token() -> None:
    bridge = OpenClawBridge(
        OpenClawConfig(enabled=True, token_env="OPENCLAW_TEST_MISSING"), route_message=lambda m: {}, token=""
    )
    with pytest.raises(OpenClawBridgeError):
        bridge.start()


# ---- end-to-end over a fake gateway ----------------------------------------
class _FakeGateway:
    """A minimal RFC 6455 server that performs the OpenClaw connect handshake,
    pushes one inbound session.message, and captures the client's chat.send."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.connect_params: dict | None = None
        self.subscribe_method: str | None = None
        self.reply_params: dict | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _recv_frame(self, conn: socket.socket, buf: bytearray) -> str:
        def need(n: int) -> bytes:
            while len(buf) < n:
                buf.extend(conn.recv(4096))
            out = bytes(buf[:n])
            del buf[:n]
            return out

        first = need(2)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", need(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", need(8))[0]
        mask = need(4) if first[1] & 0x80 else b""
        data = need(length)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return data.decode("utf-8")

    def _send_text(self, conn: socket.socket, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header += struct.pack("!H", len(payload))
        conn.sendall(bytes(header) + payload)

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        buf = bytearray()
        # HTTP upgrade handshake
        while b"\r\n\r\n" not in buf:
            buf.extend(conn.recv(4096))
        request = bytes(buf).decode("latin-1")
        del buf[:]
        key = ""
        for line in request.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        # push the connect.challenge, read connect, reply hello-ok
        self._send_text(conn, {"type": "event", "event": "connect.challenge", "payload": {"nonce": "n1", "ts": 1}})
        connect = json.loads(self._recv_frame(conn, buf))
        self.connect_params = connect["params"]
        self._send_text(conn, {"type": "res", "id": connect["id"], "ok": True, "payload": {"type": "hello-ok", "protocol": 4}})
        # the operator must subscribe before it can receive session events
        subscribe = json.loads(self._recv_frame(conn, buf))
        self.subscribe_method = subscribe.get("method")
        self._send_text(conn, {"type": "res", "id": subscribe["id"], "ok": True, "payload": {}})
        # deliver one inbound channel message
        self._send_text(
            conn,
            {
                "type": "event",
                "event": "session.message",
                "payload": {
                    "sessionKey": "agent:zade:telegram:direct:555",
                    "message": {"role": "user", "content": [{"type": "text", "text": "status?"}]},
                    "channel": "telegram",
                    "originatingTo": "555",
                },
            },
        )
        # capture the client's chat.send reply
        self.reply_params = json.loads(self._recv_frame(conn, buf))["params"]
        conn.close()


def test_bridge_handshake_routes_inbound_and_sends_reply() -> None:
    gateway = _FakeGateway()
    gateway.start()
    routed: list = []

    def route(inbound):
        routed.append(inbound)
        return {"status": "ok", "reply": "All systems local."}

    config = OpenClawConfig(enabled=True, ws_url=f"ws://127.0.0.1:{gateway.port}")
    bridge = OpenClawBridge(config, route_message=route, token="test-token")
    bridge.start()

    # wait for the round-trip to complete
    for _ in range(100):
        if gateway.reply_params is not None:
            break
        threading.Event().wait(0.05)
    bridge.stop()

    # connect frame carried the operator role + token
    assert gateway.connect_params is not None
    assert gateway.connect_params["role"] == "operator"
    assert gateway.connect_params["auth"]["token"] == "test-token"
    assert gateway.connect_params["minProtocol"] == 4

    # the operator subscribed to session events (else it would be deaf)
    assert gateway.subscribe_method == "sessions.subscribe"

    # the inbound message was projected and routed through the governed callable
    assert routed and routed[0].text == "status?"
    assert routed[0].external_id == "telegram:555"

    # the reply went back via chat.send with delivery routing
    assert gateway.reply_params is not None
    assert gateway.reply_params["message"] == "All systems local."
    assert gateway.reply_params["sessionKey"] == "agent:zade:telegram:direct:555"
    assert gateway.reply_params["deliver"] is True
    assert gateway.reply_params["idempotencyKey"]
    assert gateway.reply_params["originatingTo"] == "555"
