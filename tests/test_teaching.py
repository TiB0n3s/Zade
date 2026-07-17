"""Deep Thought teaching bridge: importing external material into memory as
evidence, and linking that evidence to founder objects, must be idempotent —
re-importing a candidate or re-linking the same (evidence, target) pair must not
mint duplicate evidence, documents, or links.
"""
from pathlib import Path

from cofounder_kernel.config import (
    AppConfig,
    IdentityConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
    ensure_local_paths,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.founder import FounderService
from cofounder_kernel.ingestion import IngestionService
from cofounder_kernel.teaching import DeepThoughtTeachingBridge, _reliability_for_path


class FakeEmbedder:
    """Deterministic non-empty vector so ingestion stores embeddings without a
    live Ollama; content still round-trips through the real store path."""

    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        return [0.0, 1.0]


def _make(tmp_path: Path):
    config = KernelConfig(
        app=AppConfig(),
        identity=IdentityConfig(name="Zade"),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    founder = FounderService(config=config, db=db)
    ingestion = IngestionService(config=config, db=db, embedder=FakeEmbedder())
    bridge = DeepThoughtTeachingBridge(config=config, db=db, founder=founder, ingestion=ingestion)
    return bridge, founder, db


def _count(db: KernelDatabase, table: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _goal_evidence_ids(db: KernelDatabase, goal_id: int) -> list[int]:
    import json

    with db.connect() as conn:
        row = conn.execute("SELECT evidence_ids_json FROM founder_goals WHERE id = ?", (goal_id,)).fetchone()
    return json.loads(row["evidence_ids_json"] or "[]")


def _teaching_import_requests(db: KernelDatabase, status: str | None = None) -> list[dict]:
    with db.connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT id, status FROM approval_requests WHERE source_type = 'teaching_import' AND status = ? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, status FROM approval_requests WHERE source_type = 'teaching_import' ORDER BY id"
            ).fetchall()
    return [dict(row) for row in rows]


def _candidate_status(db: KernelDatabase, candidate_id: int) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM teaching_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return row["status"] if row else None


def _weak_evidence_status(db: KernelDatabase, goal_id: int) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT status FROM integrity_warnings
            WHERE warning_type = 'weak_evidence' AND subject_type = 'goal' AND subject_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (goal_id,),
        ).fetchone()
    return row["status"] if row else None


def _write_source(tmp_path: Path, name: str = "deep-thought-standing-brief.md") -> Path:
    source = tmp_path / name
    source.write_text(
        "Deep Thought standing brief.\n\n"
        "Bootstrap Zade Founder OS requires sourced evidence and object links.",
        encoding="utf-8",
    )
    return source


def test_scan_upserts_by_content_hash(tmp_path: Path) -> None:
    """Re-scanning the same file returns the same candidate row (content-hash
    keyed), not a second one."""
    bridge, _founder, db = _make(tmp_path)
    source = _write_source(tmp_path)

    first = bridge.scan(paths=[str(source)], limit=5)
    second = bridge.scan(paths=[str(source)], limit=5)

    assert first["candidates"][0]["created"] is True
    assert second["candidates"][0]["created"] is False
    assert first["candidates"][0]["id"] == second["candidates"][0]["id"]
    assert _count(db, "teaching_candidates") == 1


def test_import_is_idempotent_no_duplicate_evidence(tmp_path: Path) -> None:
    """Importing an already-imported candidate (reachable via explicit
    candidate_ids) must not mint a second evidence row."""
    bridge, _founder, db = _make(tmp_path)
    source = _write_source(tmp_path)
    candidate_id = bridge.scan(paths=[str(source)], limit=5)["candidates"][0]["id"]

    first = bridge.import_candidates(candidate_ids=[candidate_id])
    assert first["imported"][0]["status"] == "imported"
    evidence_id = first["imported"][0]["evidence_id"]
    document_id = first["imported"][0]["document_id"]
    assert evidence_id is not None and document_id is not None
    assert _count(db, "founder_evidence") == 1

    # Re-import the same candidate: no new evidence, no new document.
    second = bridge.import_candidates(candidate_ids=[candidate_id])
    assert second["imported"][0]["status"] == "already_imported"
    assert second["imported"][0]["evidence_id"] == evidence_id
    assert second["imported"][0]["document_id"] == document_id
    assert _count(db, "founder_evidence") == 1
    assert _count(db, "documents") == 1


def test_link_evidence_is_idempotent_no_duplicate_links(tmp_path: Path) -> None:
    """Re-linking the same (evidence, relation, target) must not create a second
    founder_links row, and must not double-list the evidence id on the target."""
    bridge, founder, db = _make(tmp_path)
    source = _write_source(tmp_path)
    goal = founder.create_goal({"name": "Bootstrap Zade Founder OS", "metric": "evidence", "target": "linked source"})
    candidate_id = bridge.scan(paths=[str(source)], limit=5)["candidates"][0]["id"]
    evidence_id = bridge.import_candidates(candidate_ids=[candidate_id])["imported"][0]["evidence_id"]

    first = bridge.link_evidence(evidence_id=evidence_id, to_type="goal", to_id=goal.id, relation="supports")
    assert first["deduped"] is False
    assert first["target"] == {"type": "goal", "id": goal.id}
    assert _count(db, "founder_links") == 1
    assert _goal_evidence_ids(db, goal.id) == [evidence_id]

    second = bridge.link_evidence(evidence_id=evidence_id, to_type="goal", to_id=goal.id, relation="supports")
    assert second["deduped"] is True
    assert _count(db, "founder_links") == 1  # no duplicate link
    assert _goal_evidence_ids(db, goal.id) == [evidence_id]  # still listed once


def test_evidence_loop_second_run_adds_no_new_evidence_or_links(tmp_path: Path) -> None:
    """The end-to-end loop is idempotent: a second run re-imports nothing (the
    candidate is already imported) and therefore creates no new evidence or
    links."""
    bridge, founder, db = _make(tmp_path)
    founder.create_goal({"name": "Validate willingness to pay", "metric": "interviews", "target": "5 calls"})
    source = tmp_path / "goal-evidence.md"
    source.write_text(
        "Validate willingness to pay depends on pricing interviews and source-backed conversion evidence.",
        encoding="utf-8",
    )
    bridge.scan(paths=[str(source)], limit=5)

    # Authorized path (require_approval=False) so the loop actually imports; the
    # point here is idempotency across runs, not the gate.
    loop1 = bridge.evidence_loop(max_import=5, require_approval=False)
    assert loop1["imported"]["count"] == 1
    assert len(loop1["links"]) >= 1
    evidence_after_1 = _count(db, "founder_evidence")
    links_after_1 = _count(db, "founder_links")

    loop2 = bridge.evidence_loop(max_import=5, require_approval=False)
    assert loop2["imported"]["count"] == 0  # already imported -> nothing new
    assert loop2["links"] == []
    assert _count(db, "founder_evidence") == evidence_after_1
    assert _count(db, "founder_links") == links_after_1


def test_anecdotal_evidence_does_not_resolve_weak_evidence_warning(tmp_path: Path) -> None:
    """A D-grade / thin-strength import must NOT clear the 'anecdotal or absent'
    integrity warning — mere presence of evidence is not enough."""
    bridge, founder, db = _make(tmp_path)
    goal = founder.create_goal({"name": "Grow pipeline", "metric": "deals", "target": "10", "owner": "Ellie"})
    founder.run_integrity_check()  # goal has no evidence -> weak_evidence warning opens
    assert _weak_evidence_status(db, goal.id) == "open"

    anecdote = founder.create_evidence(
        {"evidence_type": "note", "source": "hallway chat", "reliability": "D", "strength": 40, "claim_supported": "vibes"}
    )
    bridge.link_evidence(evidence_id=anecdote.id, to_type="goal", to_id=goal.id, relation="supports")

    resolved = bridge._resolve_weak_evidence_warnings()
    assert resolved == []
    assert _weak_evidence_status(db, goal.id) == "open"  # still flagged — anecdotal doesn't count


def test_credible_evidence_resolves_weak_evidence_warning(tmp_path: Path) -> None:
    """Genuinely credible evidence (B-grade, strength >= 50) DOES clear the
    warning — the gate blocks anecdotal evidence, not real evidence."""
    bridge, founder, db = _make(tmp_path)
    goal = founder.create_goal({"name": "Grow pipeline", "metric": "deals", "target": "10", "owner": "Ellie"})
    founder.run_integrity_check()
    assert _weak_evidence_status(db, goal.id) == "open"

    sourced = founder.create_evidence(
        {"evidence_type": "report", "source": "pilot results", "reliability": "B", "strength": 75, "claim_supported": "conversion"}
    )
    bridge.link_evidence(evidence_id=sourced.id, to_type="goal", to_id=goal.id, relation="supports")

    resolved = bridge._resolve_weak_evidence_warnings()
    assert len(resolved) == 1
    assert resolved[0]["goal_id"] == goal.id
    assert resolved[0]["credible_evidence_ids"] == [sourced.id]
    assert _weak_evidence_status(db, goal.id) == "resolved"


def test_evidence_loop_gated_by_default_imports_nothing_and_files_approval(tmp_path: Path) -> None:
    """The autonomous loop must NOT auto-import external Deep Thought material.
    By default it surfaces a pending founder-approval request and imports nothing."""
    bridge, founder, db = _make(tmp_path)
    founder.create_goal({"name": "Bootstrap Zade Founder OS", "metric": "evidence", "target": "linked"})
    source = _write_source(tmp_path)
    bridge.scan(paths=[str(source)], limit=5)

    result = bridge.evidence_loop(max_import=5)  # require_approval defaults to True

    assert result["status"] == "awaiting_approval"
    assert result["imported"]["count"] == 0
    assert result["links"] == []
    assert result["pending_approval"]["approval_request_id"] is not None
    # Nothing entered the belief graph.
    assert _count(db, "founder_evidence") == 0
    assert _count(db, "founder_links") == 0
    # Exactly one pending gate request, and re-running the loop refreshes it (no pile-up).
    assert len(_teaching_import_requests(db, status="pending")) == 1
    bridge.evidence_loop(max_import=5)
    assert len(_teaching_import_requests(db, status="pending")) == 1


def test_explicit_import_after_gate_imports_and_resolves_approval(tmp_path: Path) -> None:
    """An explicit founder import IS the approval: it imports the candidate and
    clears the pending gate request."""
    bridge, founder, db = _make(tmp_path)
    source = _write_source(tmp_path)
    candidate_id = bridge.scan(paths=[str(source)], limit=5)["candidates"][0]["id"]

    bridge.evidence_loop(max_import=5)  # gated -> files pending request, imports nothing
    assert len(_teaching_import_requests(db, status="pending")) == 1
    assert _count(db, "founder_evidence") == 0

    imported = bridge.import_candidates(candidate_ids=[candidate_id])  # explicit founder action
    assert imported["imported"][0]["status"] == "imported"
    assert _count(db, "founder_evidence") == 1
    assert _teaching_import_requests(db, status="pending") == []
    assert len(_teaching_import_requests(db, status="approved")) == 1


def test_evidence_loop_require_approval_false_imports_directly(tmp_path: Path) -> None:
    """The explicit-authorization path (require_approval=False) still imports and
    links, and files no gate request."""
    bridge, founder, db = _make(tmp_path)
    founder.create_goal({"name": "Bootstrap Zade Founder OS", "metric": "evidence", "target": "linked"})
    source = _write_source(tmp_path)
    bridge.scan(paths=[str(source)], limit=5)

    result = bridge.evidence_loop(max_import=5, require_approval=False)

    assert result["status"] == "ok"
    assert result["imported"]["count"] == 1
    assert result["pending_approval"] is None
    assert _count(db, "founder_evidence") == 1
    assert _teaching_import_requests(db) == []


def test_denied_gate_request_declines_candidates_and_stops_resurfacing(tmp_path: Path) -> None:
    """A founder denial sticks: the denied candidates are marked 'declined' and the
    autonomous gate never re-surfaces them on a later pass."""
    bridge, _founder, db = _make(tmp_path)
    source = _write_source(tmp_path)
    candidate_id = bridge.scan(paths=[str(source)], limit=5)["candidates"][0]["id"]

    bridge.evidence_loop(max_import=5)  # gate files a pending request
    pending = _teaching_import_requests(db, status="pending")
    assert len(pending) == 1

    # Founder denies it through the generic approvals console path.
    db.resolve_approval_request(pending[0]["id"], status="denied", resolved_by="founder")

    # Next autonomous pass: deny sticks — candidate declined, nothing re-filed.
    result = bridge.evidence_loop(max_import=5)
    assert result["status"] == "ok"
    assert result["pending_approval"] is None
    assert _teaching_import_requests(db, status="pending") == []
    assert _candidate_status(db, candidate_id) == "declined"
    assert _count(db, "founder_evidence") == 0

    # And it stays declined across further passes (idempotent, no re-nag).
    bridge.evidence_loop(max_import=5)
    assert _teaching_import_requests(db, status="pending") == []
    assert _candidate_status(db, candidate_id) == "declined"


def test_explicit_import_overrides_a_declined_candidate(tmp_path: Path) -> None:
    """An explicit founder import by id still overrides a prior denial — the
    candidate_ids path has no status filter."""
    bridge, _founder, db = _make(tmp_path)
    source = _write_source(tmp_path)
    candidate_id = bridge.scan(paths=[str(source)], limit=5)["candidates"][0]["id"]
    bridge.evidence_loop(max_import=5)
    db.resolve_approval_request(
        _teaching_import_requests(db, status="pending")[0]["id"], status="denied", resolved_by="founder"
    )
    bridge.evidence_loop(max_import=5)  # reconcile -> declined
    assert _candidate_status(db, candidate_id) == "declined"

    imported = bridge.import_candidates(candidate_ids=[candidate_id])  # explicit override
    assert imported["imported"][0]["status"] == "imported"
    assert _count(db, "founder_evidence") == 1
    assert _candidate_status(db, candidate_id) == "imported"


def test_filename_scan_cannot_mint_grade_a() -> None:
    """A spoofable path (a folder named 'runtime-verified') must not mint grade A
    / strength 90 — auto-scan reliability caps at B."""
    spoof = Path(r"C:\AI Brain\runtime\verified\totally-legit.md")
    assert _reliability_for_path(spoof) == "B"
    # Existing tiers are unchanged.
    assert _reliability_for_path(Path("deep-thought-standing-brief.md")) == "B"
    assert _reliability_for_path(Path(r"C:\x\architecture\notes.md")) == "C"
    assert _reliability_for_path(Path("random.md")) == "D"
    # Grade A is never produced by a bare filename scan.
    for candidate in [spoof, Path("A-verified-runtime-A.md"), Path(r"C:\runtime\validation\x.md")]:
        assert _reliability_for_path(candidate) != "A"
