"""Autonomous web research: local topic derivation + approval-gated web fetch.

The autonomy is deliberately split from the egress:

  * ``derive_topics`` / ``daydream`` are fully LOCAL. They read the founder's own
    gaps (assumptions lacking evidence or held at low confidence) and turn them
    into research questions, optionally surfacing the top ones as notifications.
    No network, no approval — this is the "daydream" that decides *what* is worth
    researching.
  * ``queue_research`` / ``run_from_work_item`` are the only path that touches the
    network, and it is an L3 external action: it runs only through an approved
    work item + the typed confirmation phrase, exactly like a connector sync.

The web-fetch lane is bounded and now unified under the data-class egress gate:
https only, public hosts only (netguard blocks private / internal), redirects
refused, and a byte cap — one fetch policy reviewed in one place. Each run is
classified ``public_derived -> public_web`` and passes through ``authorize_egress``
before any fetch, so research egress shows up in the same egress ledger as
everything else and honors provider_policy (under ``local_only`` it is refused —
research is no longer a special exception; local-only means nothing leaves). A
STANDING ``public_derived:public_web`` grant is the founder's config-level opt-in;
the per-run L3 typed-phrase approval remains the real decision. Fetched pages are
salience-scored against the topic and filed as graded
evidence (external claim, never native certainty), reusing the same ingestion +
evidence path connectors use. The fetcher is injectable so all of this is
testable without a live network.
"""
from __future__ import annotations

import hashlib
import html as html_module
import re
import urllib.error
import urllib.parse
from typing import Any, Callable

from . import netguard
from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .egress import DataClass, EgressPolicy, EgressRequest, authorize_egress
from .founder import FounderService
from .ingestion import IngestionService

from .autonomy import WorkQueueService


RESEARCH_RUN_ACTION = "external.research.run"

# Reference-URL templates the local source proposer builds from a topic when the
# founder asks for research without naming sources. Path-based canonical forms are
# preferred (they don't redirect, which the fetch lane refuses); the fulltext
# search form is the reliable fallback. These are starting points the founder
# edits behind the approval gate, not curated authorities.
DEFAULT_SOURCE_SEEDS = (
    "https://en.wikipedia.org/wiki/{slug}",
    "https://en.wikipedia.org/w/index.php?search={query}&fulltext=1",
)

_STOPWORDS = frozenset(
    {"the", "and", "for", "are", "was", "our", "will", "with", "that", "this", "from", "into",
     "what", "which", "about", "evidence", "against", "should", "would", "could", "does", "have"}
)

Fetcher = Callable[..., str]


