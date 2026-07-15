from __future__ import annotations

import email
import email.message
import email.utils
import hashlib
import imaplib
import json
import os
import re
import urllib.error
from email.header import decode_header
from pathlib import Path
from typing import Any

from . import netguard
from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .founder import FounderService
from .ingestion import IngestionService

from .autonomy import WorkQueueService


CONNECTOR_TYPES = {"imap", "ics"}
# Substrings that mark a config key as secret-bearing. Detection is by substring
# (so app_password, client_secret_value, imap_pwd are all caught), with an
# exemption for the sanctioned "*_env" pattern, which names an environment
# variable to read the secret from rather than the secret itself.
SECRET_KEY_FRAGMENTS = ("pass", "pwd", "secret", "token", "credential", "apikey", "api_key", "private_key", "access_key")
SYNC_ACTION = "external.connector.sync"
EXCERPT_CHARS = 1200
BODY_FETCH_CHARS = 2000
DEFAULT_FETCH_LIMIT = 25


class ConnectorService:
    """Read-only external connectors feeding the graded evidence ledger.

    Connectors are situational awareness only: IMAP mailboxes are opened
    read-only and ICS calendars are parsed from exports or feeds. Nothing is
    ever sent or mutated. Sync executes exclusively through the approved
    dispatch flow (work item -> founder approval with the typed phrase), and
    synced items land as staged candidates the founder imports as evidence —
    external claims never become native certainty. Credentials live in
    environment variables referenced by name; they are never stored or logged.
    """

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: FounderService,
        ingestion: IngestionService,
        work_queue: WorkQueueService,
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.ingestion = ingestion
        self.work_queue = work_queue

    def create_connector(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload["name"]).strip()
        connector_type = str(payload.get("connector_type", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", name):
            raise ValueError("Connector name must be 2-80 chars of lowercase letters, digits, dot, dash, underscore.")
        if connector_type not in CONNECTOR_TYPES:
            raise ValueError(f"Connector type must be one of: {', '.join(sorted(CONNECTOR_TYPES))}")
        config = dict(payload.get("config", {}))
        leaked = sorted(
            key
            for key in config
            if not key.strip().lower().endswith("_env")
            and any(fragment in key.strip().lower() for fragment in SECRET_KEY_FRAGMENTS)
        )
        if leaked:
            raise ValueError(
                f"Connector config must not contain secrets ({', '.join(leaked)}). "
                "Store the credential in an environment variable and reference it via password_env."
            )
        if connector_type == "imap":
            for required in ("host", "username", "password_env"):
                if not str(config.get(required, "")).strip():
                    raise ValueError(f"IMAP connector config requires '{required}'.")
        if connector_type == "ics":
            if not str(config.get("url", "")).strip() and not str(config.get("path", "")).strip():
                raise ValueError("ICS connector config requires 'url' or 'path'.")
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM connectors WHERE name = ?", (name,)).fetchone()
            if existing:
                raise ValueError(f"Connector already exists: {name}")
            conn.execute(
                """
                INSERT INTO connectors (
                  created_at, updated_at, name, connector_type, description, config_json,
                  enabled, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    name,
                    connector_type,
                    str(payload.get("description", "")),
                    json.dumps(config, sort_keys=True),
                    int(bool(payload.get("enabled", True))),
                    json.dumps(payload.get("metadata", {}), sort_keys=True),
                ),
            )
        self.db.audit(
            actor="connectors",
            action="connectors.create",
            target=name,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"connector_type": connector_type, "config_keys": sorted(config.keys())},
        )
        return self.get_connector(name)

    def get_connector(self, name: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM connectors WHERE name = ?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Connector not found: {name}")
        return _connector_from_row(row)

    def list_connectors(self, *, enabled: bool | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if enabled is None:
                rows = conn.execute("SELECT * FROM connectors ORDER BY name ASC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM connectors WHERE enabled = ? ORDER BY name ASC LIMIT ?",
                    (int(enabled), limit),
                ).fetchall()
        return [_connector_from_row(row) for row in rows]

    def queue_sync(self, name: str) -> dict[str, Any]:
        """Queue an approval-gated sync. The only execution path is approved dispatch."""
        connector = self.get_connector(name)
        if not connector["enabled"]:
            raise ValueError(f"Connector is disabled: {name}")
        result = self.work_queue.enqueue(
            kind="connector_sync",
            title=f"Sync external connector: {name}",
            detail=(
                f"Read-only sync of {connector['connector_type']} connector '{name}' into staged "
                "candidate items. Nothing is sent or mutated externally."
            ),
            action=SYNC_ACTION,
            target=name,
            permission_tier="L3_EXTERNAL_ACTION",
            priority=65,
            source="connectors",
            metadata={"connector": name, "connector_type": connector["connector_type"]},
            unique_key=f"{SYNC_ACTION}:{name}:{utc_now()[:10]}",
        )
        return result.as_dict()

    def sync_from_work_item(self, item: Any) -> dict[str, Any]:
        name = str(item.target or (item.metadata or {}).get("connector", "")).strip()
        if not name:
            raise ValueError("Connector sync work item has no connector target.")
        return {"handler": SYNC_ACTION, "status": "ok", **self.sync(name)}

    def sync(self, name: str) -> dict[str, Any]:
        """Fetch read-only items and stage them as candidates. Never called autonomously."""
        connector = self.get_connector(name)
        if not connector["enabled"]:
            raise ValueError(f"Connector is disabled: {name}")
        try:
            if connector["connector_type"] == "imap":
                fetched = fetch_imap_items(connector["config"], self._resolve_password(connector))
            else:
                fetched = fetch_ics_events(connector["config"], allowed_roots=self._allowed_roots())
        except ValueError:
            self._record_sync(connector["id"], status="error")
            raise
        except Exception as exc:
            self._record_sync(connector["id"], status="error")
            raise ValueError(f"Connector sync failed for '{name}': {exc}") from exc
        created = 0
        updated = 0
        unchanged = 0
        for item in fetched:
            outcome = self._upsert_item(connector["id"], item)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1
        self._record_sync(connector["id"], status="ok")
        self.db.audit(
            actor="connectors",
            action="connectors.sync",
            target=name,
            permission_tier="L3_EXTERNAL_ACTION",
            status="ok",
            details={
                "connector_type": connector["connector_type"],
                "fetched": len(fetched),
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "read_only": True,
            },
        )
        return {
            "connector": name,
            "connector_type": connector["connector_type"],
            "fetched": len(fetched),
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "read_only": True,
        }

    def list_items(
        self,
        *,
        status: str | None = None,
        connector: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("i.status = ?")
            params.append(status)
        if connector:
            clauses.append("c.name = ?")
            params.append(connector)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT i.*, c.name AS connector_name, c.connector_type
                FROM connector_items i
                JOIN connectors c ON c.id = i.connector_id
                {where}
                ORDER BY i.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def import_items(
        self,
        *,
        item_ids: list[int],
        create_evidence: bool = True,
        ingest_documents: bool = True,
        reliability: str = "C",
        strength: int = 60,
        linked_assumption_id: int | None = None,
        linked_decision_id: int | None = None,
    ) -> dict[str, Any]:
        imported = []
        skipped = []
        for item_id in item_ids:
            item = self._get_item(item_id)
            if item["status"] != "candidate":
                skipped.append({"item_id": item_id, "reason": f"status is {item['status']}"})
                continue
            document_id = None
            if ingest_documents:
                result = self.ingestion.ingest_text(
                    title=item["title"] or f"Connector item {item_id}",
                    text=_item_text(item),
                    source=f"connector:{item['connector_name']}:{item['external_id']}",
                    metadata={
                        "connector": item["connector_name"],
                        "connector_type": item["connector_type"],
                        "item_type": item["item_type"],
                        "entity_boundary": "External source imported into Zade as evidence, not native certainty.",
                    },
                )
                document_id = result.document_id
            evidence_id = None
            if create_evidence:
                evidence = self.founder.create_evidence(
                    {
                        "evidence_type": f"connector_{item['item_type']}",
                        "source": f"connector:{item['connector_name']}:{item['external_id']}",
                        "evidence_date": (item["occurred_at"] or "")[:10] or None,
                        "reliability": reliability,
                        "claim_supported": f"External {item['item_type']} '{item['title']}' from {item['sender'] or 'unknown sender'}: {item['excerpt'][:400]}",
                        "strength": strength,
                        "linked_assumption_id": linked_assumption_id,
                        "linked_decision_id": linked_decision_id,
                        "notes": "Imported from a read-only external connector. Treat as sourced external claim.",
                        "metadata": {
                            "connector": item["connector_name"],
                            "connector_item_id": item_id,
                            "document_id": document_id,
                            "entity_boundary": "External source says; Zade records as evidence.",
                        },
                    }
                )
                evidence_id = evidence.id
            self._mark_item(item_id, status="imported", evidence_id=evidence_id, document_id=document_id)
            imported.append({"item_id": item_id, "evidence_id": evidence_id, "document_id": document_id})
        self.db.audit(
            actor="connectors",
            action="connectors.items.import",
            target="connector_items",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"imported_count": len(imported), "skipped_count": len(skipped)},
        )
        return {"imported": imported, "skipped": skipped, "count": len(imported)}

    def dismiss_item(self, item_id: int, *, reason: str = "") -> dict[str, Any]:
        item = self._get_item(item_id)
        if item["status"] != "candidate":
            raise ValueError(f"Only candidate items can be dismissed; item is {item['status']}.")
        self._mark_item(item_id, status="dismissed", note=reason)
        self.db.audit(
            actor="connectors",
            action="connectors.items.dismiss",
            target=f"connector_item:{item_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"reason": reason},
        )
        return self._get_item(item_id)

    def _resolve_password(self, connector: dict[str, Any]) -> str:
        env_name = str(connector["config"].get("password_env", "")).strip()
        if not env_name:
            raise ValueError("IMAP connector config requires 'password_env'.")
        password = os.environ.get(env_name, "")
        if not password:
            raise ValueError(f"Credential environment variable is not set: {env_name}")
        return password

    def _allowed_roots(self) -> list[Path]:
        return [
            self.config.paths.hot_root.resolve(),
            self.config.paths.cold_root.resolve(),
            self.config.paths.data_dir.resolve(),
        ]

    def _upsert_item(self, connector_id: int, item: dict[str, Any]) -> str:
        now = utc_now()
        content_hash = hashlib.sha256(
            f"{item['title']}\n{item['sender']}\n{item['excerpt']}".encode("utf-8")
        ).hexdigest()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id, content_hash, status FROM connector_items WHERE connector_id = ? AND external_id = ?",
                (connector_id, item["external_id"]),
            ).fetchone()
            if existing:
                if str(existing["content_hash"]) == content_hash:
                    return "unchanged"
                conn.execute(
                    """
                    UPDATE connector_items
                    SET updated_at = ?, title = ?, sender = ?, occurred_at = ?, excerpt = ?, content_hash = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        item["title"],
                        item["sender"],
                        item["occurred_at"],
                        item["excerpt"],
                        content_hash,
                        int(existing["id"]),
                    ),
                )
                return "updated"
            conn.execute(
                """
                INSERT INTO connector_items (
                  created_at, updated_at, connector_id, external_id, item_type, title,
                  sender, occurred_at, excerpt, content_hash, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                """,
                (
                    now,
                    now,
                    connector_id,
                    item["external_id"],
                    item["item_type"],
                    item["title"],
                    item["sender"],
                    item["occurred_at"],
                    item["excerpt"],
                    content_hash,
                    json.dumps(item.get("metadata", {}), sort_keys=True),
                ),
            )
            return "created"

    def _get_item(self, item_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT i.*, c.name AS connector_name, c.connector_type
                FROM connector_items i
                JOIN connectors c ON c.id = i.connector_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Connector item not found: {item_id}")
        return _item_from_row(row)

    def _mark_item(
        self,
        item_id: int,
        *,
        status: str,
        evidence_id: int | None = None,
        document_id: int | None = None,
        note: str = "",
    ) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT metadata_json FROM connector_items WHERE id = ?", (item_id,)).fetchone()
            metadata = json.loads(row["metadata_json"] or "{}") if row else {}
            metadata.update(
                {
                    key: value
                    for key, value in {
                        "evidence_id": evidence_id,
                        "document_id": document_id,
                        "resolution_note": note,
                        "resolved_at": utc_now(),
                    }.items()
                    if value
                }
            )
            conn.execute(
                "UPDATE connector_items SET updated_at = ?, status = ?, metadata_json = ? WHERE id = ?",
                (utc_now(), status, json.dumps(metadata, sort_keys=True), item_id),
            )

    def _record_sync(self, connector_id: int, *, status: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE connectors SET updated_at = ?, last_sync_at = ?, last_sync_status = ? WHERE id = ?",
                (utc_now(), utc_now(), status, connector_id),
            )


def fetch_imap_items(config: dict[str, Any], password: str, *, limit: int = DEFAULT_FETCH_LIMIT) -> list[dict[str, Any]]:
    """Fetch recent messages read-only: readonly select + BODY.PEEK, no flag changes."""
    host = str(config["host"])
    port = int(config.get("port", 993))
    username = str(config["username"])
    mailbox = str(config.get("mailbox", "INBOX"))
    limit = max(1, min(int(config.get("fetch_limit", limit)), 100))
    connection = imaplib.IMAP4_SSL(host, port)
    try:
        connection.login(username, password)
        connection.select(mailbox, readonly=True)
        _status, data = connection.search(None, "ALL")
        message_numbers = data[0].split() if data and data[0] else []
        items = []
        for number in reversed(message_numbers[-limit:]):
            _status, message_data = connection.fetch(number, "(BODY.PEEK[])")
            raw = _first_bytes_payload(message_data)
            if raw is None:
                continue
            message = email.message_from_bytes(raw)
            subject = _decode_mime_header(message.get("Subject", "")) or "(no subject)"
            sender = _decode_mime_header(message.get("From", ""))
            external_id = str(message.get("Message-ID") or f"{mailbox}:{number.decode()}").strip()
            occurred_at = _parse_email_date(message.get("Date", ""))
            body = _plain_text_body(message)
            items.append(
                {
                    "external_id": external_id,
                    "item_type": "email",
                    "title": subject[:240],
                    "sender": sender[:240],
                    "occurred_at": occurred_at,
                    "excerpt": body[:EXCERPT_CHARS],
                    "metadata": {"mailbox": mailbox},
                }
            )
        return items
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def fetch_ics_events(config: dict[str, Any], *, allowed_roots: list[Path]) -> list[dict[str, Any]]:
    """Read a calendar export or feed and parse VEVENT blocks. Read-only by nature."""
    path = str(config.get("path", "")).strip()
    url = str(config.get("url", "")).strip()
    if path:
        resolved = Path(path).expanduser().resolve()
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise ValueError(f"ICS path is outside allowed local roots: {resolved}")
        if not resolved.is_file():
            raise ValueError(f"ICS file not found: {resolved}")
        text = resolved.read_text(encoding="utf-8", errors="replace")
    elif url:
        text = _fetch_ics_over_http(url)
    else:
        raise ValueError("ICS connector config requires 'url' or 'path'.")
    return [_event_to_item(event) for event in parse_ics_events(text)]


# Egress policy for calendar/ICS feeds lives in the central netguard module so a
# single SSRF review covers every outbound call. These thin aliases keep the
# historical names importable.
_host_is_private = netguard.is_private_host
_NO_REDIRECT_OPENER = netguard.NO_REDIRECT_OPENER


def _fetch_ics_over_http(url: str) -> str:
    """Fetch an ICS feed with SSRF guards: https-only (loopback http allowed for
    testing), no private/internal targets, and redirects refused so a public URL
    cannot hop to an internal service."""
    try:
        netguard.assert_allowed(url, allow_loopback_http=True, require_https=True)
    except netguard.EgressError as exc:
        raise ValueError(str(exc)) from exc
    try:
        with _NO_REDIRECT_OPENER.open(url, timeout=20) as response:  # noqa: S310 - scheme+host validated above
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("ICS url returned a redirect; redirects are refused to prevent internal-host hops.") from exc
        raise ValueError(f"ICS fetch failed (HTTP {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"ICS fetch failed: {exc.reason}") from exc


def parse_ics_events(text: str, *, limit: int = 100) -> list[dict[str, str]]:
    unfolded: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue  # blank lines are invalid inside VCALENDAR; skipping keeps folds intact
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in unfolded:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
        elif stripped == "END:VEVENT":
            if current is not None:
                events.append(current)
                current = None
            if len(events) >= limit:
                break
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";")[0].strip().upper()] = value.strip()
    return events


def _event_to_item(event: dict[str, str]) -> dict[str, Any]:
    summary = event.get("SUMMARY", "(no title)")
    description = event.get("DESCRIPTION", "").replace("\\n", "\n").replace("\\,", ",")
    location = event.get("LOCATION", "").replace("\\,", ",")
    organizer = event.get("ORGANIZER", "").removeprefix("mailto:").removeprefix("MAILTO:")
    start = _normalize_ics_datetime(event.get("DTSTART", ""))
    end = _normalize_ics_datetime(event.get("DTEND", ""))
    excerpt_parts = [part for part in [f"When: {start}" + (f" -> {end}" if end else ""), f"Where: {location}" if location else "", description] if part]
    external_id = event.get("UID") or hashlib.sha256(f"{summary}{start}".encode("utf-8")).hexdigest()
    return {
        "external_id": external_id,
        "item_type": "calendar_event",
        "title": summary[:240],
        "sender": organizer[:240],
        "occurred_at": start or None,
        "excerpt": "\n".join(excerpt_parts)[:EXCERPT_CHARS],
        "metadata": {"dtend": end, "location": location},
    }


def _normalize_ics_datetime(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z?))?", value)
    if not match:
        return value
    year, month, day, hour, minute, second, zulu = match.groups()
    if hour is None:
        return f"{year}-{month}-{day}"
    suffix = "+00:00" if zulu else ""
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}{suffix}"


def _decode_mime_header(value: str) -> str:
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _parse_email_date(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value


def _plain_text_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")[:BODY_FETCH_CHARS]
        return ""
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = message.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")[:BODY_FETCH_CHARS]
    return str(payload or "")[:BODY_FETCH_CHARS]


def _first_bytes_payload(message_data: Any) -> bytes | None:
    for entry in message_data or []:
        if isinstance(entry, tuple) and len(entry) > 1 and isinstance(entry[1], bytes):
            return entry[1]
    return None


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        f"External {item['item_type']} from connector '{item['connector_name']}'.",
        f"Title: {item['title']}",
        f"From: {item['sender']}" if item["sender"] else "",
        f"Occurred: {item['occurred_at']}" if item["occurred_at"] else "",
        item["excerpt"],
    ]
    return "\n\n".join(part for part in parts if str(part).strip())


def _connector_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["config"] = json.loads(data.pop("config_json") or "{}")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    data["enabled"] = bool(data["enabled"])
    return data


def _item_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
