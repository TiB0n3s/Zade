from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .founder import FounderService, InsertResult
from .ingestion import IngestionService, SUPPORTED_TEXT_EXTENSIONS


REVIEW_DECISIONS = {"continue", "revise", "kill", "escalate"}
ACTIVE_STATUSES = {"active", "running", "revised"}
DEFAULT_EXPERIMENT_TITLE = "EXP-001 - Founder Evidence Intake"


class ExperimentService:
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

    def create_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_experiments (
                  created_at, updated_at, title, experiment_type, hypothesis, target_persona,
                  owner, status, start_date, end_date, success_metric, success_threshold,
                  minimum_evidence, decision_rule, linked_assumption_ids_json,
                  linked_bet_ids_json, linked_goal_ids_json, linked_prediction_ids_json,
                  evidence_ids_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["title"],
                    payload.get("experiment_type", "validation"),
                    payload.get("hypothesis", ""),
                    payload.get("target_persona", ""),
                    payload.get("owner", ""),
                    payload.get("status", "active"),
                    payload.get("start_date") or now[:10],
                    payload.get("end_date"),
                    payload.get("success_metric", ""),
                    payload.get("success_threshold", ""),
                    payload.get("minimum_evidence", 1),
                    payload.get("decision_rule", ""),
                    _json(payload.get("linked_assumption_ids", [])),
                    _json(payload.get("linked_bet_ids", [])),
                    _json(payload.get("linked_goal_ids", [])),
                    _json(payload.get("linked_prediction_ids", [])),
                    _json(payload.get("evidence_ids", [])),
                    _json(payload.get("metadata", {})),
                ),
            )
            experiment_id = int(cur.lastrowid)
        experiment = self.get_experiment(experiment_id)
        self._link_experiment_targets(experiment)
        self.db.audit(
            actor="experiments",
            action="experiments.create",
            target=f"experiment:{experiment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"title": experiment["title"], "experiment_type": experiment["experiment_type"]},
        )
        self.founder.create_reflection(
            {
                "event": f"Experiment created: {experiment['title']}",
                "expected": experiment.get("hypothesis", ""),
                "changed": "A proof loop entered the founder operating layer.",
                "belief_update": "Treat this experiment as the next evidence source for linked assumptions and bets.",
                "metadata": {"artifact": "founder_experiment", "id": experiment_id},
            }
        )
        return experiment

    def ensure_default_experiment(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM founder_experiments WHERE title = ? ORDER BY id ASC LIMIT 1",
                (DEFAULT_EXPERIMENT_TITLE,),
            ).fetchone()
        if row:
            return self.get_experiment(int(row["id"]))
        return self.create_experiment(
            {
                "title": DEFAULT_EXPERIMENT_TITLE,
                "experiment_type": "evidence_intake",
                "hypothesis": "Fast, local evidence capture will improve founder decisions before integrations exist.",
                "target_persona": "founder-operator",
                "owner": "Zade",
                "status": "active",
                "success_metric": "useful evidence items logged into the local founder ledger",
                "success_threshold": "10 high-signal evidence items",
                "minimum_evidence": 10,
                "decision_rule": "Continue while evidence capture lowers ambiguity in active assumptions, bets, and goals.",
                "metadata": {
                    "seed_key": "EXP-001",
                    "seeded_by": "experiments.dashboard",
                    "purpose": "Default UI evidence intake loop.",
                },
            }
        )

    def list_experiments(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM founder_experiments WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM founder_experiments ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_experiment_row_to_dict(row) for row in rows]

    def get_experiment(self, experiment_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM founder_experiments WHERE id = ?", (experiment_id,)).fetchone()
        if not row:
            raise ValueError(f"Experiment not found: {experiment_id}")
        experiment = _experiment_row_to_dict(row)
        experiment["reviews"] = self.list_reviews(experiment_id=experiment_id, limit=10)
        return experiment

    def add_evidence(self, experiment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        document_id = None
        ingestion_result = None
        source = payload.get("source") or payload.get("file_path") or f"experiment:{experiment_id}"
        text = _evidence_text(experiment, payload)
        artifact_note = ""
        if payload.get("ingest_document", True):
            file_path = payload.get("file_path")
            if file_path:
                path = Path(file_path)
                if path.suffix.lower() in SUPPORTED_TEXT_EXTENSIONS:
                    ingestion_result = self.ingestion.ingest_file(
                        path=path,
                        metadata=self._evidence_metadata(experiment, payload, document_id=None),
                    )
                    if ingestion_result.status == "error":
                        raise ValueError(ingestion_result.error)
                    document_id = ingestion_result.document_id
                else:
                    artifact_note = f"Non-text artifact recorded but not semantically ingested: {path.suffix}"
            elif text:
                ingestion_result = self.ingestion.ingest_text(
                    title=payload.get("title") or f"Experiment evidence: {experiment['title']}",
                    text=text,
                    source=f"experiment:{experiment_id}:{source}",
                    metadata=self._evidence_metadata(experiment, payload, document_id=None),
                )
                if ingestion_result.status == "error":
                    raise ValueError(ingestion_result.error)
                document_id = ingestion_result.document_id

        evidence_payload = {
            "evidence_type": payload.get("evidence_type", "experiment_observation"),
            "source": source,
            "evidence_date": payload.get("evidence_date"),
            "reliability": payload.get("reliability", "C"),
            "claim_supported": payload.get("claim_supported") or _default_supported_claim(experiment, payload),
            "claim_contradicted": payload.get("claim_contradicted", ""),
            "strength": payload.get("strength", 50),
            "linked_assumption_id": payload.get("linked_assumption_id"),
            "linked_decision_id": payload.get("linked_decision_id"),
            "notes": "\n".join(item for item in [payload.get("notes", ""), artifact_note] if item).strip(),
            "metadata": self._evidence_metadata(experiment, payload, document_id=document_id)
            | {
                "ingestion": _ingestion_dict(ingestion_result),
                "artifact_note": artifact_note,
            },
        }
        evidence = self.founder.create_evidence(evidence_payload).record
        self._attach_evidence_to_experiment(experiment_id, evidence["id"])
        links = self._link_evidence_to_targets(
            experiment=self.get_experiment(experiment_id),
            evidence=evidence,
            payload=payload,
        )
        self.db.audit(
            actor="experiments",
            action="experiments.evidence.add",
            target=f"experiment:{experiment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"evidence_id": evidence["id"], "links": len(links), "document_id": document_id},
        )
        return {
            "experiment": self.get_experiment(experiment_id),
            "evidence": evidence,
            "document_id": document_id,
            "links": links,
            "ingestion": _ingestion_dict(ingestion_result),
        }

    def list_reviews(self, *, experiment_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if experiment_id:
                rows = conn.execute(
                    "SELECT * FROM experiment_reviews WHERE experiment_id = ? ORDER BY id DESC LIMIT ?",
                    (experiment_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM experiment_reviews ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_review_row_to_dict(row) for row in rows]

    def review_experiment(self, experiment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        decision = str(payload.get("decision", "continue")).strip().lower()
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"Decision must be one of: {', '.join(sorted(REVIEW_DECISIONS))}")
        status_after = payload.get("status_after") or _status_after_decision(decision, experiment["status"])
        evidence_ids = payload.get("evidence_ids") or experiment.get("evidence_ids", [])
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO experiment_reviews (
                  created_at, experiment_id, review_type, period, decision, outcome_summary,
                  findings_json, next_actions_json, evidence_ids_json, confidence_delta,
                  status_after, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    experiment_id,
                    payload.get("review_type", "weekly"),
                    payload.get("period") or utc_now()[:10],
                    decision,
                    payload.get("outcome_summary", ""),
                    _json(payload.get("findings", {})),
                    _json(payload.get("next_actions", [])),
                    _json(evidence_ids),
                    payload.get("confidence_delta", 0),
                    status_after,
                    _json(payload.get("metadata", {})),
                ),
            )
            review_id = int(cur.lastrowid)
            conn.execute(
                """
                UPDATE founder_experiments
                SET updated_at = ?, status = ?, result = ?, recommendation = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status_after,
                    payload.get("outcome_summary", experiment.get("result", "")),
                    decision,
                    experiment_id,
                ),
            )
        review = self.get_review(review_id)
        self.db.audit(
            actor="experiments",
            action="experiments.review",
            target=f"experiment:{experiment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"decision": decision, "status_after": status_after, "review_id": review_id},
        )
        self.founder.create_reflection(
            {
                "event": f"Experiment reviewed: {experiment['title']}",
                "expected": experiment.get("hypothesis", ""),
                "changed": payload.get("outcome_summary", ""),
                "belief_update": f"Experiment decision: {decision}.",
                "strategy_update": "; ".join(payload.get("next_actions", [])[:3]),
                "metadata": {"artifact": "experiment_review", "experiment_id": experiment_id, "review_id": review_id},
            }
        )
        return {"experiment": self.get_experiment(experiment_id), "review": review}

    def get_review(self, review_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM experiment_reviews WHERE id = ?", (review_id,)).fetchone()
        if not row:
            raise ValueError(f"Experiment review not found: {review_id}")
        return _review_row_to_dict(row)

    def pushback(self, experiment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        review = self.founder.create_contrarian_review(
            {
                "subject_type": "experiment",
                "subject_id": experiment_id,
                "title": payload.get("title") or f"Zade disagrees: {experiment['title']}",
                "context": payload.get("objection", ""),
                "top_risks": [item for item in [payload.get("risk", "")] if item],
                "blind_spots": payload.get("blind_spots", []),
                "confidence_adjustment": payload.get("confidence_adjustment", -10),
                "recommendation": payload.get("recommendation", "proceed_with_changes"),
                "metadata": {
                    "experiment_id": experiment_id,
                    "severity": payload.get("severity", "yellow"),
                    "non_blocking": True,
                    **payload.get("metadata", {}),
                },
            }
        ).record
        link = self.founder.create_link(
            {
                "from_type": "contrarian_review",
                "from_id": review["id"],
                "relation": "pushes_back_on",
                "to_type": "experiment",
                "to_id": experiment_id,
                "strength": payload.get("strength", 70),
                "metadata": {"non_blocking": True, "source": "experiments.pushback"},
            }
        ).record
        self.db.audit(
            actor="experiments",
            action="experiments.pushback",
            target=f"experiment:{experiment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"contrarian_review_id": review["id"], "non_blocking": True},
        )
        return {
            "experiment": experiment,
            "pushback": review,
            "link": link,
            "non_blocking": True,
        }

    def dashboard(self) -> dict[str, Any]:
        if not self.list_experiments(limit=1):
            self.ensure_default_experiment()
        active = [item for item in self.list_experiments(limit=100) if item["status"] in ACTIVE_STATUSES]
        reviews = self.list_reviews(limit=20)
        needs_evidence = [item for item in active if len(item.get("evidence_ids", [])) < int(item.get("minimum_evidence") or 1)]
        needs_decision = [item for item in self.list_experiments(status="needs_decision", limit=50)]
        return {
            "active_count": len(active),
            "needs_evidence_count": len(needs_evidence),
            "needs_decision_count": len(needs_decision),
            "active": active[:10],
            "needs_evidence": needs_evidence[:10],
            "needs_decision": needs_decision[:10],
            "recent_reviews": reviews[:10],
            "next_action": _next_experiment_action(needs_evidence, needs_decision, active),
        }

    def run_loop(self, *, review_type: str = "weekly", period: str | None = None, max_reviews: int = 10) -> dict[str, Any]:
        active = [item for item in self.list_experiments(limit=100) if item["status"] in ACTIVE_STATUSES]
        reviews = []
        today = utc_now()[:10]
        for experiment in active[:max_reviews]:
            evidence_count = len(experiment.get("evidence_ids", []))
            minimum = max(1, int(experiment.get("minimum_evidence") or 1))
            evidence_needed = max(0, minimum - evidence_count)
            due = bool(experiment.get("end_date") and experiment["end_date"] < today)
            if due and evidence_count >= minimum:
                decision = "escalate"
                status_after = "needs_decision"
                next_actions = ["Make continue/revise/kill decision from collected evidence."]
            elif due and evidence_needed:
                decision = "escalate"
                status_after = "needs_decision"
                next_actions = [f"Evidence short by {evidence_needed}; founder decision required before extending."]
            elif evidence_needed:
                decision = "continue"
                status_after = experiment["status"]
                next_actions = [f"Collect {evidence_needed} more evidence item(s)."]
            else:
                decision = "continue"
                status_after = experiment["status"]
                next_actions = ["Review metric movement against the decision rule."]
            result = self.review_experiment(
                experiment["id"],
                {
                    "review_type": review_type,
                    "period": period or today,
                    "decision": decision,
                    "status_after": status_after,
                    "outcome_summary": _loop_summary(experiment, evidence_count, minimum, due),
                    "findings": {
                        "evidence_count": evidence_count,
                        "minimum_evidence": minimum,
                        "evidence_needed": evidence_needed,
                        "due": due,
                        "success_metric": experiment.get("success_metric", ""),
                        "success_threshold": experiment.get("success_threshold", ""),
                    },
                    "next_actions": next_actions,
                    "evidence_ids": experiment.get("evidence_ids", []),
                    "metadata": {"generated_by": "runtime.experiment_loop"},
                },
            )
            reviews.append(result["review"])
        dashboard = self.dashboard()
        event_id = self._log_runtime_event(
            details={"reviews": reviews, "dashboard": dashboard},
            response=dashboard["next_action"],
        )
        return {
            "event_id": event_id,
            "reviews": reviews,
            "dashboard": dashboard,
            "next_action": dashboard["next_action"],
        }

    def _link_experiment_targets(self, experiment: dict[str, Any]) -> None:
        for target in _experiment_targets(experiment):
            self.founder.create_link(
                {
                    "from_type": "experiment",
                    "from_id": experiment["id"],
                    "relation": "tests",
                    "to_type": target["to_type"],
                    "to_id": target["to_id"],
                    "strength": 60,
                    "metadata": {"source": "experiments.create"},
                }
            )

    def _attach_evidence_to_experiment(self, experiment_id: int, evidence_id: int) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT evidence_ids_json FROM founder_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Experiment not found: {experiment_id}")
            evidence_ids = json.loads(row["evidence_ids_json"] or "[]")
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            conn.execute(
                "UPDATE founder_experiments SET updated_at = ?, evidence_ids_json = ? WHERE id = ?",
                (utc_now(), _json(evidence_ids), experiment_id),
            )

    def _link_evidence_to_targets(
        self,
        *,
        experiment: dict[str, Any],
        evidence: dict[str, Any],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        evidence_id = int(evidence["id"])
        targets = list(_experiment_targets(experiment))
        targets.extend(payload.get("link_targets", []))
        seen = set()
        # The evidence's own named assumption was already Bayesian-updated inside
        # create_evidence; don't move it a second time if it is also a target.
        scored: set[tuple[str, int]] = set()
        own_assumption = evidence.get("linked_assumption_id")
        if own_assumption is not None:
            scored.add(("assumption", int(own_assumption)))
        links = []
        for target in targets:
            to_type = _normalize_target_type(str(target["to_type"]))
            to_id = int(target["to_id"])
            key = (to_type, to_id)
            if key in seen:
                continue
            seen.add(key)
            self._attach_evidence_id(to_type, to_id, evidence_id, relation=target.get("relation", "informs"))
            if key not in scored:
                # Every belief this evidence bears on moves, not just assumptions.
                self.founder.apply_evidence_confidence_to(to_type, to_id, evidence)
                scored.add(key)
            link = self.founder.create_link(
                {
                    "from_type": "evidence",
                    "from_id": evidence_id,
                    "relation": target.get("relation", "informs"),
                    "to_type": to_type,
                    "to_id": to_id,
                    "strength": int(target.get("strength", payload.get("strength", 50))),
                    "metadata": {
                        "source": "experiments.evidence",
                        "experiment_id": experiment["id"],
                        **target.get("metadata", {}),
                    },
                }
            ).record
            links.append(link)
        return links

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
                (utc_now(), _json(evidence_ids), to_id),
            )

    def _attach_prediction_evidence(self, prediction_id: int, evidence_id: int, *, relation: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT evidence_json FROM founder_predictions WHERE id = ?", (prediction_id,)).fetchone()
            if not row:
                raise ValueError(f"Target not found: prediction:{prediction_id}")
            evidence = json.loads(row["evidence_json"] or "[]")
            if not any(item.get("evidence_id") == evidence_id for item in evidence if isinstance(item, dict)):
                evidence.append({"evidence_id": evidence_id, "relation": relation, "source": "experiments.evidence"})
            conn.execute(
                "UPDATE founder_predictions SET updated_at = ?, evidence_json = ? WHERE id = ?",
                (utc_now(), _json(evidence), prediction_id),
            )

    def _evidence_metadata(
        self,
        experiment: dict[str, Any],
        payload: dict[str, Any],
        *,
        document_id: int | None,
    ) -> dict[str, Any]:
        return {
            "experiment_id": experiment["id"],
            "experiment_title": experiment["title"],
            "experiment_type": experiment["experiment_type"],
            "metrics": payload.get("metrics", {}),
            "document_id": document_id,
            "file_path": payload.get("file_path"),
            "evidence_loop": "experiment",
        }

    def _log_runtime_event(self, *, details: dict[str, Any], response: str) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runtime_events (
                  created_at, event_type, status, message, response, model,
                  authority_decision, details_json
                )
                VALUES (?, 'runtime.experiment_loop', 'ok', 'Experiment evidence loop', ?, 'local-runtime', 'allow', ?)
                """,
                (utc_now(), response, _json(details)),
            )
            return int(cur.lastrowid)


def _experiment_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for source, target in {
        "linked_assumption_ids_json": "linked_assumption_ids",
        "linked_bet_ids_json": "linked_bet_ids",
        "linked_goal_ids_json": "linked_goal_ids",
        "linked_prediction_ids_json": "linked_prediction_ids",
        "evidence_ids_json": "evidence_ids",
        "metadata_json": "metadata",
    }.items():
        data[target] = json.loads(data.pop(source) or "[]")
    return data


def _review_row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for source, target in {
        "findings_json": "findings",
        "next_actions_json": "next_actions",
        "evidence_ids_json": "evidence_ids",
        "metadata_json": "metadata",
    }.items():
        fallback = "{}" if source in {"findings_json", "metadata_json"} else "[]"
        data[target] = json.loads(data.pop(source) or fallback)
    return data


def _experiment_targets(experiment: dict[str, Any]) -> list[dict[str, Any]]:
    targets = []
    for assumption_id in experiment.get("linked_assumption_ids", []):
        targets.append({"to_type": "assumption", "to_id": assumption_id, "relation": "tests"})
    for bet_id in experiment.get("linked_bet_ids", []):
        targets.append({"to_type": "bet", "to_id": bet_id, "relation": "tests"})
    for goal_id in experiment.get("linked_goal_ids", []):
        targets.append({"to_type": "goal", "to_id": goal_id, "relation": "supports"})
    for prediction_id in experiment.get("linked_prediction_ids", []):
        targets.append({"to_type": "prediction", "to_id": prediction_id, "relation": "tests"})
    return targets


def _evidence_text(experiment: dict[str, Any], payload: dict[str, Any]) -> str:
    metrics = payload.get("metrics", {})
    metric_lines = [f"- {key}: {value}" for key, value in metrics.items()]
    parts = [
        f"Experiment: {experiment['title']}",
        f"Hypothesis: {experiment.get('hypothesis', '')}",
        f"Evidence type: {payload.get('evidence_type', 'experiment_observation')}",
        payload.get("content", ""),
        "Metrics:\n" + "\n".join(metric_lines) if metric_lines else "",
        payload.get("notes", ""),
    ]
    return "\n\n".join(part for part in parts if str(part).strip())


def _default_supported_claim(experiment: dict[str, Any], payload: dict[str, Any]) -> str:
    if payload.get("metrics"):
        return f"Experiment '{experiment['title']}' collected metric evidence for {experiment.get('success_metric') or 'its success metric'}."
    if payload.get("content") or payload.get("file_path"):
        return f"Experiment '{experiment['title']}' collected observational evidence."
    return f"Experiment '{experiment['title']}' has a new evidence item."


def _status_after_decision(decision: str, current_status: str) -> str:
    if decision == "kill":
        return "killed"
    if decision == "escalate":
        return "needs_decision"
    if decision == "revise":
        return "revised"
    return current_status


def _loop_summary(experiment: dict[str, Any], evidence_count: int, minimum: int, due: bool) -> str:
    due_text = "deadline has passed" if due else "deadline is still open"
    decision_rule = str(experiment.get("decision_rule") or "not specified").rstrip(".")
    return (
        f"{experiment['title']} has {evidence_count}/{minimum} required evidence item(s); "
        f"{due_text}; decision rule: {decision_rule}."
    )


def _next_experiment_action(
    needs_evidence: list[dict[str, Any]],
    needs_decision: list[dict[str, Any]],
    active: list[dict[str, Any]],
) -> str:
    if needs_decision:
        return f"Decide experiment outcome: {needs_decision[0]['title']}"
    if needs_evidence:
        return f"Collect evidence for experiment: {needs_evidence[0]['title']}"
    if active:
        return f"Review experiment metric: {active[0]['title']}"
    return "Create the next experiment for the highest-risk assumption."


def _normalize_target_type(to_type: str) -> str:
    normalized = to_type.strip().lower().replace("-", "_")
    if normalized in {"active_bet", "bet"}:
        return "bet"
    if normalized in {"strategy", "strategy_object"}:
        return "strategy_object"
    if normalized in {"goal", "assumption", "prediction"}:
        return normalized
    return normalized


def _ingestion_dict(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "job_id": result.job_id,
        "status": result.status,
        "document_id": result.document_id,
        "documents_count": result.documents_count,
        "chunks_count": result.chunks_count,
        "skipped": result.skipped,
        "error": result.error,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