class ResearchService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: FounderService,
        ingestion: IngestionService,
        work_queue: WorkQueueService,
        bus: Any | None = None,
        fetcher: Fetcher | None = None,
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.ingestion = ingestion
        self.work_queue = work_queue
        self.bus = bus
        # Injected fetcher wins for tests; else the module-level fetch_url is
        # resolved by name at call time so monkeypatching it takes effect.
        self._fetcher = fetcher

    # ---- registration ----
    def register_into(self, registry: Any) -> list[str]:
        if not self.config.research.enabled:
            return []
        registry.register(
            RESEARCH_RUN_ACTION,
            "Fetch approved web sources for a research topic and file them as graded evidence (approved external action).",
            self.run_from_work_item,
        )
        return [RESEARCH_RUN_ACTION]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.research.enabled,
            "max_urls_per_run": self.config.research.max_urls_per_run,
            "allow_hosts": list(self.config.research.allow_hosts) or "any public https host",
            "egress_policy": "https only, public hosts only, redirects refused, byte-capped, approval-gated",
        }

    # ---- local topic derivation (the "daydream") ----
    def derive_topics(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """Turn the founder's evidence gaps into ranked research questions. Local only."""
        topics: list[dict[str, Any]] = []
        for assumption in self.founder.list_assumptions(limit=50):
            statement = str(assumption.get("statement", "")).strip()
            if not statement:
                continue
            evidence = assumption.get("evidence") or []
            confidence = int(assumption.get("confidence", 50) or 50)
            if confidence >= 70:
                continue  # near-certain: low research value, whether or not it has evidence
            score = min(100, (0 if evidence else 40) + max(0, 60 - confidence))
            topics.append(
                {
                    "question": f"What is the evidence for or against: {statement}",
                    "rationale": "No evidence attached." if not evidence else f"Held at low confidence ({confidence}).",
                    "source": f"assumption:{assumption.get('id')}",
                    "statement": statement,
                    "score": score,
                }
            )
        topics.sort(key=lambda topic: topic["score"], reverse=True)
        return topics[:limit]

    def propose_sources(self, topic: str, *, limit: int | None = None) -> list[str]:
        """Propose candidate web sources for *topic*, fully LOCAL — no network, no
        approval. This is the source half of the "daydream": given a topic the
        founder wants researched but no URLs, build well-formed reference URLs the
        founder can approve or edit in the Inbox before any fetch.

        Every candidate is run through the same egress policy the fetch lane uses
        (https only, host allowlist honored, private/internal refused), so a
        proposal can never smuggle in an unfetchable or internal target. These are
        editable candidates behind an approval gate, never asserted as authoritative.
        """
        topic = (topic or "").strip()
        if not topic:
            return []
        cap = limit or self.config.research.max_urls_per_run
        allowed = frozenset(self.config.research.allow_hosts)
        words = re.findall(r"[A-Za-z0-9]+", topic)
        if not words:
            return []
        slug = "_".join(word.capitalize() for word in words)
        query = urllib.parse.quote(" ".join(words))
        candidates = [template.format(slug=slug, query=query) for template in DEFAULT_SOURCE_SEEDS]
        if allowed:
            allowed_lower = {host.lower() for host in allowed}
            on_allowed = [url for url in candidates if _host(url) in allowed_lower]
            # If none of the default reference hosts are allowlisted, fall back to
            # the allowed hosts' landing pages so we can still propose something valid.
            candidates = on_allowed or [f"https://{host}/" for host in sorted(allowed)]
        proposed: list[str] = []
        for url in candidates:
            if url in proposed:
                continue
            try:
                netguard.assert_allowed(url, require_https=True, allowed_hosts=allowed or None)
            except netguard.EgressError:
                continue
            proposed.append(url)
            if len(proposed) >= cap:
                break
        return proposed

    def daydream(self, *, limit: int = 3, notify: bool = True) -> dict[str, Any]:
        """Derive top research questions and (optionally) surface them as notifications."""
        topics = self.derive_topics(limit=limit)
        notified = 0
        if notify and self.bus is not None:
            for topic in topics:
                self.bus.notify(
                    topic="research",
                    title="Research opportunity",
                    body=topic["question"],
                    severity="info",
                    source="research",
                    dedupe_key=f"research:{topic['source']}",
                )
                notified += 1
        return {"topics": topics, "notified": notified}

    # ---- queue (approval-gated egress) ----
    def queue_research(self, *, topic: str, urls: list[str], create_evidence: bool = True) -> dict[str, Any]:
        self._require_enabled()
        topic = topic.strip()
        if not topic:
            raise ValueError("Research requires a topic or question.")
        clean = self._validate_urls(urls)
        detail = (
            "Autonomous web research (approved external fetch). This reaches out to public web sources.\n"
            f"Topic: {topic}\nURLs:\n" + "\n".join(f"- {url}" for url in clean)
        )
        result = self.work_queue.enqueue(
            kind="research_run",
            title=f"Research: {topic[:80]}",
            detail=detail,
            action=RESEARCH_RUN_ACTION,
            target="web-research",  # keep the authority-scanned target clean; topic/urls live in metadata/detail
            permission_tier="L3_EXTERNAL_ACTION",
            priority=50,
            source="research",
            metadata={"topic": topic, "urls": clean, "create_evidence": create_evidence},
            unique_key=f"{RESEARCH_RUN_ACTION}:{_digest(topic + ''.join(clean))}:{utc_now()}",
        )
        return result.as_dict() | {"url_count": len(clean)}

    # ---- dispatch handler ----
    def run_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        topic = str(metadata.get("topic", "")).strip()
        urls = metadata.get("urls") or []
        create_evidence = bool(metadata.get("create_evidence", True))
        if not topic or not isinstance(urls, list) or not urls:
            raise ValueError("Research work item is missing a topic or URLs.")
        # Re-validate egress at dispatch time (the item may have been edited).
        urls = self._validate_urls(urls)
        # Data-class egress gate: this run's fetches are public_derived -> public_web
        # (a query/URL to the open web). The gate CLASSIFIES + audits the egress and
        # defers the real decision to research's own L3 approval via a STANDING grant
        # — but it also means research is now part of the unified posture: under
        # provider_policy=local_only (or without the standing grant) it is refused,
        # so "local_only" truly means nothing leaves, research included.
        egress = authorize_egress(
            self.db,
            EgressPolicy.from_config(self.config),
            EgressRequest(
                request_id=f"research:{item.id}",
                data_class=DataClass.PUBLIC_DERIVED,
                vendor="public_web",
                purpose=f"web research: {topic[:80]}",
                byte_estimate=len(urls),
            ),
            preview=f"web research on '{topic[:80]}' ({len(urls)} source(s))",
        )
        if not egress.allowed:
            self.db.audit(
                actor="approved-handler", action=RESEARCH_RUN_ACTION, target="web-research",
                permission_tier=item.permission_tier, status="refused",
                details={"work_item_id": item.id, "egress": egress.audit_record()},
            )
            return {
                "handler": RESEARCH_RUN_ACTION, "status": "refused", "ok": False,
                "topic": topic, "reason": egress.reason, "matched_rule": egress.matched_rule,
                "fetched": 0, "filed": 0, "findings": [],
            }
        fetcher = self._fetcher or fetch_url
        allowed = frozenset(self.config.research.allow_hosts) or None
        findings: list[dict[str, Any]] = []
        filed = 0
        for url in urls:
            try:
                raw = fetcher(
                    url,
                    timeout=self.config.research.fetch_timeout_seconds,
                    max_bytes=self.config.research.max_fetch_bytes,
                    allowed_hosts=allowed,
                )
            except ValueError as exc:
                findings.append({"url": url, "status": "error", "error": str(exc)[:200]})
                continue
            text = _html_to_text(raw)[: self.config.research.max_text_chars]
            salience = _salience(topic, text)
            document_id = None
            evidence_id = None
            filing_error = ""
            # Filing (embed + evidence) is isolated: a fetched page must still be
            # reported with its text + salience even if ingestion/embeddings hiccup.
            if text:
                try:
                    ingested = self.ingestion.ingest_text(
                        title=f"Research: {topic[:80]} ({_host(url)})",
                        text=text,
                        source=f"research:{url}",
                        metadata={
                            "topic": topic,
                            "url": url,
                            "salience": salience,
                            "entity_boundary": "External web source imported as research evidence, not native certainty.",
                        },
                    )
                    document_id = ingested.document_id
                    if create_evidence:
                        evidence = self.founder.create_evidence(
                            {
                                "evidence_type": "web_research",
                                "source": f"research:{url}",
                                "reliability": self.config.research.default_reliability,
                                "claim_supported": f"Web source on '{topic}': {text[:400]}",
                                "strength": min(90, 30 + salience // 2),
                                "notes": "Imported from approved autonomous web research. Treat as a sourced external claim.",
                                "metadata": {
                                    "url": url,
                                    "topic": topic,
                                    "salience": salience,
                                    "document_id": document_id,
                                    "entity_boundary": "External source says; Zade records as research evidence.",
                                },
                            }
                        )
                        evidence_id = evidence.id
                        filed += 1
                except Exception as exc:  # noqa: BLE001 - a filing hiccup must not discard a fetched finding
                    filing_error = str(exc)[:200]
            finding = {
                "url": url,
                "status": "ok",
                "salience": salience,
                "chars": len(text),
                "evidence_id": evidence_id,
                "document_id": document_id,
                "excerpt": text[:300],
            }
            if filing_error:
                finding["filing_error"] = filing_error
            findings.append(finding)

        fetched = sum(1 for finding in findings if finding["status"] == "ok")
        ok = fetched > 0
        self.db.audit(
            actor="approved-handler",
            action=RESEARCH_RUN_ACTION,
            target="web-research",
            permission_tier=item.permission_tier,
            status="ok" if ok else "flow_error",
            details={"work_item_id": item.id, "topic": topic, "urls": urls, "fetched": fetched, "filed": filed},
        )
        return {
            "handler": RESEARCH_RUN_ACTION,
            "status": "ok" if ok else "flow_error",
            "ok": ok,
            "topic": topic,
            "fetched": fetched,
            "filed": filed,
            "findings": findings,
        }

    # ---- internals ----
    def _require_enabled(self) -> None:
        if not self.config.research.enabled:
            raise ValueError("Autonomous research is disabled (research.enabled = false).")

    def _validate_urls(self, urls: Any) -> list[str]:
        if not isinstance(urls, list) or not urls:
            raise ValueError("Research requires a non-empty list of URLs.")
        if len(urls) > self.config.research.max_urls_per_run:
            raise ValueError(f"Too many URLs (max {self.config.research.max_urls_per_run}).")
        allowed = frozenset(self.config.research.allow_hosts) or None
        clean: list[str] = []
        for url in urls:
            url = str(url).strip()
            try:
                netguard.assert_allowed(url, require_https=True, allowed_hosts=allowed)
            except netguard.EgressError as exc:
                raise ValueError(f"Refused research URL {url!r}: {exc}") from exc
            clean.append(url)
        return clean


# ---- module-level fetch (the actual egress) ----
def fetch_url(url: str, *, timeout: float = 20.0, max_bytes: int = 2_000_000, allowed_hosts: Any | None = None) -> str:
    """Fetch a public https page under egress policy; return decoded HTML/text.

    SSRF-guarded exactly like the ICS fetch: https only, no private/internal
    hosts, redirects refused (a validated public URL cannot 3xx-hop inward), and
    a hard byte cap so a huge page cannot blow up memory or the evidence store.
    """
    try:
        netguard.assert_allowed(url, require_https=True, allowed_hosts=allowed_hosts)
    except netguard.EgressError as exc:
        raise ValueError(str(exc)) from exc
    try:
        with netguard.NO_REDIRECT_OPENER.open(url, timeout=timeout) as response:  # noqa: S310 - scheme+host validated
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("Research URL returned a redirect; redirects are refused to prevent internal-host hops.") from exc
        raise ValueError(f"Research fetch failed (HTTP {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Research fetch failed: {exc.reason}") from exc
    return raw[:max_bytes].decode("utf-8", errors="replace")


def _html_to_text(raw_html: str) -> str:
    without_blocks = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
    stripped = re.sub(r"(?s)<[^>]+>", " ", without_blocks)
    unescaped = html_module.unescape(stripped)
    return re.sub(r"\s+", " ", unescaped).strip()


def _salience(topic: str, text: str) -> int:
    terms = {token for token in re.findall(r"[a-z0-9]{3,}", topic.lower())} - _STOPWORDS
    if not terms:
        return 0
    low = text.lower()
    hits = sum(1 for term in terms if term in low)
    return round(100 * hits / len(terms))


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "source").lower()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
