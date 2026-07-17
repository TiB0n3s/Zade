from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .founder import FounderService
from .ingestion import IngestionService, SUPPORTED_TEXT_EXTENSIONS, normalize_text, read_text_file


SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    "run-logs",
    "_ingested",
    ".obsidian",
    ".smart-env",
    ".deepthought-trash",
    ".trash",
    "graphify-out",
}


class DeepThoughtTeachingBridge:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: FounderService,
        ingestion: IngestionService,
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.ingestion = ingestion

    def scan(
        self,
        *,
        paths: list[str] | None = None,
        limit: int = 25,
        max_file_bytes: int = 500_000,
    ) -> dict[str, Any]:
        candidates = []
        scanned = 0
        for file_path in self._iter_source_files(paths or self._default_paths(), limit=limit, max_file_bytes=max_file_bytes):
            scanned += 1
            try:
                text = normalize_text(read_text_file(file_path))
            except Exception:
                continue
            if not text:
                continue
            candidate = self._candidate_from_file(file_path, text)
            candidate_id, created = self._upsert_candidate(candidate)
            candidates.append(candidate | {"id": candidate_id, "created": created})
            if len(candidates) >= limit:
                break
        self.db.audit(
            actor="teaching.deepthought",
            action="teach.deepthought.scan",
            target="Deep Thought sources",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"scanned": scanned, "candidates": len(candidates), "paths": paths or []},
        )
        return {"scanned_files": scanned, "candidates": candidates, "count": len(candidates)}

    def list_candidates(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            query = "SELECT * FROM teaching_candidates WHERE status = ? ORDER BY id DESC LIMIT ?"
            params: list[Any] = [status, limit]
        else:
            query = "SELECT * FROM teaching_candidates ORDER BY id DESC LIMIT ?"
            params = [limit]
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_candidate_row_to_dict(row) for row in rows]

    def import_candidates(
        self,
        *,
        candidate_ids: list[int] | None = None,
        import_all_candidates: bool = False,
        limit: int = 10,
        ingest_documents: bool = True,
        create_evidence: bool = True,
    ) -> dict[str, Any]:
        candidates = self._select_candidates(
            candidate_ids=candidate_ids or [],
            import_all_candidates=import_all_candidates,
            limit=limit,
        )
        imported = []
        already_imported = 0
        for candidate in candidates:
            if candidate.get("status") == "imported":
                # Idempotent: an already-imported candidate is never re-imported.
                # The explicit candidate_ids path has no status filter, so without
                # this a repeat import would mint a second evidence row for the same
                # source. Return its existing ids (recorded at first import) and
                # create nothing new.
                existing_meta = candidate.get("metadata") or {}
                already_imported += 1
                imported.append(
                    candidate
                    | {
                        "evidence_id": existing_meta.get("evidence_id"),
                        "document_id": existing_meta.get("document_id"),
                        "status": "already_imported",
                    }
                )
                continue
            text = self._read_candidate_text(candidate)
            if not text:
                text = candidate["excerpt"]
            document_id = None
            if ingest_documents:
                result = self.ingestion.ingest_text(
                    title=candidate["title"],
                    text=text,
                    source=f"teach:deepthought:{candidate['source_uri']}",
                    metadata={
                        "source_system": candidate["source_system"],
                        "source_uri": candidate["source_uri"],
                        "candidate_id": candidate["id"],
                        "entity_boundary": "Deep Thought source imported into Zade as evidence, not native certainty.",
                    },
                )
                document_id = result.document_id
            evidence_id = None
            if create_evidence:
                evidence = self.founder.create_evidence(
                    {
                        "evidence_type": "deepthought_import",
                        "source": candidate["source_uri"],
                        "reliability": candidate["reliability"],
                        "claim_supported": self._claim_from_candidate(candidate),
                        "strength": _strength_for_reliability(candidate["reliability"]),
                        "notes": (
                            "Imported from Deep Thought teaching bridge. "
                            "Treat as sourced Deep Thought knowledge, not direct Zade certainty."
                        ),
                        "metadata": {
                            "source_system": candidate["source_system"],
                            "candidate_id": candidate["id"],
                            "document_id": document_id,
                            "source_uri": candidate["source_uri"],
                            "entity_boundary": "Deep Thought says; Zade records as evidence.",
                        },
                    }
                )
                evidence_id = evidence.id
            self._mark_candidate_imported(candidate["id"], evidence_id=evidence_id, document_id=document_id)
            imported.append(candidate | {"evidence_id": evidence_id, "document_id": document_id, "status": "imported"})
        self.db.audit(
            actor="teaching.deepthought",
            action="teach.deepthought.import",
            target="teaching_candidates",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={
                "imported_count": len(imported) - already_imported,
                "already_imported": already_imported,
                "candidate_ids": [item["id"] for item in imported],
            },
        )
        # An explicit import IS the founder's approval — clear any pending gate
        # request whose candidates are now imported.
        self._resolve_import_approvals()
        return {"imported": imported, "count": len(imported)}

    def link_evidence(
        self,
        *,
        evidence_id: int,
        to_type: str,
        to_id: int,
        relation: str = "supports",
        strength: int = 60,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_target_type(to_type)
        self._attach_evidence_id(normalized, to_id, evidence_id, relation=relation)
        existing = self._find_link(evidence_id, to_type=normalized, to_id=to_id, relation=relation)
        if existing is not None:
            # Idempotent: the same (evidence, relation, target) link already exists.
            # Return it instead of inserting a duplicate founder_links row — the
            # /teach/deepthought/link endpoint can be called repeatedly, and
            # create_link is a bare INSERT with no dedup of its own.
            link_record = existing
            link_id = int(existing["id"])
            deduped = True
        else:
            link = self.founder.create_link(
                {
                    "from_type": "evidence",
                    "from_id": evidence_id,
                    "relation": relation,
                    "to_type": normalized,
                    "to_id": to_id,
                    "strength": strength,
                    "metadata": {
                        "source": "teach.deepthought",
                        "entity_boundary": "Deep Thought-derived evidence linked to Zade operating object.",
                        **(metadata or {}),
                    },
                }
            )
            link_record = link.record
            link_id = link.id
            deduped = False
        self.db.audit(
            actor="teaching.deepthought",
            action="teach.deepthought.link",
            target=f"{normalized}:{to_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"evidence_id": evidence_id, "link_id": link_id, "relation": relation, "deduped": deduped},
        )
        return {
            "link": link_record,
            "evidence_id": evidence_id,
            "target": {"type": normalized, "id": to_id},
            "deduped": deduped,
        }

    def evidence_gaps(self) -> dict[str, Any]:
        integrity = self.founder.run_integrity_check()
        dashboard = self.founder.dashboard()
        missing_goal_evidence = [
            goal
            for goal in self.founder.list_goals(status="active", limit=100)
            if not goal.get("evidence_ids")
        ]
        missing_bet_evidence = [
            bet
            for bet in self.founder.list_strategy_objects(object_type="active_bet", limit=100)
            if not bet.get("evidence_ids")
        ]
        return {
            "integrity_warnings": integrity["warnings"],
            "knowledge_gaps": dashboard["knowledge_gaps"],
            "missing_goal_evidence": missing_goal_evidence,
            "missing_bet_evidence": missing_bet_evidence,
            "next_evidence_needed": _next_evidence_needed(missing_goal_evidence, missing_bet_evidence, dashboard["knowledge_gaps"]),
        }

    def evidence_loop(
        self,
        *,
        import_candidates: bool = True,
        max_import: int = 5,
        link_goals: bool = True,
        clear_resolved_warnings: bool = True,
        require_approval: bool = True,
    ) -> dict[str, Any]:
        imported = {"imported": [], "count": 0}
        pending_approval = None
        if import_candidates and max_import > 0:
            if require_approval:
                # Founder-approval gate. The autonomous loop must NOT promote
                # external Deep Thought material into the belief graph on its own:
                # import creates evidence (a Bayesian input) and links it to goals/
                # bets. Instead, surface the candidates as a pending approval the
                # founder can see and deny; the import runs only on an explicit
                # founder action (the /teach/deepthought/import endpoint, or
                # evidence_loop with require_approval=False).
                # Honor prior denials first: a denied gate request marks its
                # candidates 'declined' so they drop out of the selection below.
                self._reconcile_declined_candidates()
                previewable = self._select_candidates(
                    candidate_ids=[], import_all_candidates=True, limit=max_import
                )
                if previewable:
                    pending_approval = self._request_import_approval(previewable)
            else:
                imported = self.import_candidates(import_all_candidates=True, limit=max_import)
        links = []
        # Linking and warning-resolution only happen on the authorized path — the
        # gated path imports nothing, so there is nothing to link or resolve.
        if link_goals and not require_approval:
            for item in imported["imported"]:
                evidence_id = item.get("evidence_id")
                if not evidence_id:
                    continue
                for suggested in item.get("suggested_links", []):
                    if suggested.get("to_type") in {"goal", "strategy_object", "bet", "prediction", "assumption"}:
                        links.append(
                            self.link_evidence(
                                evidence_id=evidence_id,
                                to_type=suggested["to_type"],
                                to_id=int(suggested["to_id"]),
                                relation=suggested.get("relation", "supports"),
                                strength=int(suggested.get("strength", 60)),
                                metadata={"candidate_id": item["id"], "auto_linked": True},
                            )
                        )
        resolved = self._resolve_weak_evidence_warnings() if (clear_resolved_warnings and not require_approval) else []
        gaps = self.evidence_gaps()
        status = "awaiting_approval" if pending_approval else "ok"
        event_id = self._log_runtime_event(
            status=status,
            response=gaps["next_evidence_needed"],
            details={
                "imported": imported,
                "links": links,
                "resolved_warnings": resolved,
                "gaps": gaps,
                "pending_approval": pending_approval,
            },
        )
        return {
            "event_id": event_id,
            "status": status,
            "imported": imported,
            "links": links,
            "resolved_warnings": resolved,
            "gaps": gaps,
            "pending_approval": pending_approval,
            "next_evidence_needed": gaps["next_evidence_needed"],
        }

    def auto_link_imported(self, *, limit: int = 50) -> dict[str, Any]:
        linked = []
        skipped = []
        errors = []
        for candidate in self.list_candidates(status="imported", limit=limit):
            metadata = candidate.get("metadata") or {}
            evidence_id = metadata.get("evidence_id")
            if not evidence_id:
                skipped.append({"candidate_id": candidate["id"], "reason": "no_evidence_id"})
                continue
            for suggested in candidate.get("suggested_links", []):
                try:
                    to_type = _normalize_target_type(str(suggested["to_type"]))
                    to_id = int(suggested["to_id"])
                    relation = str(suggested.get("relation") or "supports")
                    if self._link_exists(int(evidence_id), to_type=to_type, to_id=to_id, relation=relation):
                        skipped.append(
                            {
                                "candidate_id": candidate["id"],
                                "evidence_id": int(evidence_id),
                                "to_type": to_type,
                                "to_id": to_id,
                                "relation": relation,
                                "reason": "duplicate",
                            }
                        )
                        continue
                    linked.append(
                        self.link_evidence(
                            evidence_id=int(evidence_id),
                            to_type=to_type,
                            to_id=to_id,
                            relation=relation,
                            strength=int(suggested.get("strength", 60)),
                            metadata={"candidate_id": candidate["id"], "auto_linked": True},
                        )
                    )
                except Exception as exc:
                    errors.append({"candidate_id": candidate["id"], "suggestion": suggested, "error": str(exc)})
        audit_id = self.db.audit(
            actor="teaching.deepthought",
            action="teach.deepthought.auto_link",
            target="teaching_candidates",
            permission_tier="L1_MEMORY_WRITE",
            status="ok" if not errors else "degraded",
            details={
                "limit": limit,
                "linked_count": len(linked),
                "skipped_count": len(skipped),
                "error_count": len(errors),
            },
        )
        return {
            "linked": linked,
            "linked_count": len(linked),
            "skipped": skipped,
            "errors": errors,
            "audit_id": audit_id,
        }

    def _candidate_from_file(self, file_path: Path, text: str) -> dict[str, Any]:
        reliability = _reliability_for_path(file_path)
        excerpt = _best_excerpt(text)
        suggested_links = self._suggest_links(text, file_path)
        return {
            "source_system": "Deep Thought",
            "source_uri": str(file_path),
            "title": file_path.name,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "excerpt": excerpt,
            "candidate_type": _candidate_type(file_path, text),
            "reliability": reliability,
            "suggested_links": suggested_links,
            "metadata": {
                "path": str(file_path),
                "entity_boundary": "Source scanned from Deep Thought/AI Brain and must be imported as evidence.",
            },
        }

    def _suggest_links(self, text: str, file_path: Path) -> list[dict[str, Any]]:
        suggestions = []
        haystack = f"{file_path.name}\n{text}".lower()
        for goal in self.founder.list_goals(status="active", limit=100):
            score = _overlap_score(haystack, goal["name"])
            stable_id = str(goal.get("metadata", {}).get("stable_id", "")).lower()
            if (stable_id and stable_id in haystack) or score >= 2:
                suggestions.append({"to_type": "goal", "to_id": goal["id"], "relation": "supports", "strength": 65})
        for bet in self.founder.list_strategy_objects(object_type="active_bet", limit=100):
            score = _overlap_score(haystack, bet["title"])
            stable_id = str(bet.get("metadata", {}).get("stable_id", "")).lower()
            if (stable_id and stable_id in haystack) or score >= 3:
                suggestions.append({"to_type": "bet", "to_id": bet["id"], "relation": "supports", "strength": 60})
        for assumption in self.founder.list_assumptions(status="active", limit=100):
            score = _overlap_score(haystack, assumption["statement"])
            stable_id = str(assumption.get("metadata", {}).get("stable_id", "")).lower()
            if (stable_id and stable_id in haystack) or score >= 4:
                suggestions.append({"to_type": "assumption", "to_id": assumption["id"], "relation": "supports", "strength": 60})
        for prediction in self.founder.list_predictions(result="open", limit=100):
            stable_id = str(prediction.get("metadata", {}).get("stable_id", "")).lower()
            if stable_id and stable_id in haystack:
                suggestions.append({"to_type": "prediction", "to_id": prediction["id"], "relation": "informs", "strength": 55})
        return suggestions[:12]

    def _upsert_candidate(self, candidate: dict[str, Any]) -> tuple[int, bool]:
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id, status, metadata_json FROM teaching_candidates WHERE content_hash = ?",
                (candidate["content_hash"],),
            ).fetchone()
            if existing:
                metadata = json.loads(existing["metadata_json"] or "{}")
                metadata.update(candidate["metadata"])
                conn.execute(
                    """
                    UPDATE teaching_candidates
                    SET updated_at = ?, source_system = ?, source_uri = ?, title = ?, excerpt = ?,
                        candidate_type = ?, reliability = ?, suggested_links_json = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        utc_now(),
                        candidate["source_system"],
                        candidate["source_uri"],
                        candidate["title"],
                        candidate["excerpt"],
                        candidate["candidate_type"],
                        candidate["reliability"],
                        json.dumps(candidate["suggested_links"], sort_keys=True),
                        json.dumps(metadata, sort_keys=True),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"]), False
            cur = conn.execute(
                """
                INSERT INTO teaching_candidates (
                  created_at, updated_at, source_system, source_uri, title, content_hash,
                  excerpt, candidate_type, reliability, status, suggested_links_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    utc_now(),
                    utc_now(),
                    candidate["source_system"],
                    candidate["source_uri"],
                    candidate["title"],
                    candidate["content_hash"],
                    candidate["excerpt"],
                    candidate["candidate_type"],
                    candidate["reliability"],
                    json.dumps(candidate["suggested_links"], sort_keys=True),
                    json.dumps(candidate["metadata"], sort_keys=True),
                ),
            )
            return int(cur.lastrowid), True

    def _select_candidates(self, *, candidate_ids: list[int], import_all_candidates: bool, limit: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                rows = conn.execute(
                    f"SELECT * FROM teaching_candidates WHERE id IN ({placeholders}) ORDER BY id ASC LIMIT ?",
                    [*candidate_ids, limit],
                ).fetchall()
            elif import_all_candidates:
                rows = conn.execute(
                    "SELECT * FROM teaching_candidates WHERE status = 'candidate' ORDER BY reliability ASC, id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = []
        return [_candidate_row_to_dict(row) for row in rows]

    def _mark_candidate_imported(self, candidate_id: int, *, evidence_id: int | None, document_id: int | None) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT metadata_json FROM teaching_candidates WHERE id = ?", (candidate_id,)).fetchone()
            metadata = json.loads(row["metadata_json"] or "{}") if row else {}
            metadata.update({"evidence_id": evidence_id, "document_id": document_id, "imported_at": utc_now()})
            conn.execute(
                """
                UPDATE teaching_candidates
                SET updated_at = ?, status = 'imported', metadata_json = ?
                WHERE id = ?
                """,
                (utc_now(), json.dumps(metadata, sort_keys=True), candidate_id),
            )

    def _read_candidate_text(self, candidate: dict[str, Any]) -> str:
        path = Path(candidate["source_uri"])
        try:
            if path.is_file():
                return normalize_text(read_text_file(path))
        except Exception:
            return ""
        return ""

    def _claim_from_candidate(self, candidate: dict[str, Any]) -> str:
        return f"Deep Thought source '{candidate['title']}' says: {candidate['excerpt'][:500]}"

    def _attach_evidence_id(self, to_type: str, to_id: int, evidence_id: int, *, relation: str) -> None:
        if to_type == "assumption":
            table, field = "founder_assumptions", "evidence_ids_json"
        elif to_type == "goal":
            table, field = "founder_goals", "evidence_ids_json"
        elif to_type in {"bet", "strategy_object"}:
            table, field = "strategy_objects", "evidence_ids_json"
        elif to_type == "prediction":
            self._attach_prediction_evidence(to_id, evidence_id, relation=relation)
            return
        else:
            return
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT {field} FROM {table} WHERE id = ?", (to_id,)).fetchone()
            if not row:
                raise ValueError(f"Target not found: {to_type}:{to_id}")
            evidence_ids = json.loads(row[field] or "[]")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            conn.execute(
                f"UPDATE {table} SET updated_at = ?, {field} = ? WHERE id = ?",
                (utc_now(), json.dumps(evidence_ids, sort_keys=True), to_id),
            )

    def _attach_prediction_evidence(self, prediction_id: int, evidence_id: int, *, relation: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT evidence_json FROM founder_predictions WHERE id = ?", (prediction_id,)).fetchone()
            if not row:
                raise ValueError(f"Target not found: prediction:{prediction_id}")
            evidence = json.loads(row["evidence_json"] or "[]")
            if not any(item.get("evidence_id") == evidence_id for item in evidence if isinstance(item, dict)):
                evidence.append({"evidence_id": evidence_id, "relation": relation, "source": "teach.deepthought"})
            conn.execute(
                "UPDATE founder_predictions SET updated_at = ?, evidence_json = ? WHERE id = ?",
                (utc_now(), json.dumps(evidence, sort_keys=True), prediction_id),
            )

    def _find_link(self, evidence_id: int, *, to_type: str, to_id: int, relation: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM founder_links
                WHERE from_type = 'evidence'
                  AND from_id = ?
                  AND relation = ?
                  AND to_type = ?
                  AND to_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (evidence_id, relation, to_type, to_id),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json", None) or "{}")
        return data

    def _link_exists(self, evidence_id: int, *, to_type: str, to_id: int, relation: str) -> bool:
        return self._find_link(evidence_id, to_type=to_type, to_id=to_id, relation=relation) is not None

    def _resolve_weak_evidence_warnings(self) -> list[dict[str, Any]]:
        resolved = []
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT w.id, w.subject_id, g.evidence_ids_json
                FROM integrity_warnings w
                JOIN founder_goals g ON g.id = w.subject_id
                WHERE w.warning_type = 'weak_evidence'
                  AND w.subject_type = 'goal'
                  AND w.status = 'open'
                """
            ).fetchall()
            for row in rows:
                evidence_ids = json.loads(row["evidence_ids_json"] or "[]")
                # The warning says the goal's evidence is "anecdotal or absent".
                # Resolve it ONLY when at least one attached evidence is genuinely
                # non-anecdotal. Mere presence is not enough: a D-grade (or thin-
                # strength) auto-import — whose grade came from a spoofable file
                # path — must not silently clear the exact signal meant to flag
                # thin evidence. Uses the same credibility bar as _conflict_severity.
                credible = self._credible_evidence_ids(conn, evidence_ids)
                if not credible:
                    continue
                conn.execute("UPDATE integrity_warnings SET status = 'resolved' WHERE id = ?", (row["id"],))
                resolved.append(
                    {
                        "warning_id": int(row["id"]),
                        "goal_id": int(row["subject_id"]),
                        "evidence_ids": evidence_ids,
                        "credible_evidence_ids": credible,
                    }
                )
        return resolved

    def _credible_evidence_ids(self, conn: Any, evidence_ids: list[int]) -> list[int]:
        """Subset of ``evidence_ids`` whose evidence clears the 'anecdotal' bar
        (reliability A/B/C and strength >= 50), mirroring founder._conflict_severity."""
        if not evidence_ids:
            return []
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = conn.execute(
            f"SELECT id, reliability, strength FROM founder_evidence WHERE id IN ({placeholders})",
            [int(evidence_id) for evidence_id in evidence_ids],
        ).fetchall()
        return [int(row["id"]) for row in rows if _is_credible_evidence(row["reliability"], row["strength"])]

    def _pending_import_approval(self) -> Any:
        """The open (pending) founder-approval request gating Deep Thought imports,
        if one exists. There is at most one; the gate refreshes it in place."""
        for request in self.db.list_approval_requests(status="pending", limit=500):
            if request.source_type == "teaching_import":
                return request
        return None

    def _request_import_approval(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """File (or refresh) the pending founder-approval request that gates
        importing these Deep Thought candidates. Imports nothing."""
        candidate_ids = [int(candidate["id"]) for candidate in candidates]
        titles = [str(candidate.get("title") or candidate.get("source_uri") or candidate["id"]) for candidate in candidates]
        detail = "Deep Thought sources awaiting founder review before import as evidence:\n" + "\n".join(
            f"- {title}" for title in titles
        )
        metadata = {"candidate_ids": candidate_ids, "titles": titles, "source": "teach.deepthought"}
        existing = self._pending_import_approval()
        if existing is not None:
            self.db.update_approval_request(int(existing.id), detail=detail, metadata=metadata)
            request_id, created = int(existing.id), False
        else:
            request, created = self.db.ensure_approval_request(
                source_type="teaching_import",
                source_id=None,
                title=f"Review {len(candidate_ids)} Deep Thought source(s) before import",
                detail=detail,
                action="teach.deepthought.import",
                target="teaching_candidates",
                permission_tier="L1_MEMORY_WRITE",
                authority_decision="approval_required",
                authority={"reason": "External Deep Thought material must be founder-approved before entering the belief graph."},
                requested_by="teaching.deepthought",
                metadata=metadata,
            )
            request_id = int(request.id)
        self.db.audit(
            actor="teaching.deepthought",
            action="teach.deepthought.import_gated",
            target="teaching_candidates",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"approval_request_id": request_id, "candidate_ids": candidate_ids, "created": created},
        )
        return {"approval_request_id": request_id, "candidate_ids": candidate_ids, "titles": titles, "created": created}

    def _resolve_import_approvals(self) -> None:
        """Close any pending import-gate request whose candidates are now all
        imported — an explicit import is the founder's approval."""
        pending = [
            request
            for request in self.db.list_approval_requests(status="pending", limit=500)
            if request.source_type == "teaching_import"
        ]
        if not pending:
            return
        to_resolve: list[int] = []
        with self.db.connect() as conn:
            for request in pending:
                ids = [int(cid) for cid in (request.metadata or {}).get("candidate_ids", [])]
                if not ids:
                    continue
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT status FROM teaching_candidates WHERE id IN ({placeholders})", ids
                ).fetchall()
                if len(rows) == len(ids) and all(row["status"] == "imported" for row in rows):
                    to_resolve.append(int(request.id))
        for request_id in to_resolve:
            self.db.resolve_approval_request(
                request_id,
                status="approved",
                resolved_by="founder",
                resolution_note="Approved by explicit founder import.",
            )

    def _reconcile_declined_candidates(self) -> list[int]:
        """Make a founder denial stick. Candidates in a DENIED import-gate request
        are marked 'declined' so the autonomous gate never re-surfaces them (the
        auto path selects status='candidate' only). Idempotent — only flips rows
        still in 'candidate', so it never un-imports or re-declines. An explicit
        import by id still overrides, since that path has no status filter."""
        denied_ids: set[int] = set()
        for request in self.db.list_approval_requests(status="denied", limit=500):
            if request.source_type != "teaching_import":
                continue
            for candidate_id in (request.metadata or {}).get("candidate_ids", []):
                denied_ids.add(int(candidate_id))
        if not denied_ids:
            return []
        ids = sorted(denied_ids)
        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE teaching_candidates SET updated_at = ?, status = 'declined' "
                f"WHERE id IN ({placeholders}) AND status = 'candidate'",
                [utc_now(), *ids],
            )
        return ids

    def _log_runtime_event(self, *, status: str, response: str, details: dict[str, Any]) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runtime_events (
                  created_at, event_type, status, message, response, model,
                  authority_decision, details_json
                )
                VALUES (?, 'runtime.evidence_loop', ?, 'Deep Thought teaching evidence loop', ?, 'local-runtime', 'allow', ?)
                """,
                (utc_now(), status, response, json.dumps(details, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def _iter_source_files(self, paths: list[str], *, limit: int, max_file_bytes: int) -> list[Path]:
        files = []
        seen: set[str] = set()
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_file() and _is_supported_file(path, max_file_bytes=max_file_bytes):
                resolved = str(path.resolve()).lower()
                if resolved not in seen:
                    files.append(path)
                    seen.add(resolved)
            elif path.is_dir():
                for file_path in path.rglob("*"):
                    if len(files) >= limit:
                        break
                    if any(part in SKIP_DIRS for part in file_path.parts):
                        continue
                    if _is_supported_file(file_path, max_file_bytes=max_file_bytes):
                        resolved = str(file_path.resolve()).lower()
                        if resolved not in seen:
                            files.append(file_path)
                            seen.add(resolved)
            if len(files) >= limit:
                break
        return files[:limit]

    def _default_paths(self) -> list[str]:
        paths = [
            r"C:\AI Brain\Deep Thought\architecture\deep-thought-standing-brief.md",
            r"C:\AI Brain\Deep Thought\architecture\deep-thought-cofounder.md",
            r"C:\AI Brain\Deep Thought\architecture\deep-thought-operating-guide.md",
            r"C:\AI Brain\Deep Thought\architecture\deep-thought-memory-architecture.md",
            r"C:\AI Brain\Deep Thought\context.md",
            r"C:\AI Brain\Deep Thought\core-knowledge.md",
            r"C:\AI Brain\Deep Thought\session-state.md",
            r"C:\AI Brain\Deep Thought\memory",
            r"C:\AI Brain\Deep Thought\architecture",
            r"C:\DeepThought\docs",
            r"C:\DeepThought\README.md",
            r"C:\DeepThought\HANDOFF.md",
            r"C:\DeepThought\PRD.md",
        ]
        return [path for path in paths if Path(path).exists()]


def _candidate_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["suggested_links"] = json.loads(data.pop("suggested_links_json") or "[]")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _is_supported_file(path: Path, *, max_file_bytes: int) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in SUPPORTED_TEXT_EXTENSIONS and path.stat().st_size <= max_file_bytes
    except OSError:
        return False


def _reliability_for_path(path: Path) -> str:
    """Heuristic reliability from the file path. Capped at B: a path is trivially
    spoofable (a folder named "runtime-verified" would otherwise mint grade A /
    strength 90), and the grade becomes the evidence's Bayesian weight. Grade A is
    reserved for human-reviewed evidence, never granted by a bare filename scan."""
    normalized = str(path).lower()
    name = path.name.lower()
    if "runtime" in normalized and ("verified" in normalized or "validation" in normalized):
        # Would read as A on the filename alone; capped to B pending human review.
        return "B"
    if "standing-brief" in name or "standing_brief" in name or "decision" in normalized:
        return "B"
    if "architecture" in normalized or "handoff" in name or "prd" in name:
        return "C"
    return "D"


def _candidate_type(path: Path, text: str) -> str:
    lowered = f"{path.name}\n{text[:1000]}".lower()
    if "decision" in lowered:
        return "decision_record"
    if "prediction" in lowered or "calibration" in lowered:
        return "prediction_context"
    if "standing brief" in lowered or "standing-brief" in lowered:
        return "standing_brief"
    if "architecture" in lowered:
        return "architecture_note"
    return "document"


def _best_excerpt(text: str, limit: int = 1200) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return text[:limit]
    important = [part for part in paragraphs if re.search(r"co[- ]founder|zade|deep thought|evidence|assumption|prediction|goal|bet|decision", part, re.I)]
    selected = important[:3] or paragraphs[:3]
    return "\n\n".join(selected)[:limit]


def _overlap_score(haystack: str, phrase: str) -> int:
    tokens = {token for token in re.findall(r"[a-z0-9]+", phrase.lower()) if len(token) >= 4}
    return sum(1 for token in tokens if token in haystack)


def _strength_for_reliability(reliability: str) -> int:
    return {"A": 90, "B": 75, "C": 60, "D": 40, "F": 0}.get(reliability.upper(), 40)


def _is_credible_evidence(reliability: Any, strength: Any) -> bool:
    """Whether one evidence object clears the 'anecdotal' bar. Mirrors
    founder._conflict_severity's non-green tier: at least moderate grade (A/B/C)
    AND strength >= 50. A D-grade, or a thin-strength item, stays anecdotal."""
    grade = str(reliability or "D").upper()
    try:
        value = int(strength if strength is not None else 50)
    except (TypeError, ValueError):
        value = 50
    return grade in {"A", "B", "C"} and value >= 50


def _normalize_target_type(to_type: str) -> str:
    normalized = to_type.strip().lower().replace("-", "_")
    if normalized in {"active_bet", "bet"}:
        return "bet"
    if normalized in {"strategy", "strategy_object"}:
        return "strategy_object"
    if normalized in {"goal", "assumption", "prediction"}:
        return normalized
    return normalized


def _next_evidence_needed(goals: list[dict[str, Any]], bets: list[dict[str, Any]], gaps: list[Any]) -> str:
    if goals:
        return f"Attach sourced evidence to goal: {goals[0]['name']}"
    if bets:
        return f"Attach sourced evidence to bet: {bets[0]['title']}"
    if gaps:
        return f"Resolve knowledge gap: {gaps[0]}"
    return "No immediate evidence gap found."
