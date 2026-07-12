from __future__ import annotations

import json
from dataclasses import dataclass
from statistics import mean
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now


MENTAL_MODELS = [
    {
        "name": "Second-order thinking",
        "use_when": "A decision has downstream effects beyond the immediate result.",
        "prompt": "What happens after the obvious first consequence?",
    },
    {
        "name": "Expected value",
        "use_when": "Options have different probabilities, upside, and downside.",
        "prompt": "What is the probability-weighted value of each path?",
    },
    {
        "name": "Inversion",
        "use_when": "A goal is important enough that failure prevention matters.",
        "prompt": "How would we make this fail, and how do we prevent that?",
    },
    {
        "name": "OODA loop",
        "use_when": "The environment is changing quickly.",
        "prompt": "What must we observe, orient around, decide, and act on next?",
    },
    {
        "name": "First principles",
        "use_when": "Inherited assumptions may be hiding a better path.",
        "prompt": "What must be true independent of convention?",
    },
    {
        "name": "Chesterton's Fence",
        "use_when": "Removing a process, constraint, or legacy choice.",
        "prompt": "Why did this exist before we remove it?",
    },
    {
        "name": "Power law analysis",
        "use_when": "A few inputs may dominate outcomes.",
        "prompt": "Which one or two factors create most of the result?",
    },
    {
        "name": "Bayesian reasoning",
        "use_when": "New evidence should update confidence.",
        "prompt": "What prior are we starting from, and how much should this evidence move it?",
    },
]


DEFAULT_CONTRARIAN_ROLES = {
    "red_team": "What assumptions break this?",
    "skeptic": "What evidence is missing?",
    "historian": "When have we failed similarly?",
    "economist": "What is the opportunity cost?",
    "customer": "Would anyone actually care?",
    "engineer": "What technical debt are we creating?",
    "investor": "Would I fund this?",
    "competitor": "How would someone destroy this idea?",
    "future_founder": "Six months from now, what will I regret?",
}


@dataclass(frozen=True)
class InsertResult:
    id: int
    record: dict[str, Any]


class FounderService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase):
        self.config = config
        self.db = db

    def mental_models(self) -> list[dict[str, str]]:
        return MENTAL_MODELS

    def upsert_identity_charter(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM identity_charter WHERE id = 1").fetchone()
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO identity_charter (
                  id, created_at, updated_at, name, source, mission, guiding_principles_json,
                  cognitive_style_json, communication_style_json, leadership_philosophy_json,
                  emotional_framework_json, strengths_json, risk_controls_json,
                  decision_framework_json, personal_standards_json, safety_translation_json,
                  status, metadata_json
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    now,
                    payload.get("name", self.config.identity.name),
                    payload.get("source", "local"),
                    payload.get("mission", ""),
                    _json(payload.get("guiding_principles", [])),
                    _json(payload.get("cognitive_style", [])),
                    _json(payload.get("communication_style", [])),
                    _json(payload.get("leadership_philosophy", [])),
                    _json(payload.get("emotional_framework", {})),
                    _json(payload.get("strengths", [])),
                    _json(payload.get("risk_controls", [])),
                    _json(payload.get("decision_framework", [])),
                    _json(payload.get("personal_standards", [])),
                    _json(payload.get("safety_translation", {})),
                    payload.get("status", "active"),
                    _json(payload.get("metadata", {})),
                ),
            )
        record = self.get_identity_charter()
        self._audit("identity.charter.upsert", "identity_charter", "ok", {"status": record.get("status")})
        self._insert_reflection(
            event="Runtime identity charter updated",
            changed="Zade's operating posture changed.",
            belief_update="Apply the charter as behavioral guidance, bounded by authority policy and safety controls.",
            metadata={"artifact": "identity_charter", "source": record.get("source", "")},
        )
        return record

    def get_identity_charter(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM identity_charter WHERE id = 1").fetchone()
        if not row:
            return {}
        return _row_to_dict(row, IDENTITY_CHARTER_JSON_FIELDS)

    def upsert_relationship_charter(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        subject_name = payload["subject_name"]
        relationship_type = payload.get("relationship_type", "protected_principal")
        values = (
            now,
            payload.get("source", "local"),
            payload.get("first_principle", ""),
            _json(payload.get("devotion", {})),
            _json(payload.get("attention_policy", {})),
            _json(payload.get("protection_policy", {})),
            _json(payload.get("loyalty_policy", {})),
            _json(payload.get("vulnerability", {})),
            _json(payload.get("trust", {})),
            _json(payload.get("internal_conflict", {})),
            _json(payload.get("expression_of_care", {})),
            _json(payload.get("risk_controls", [])),
            _json(payload.get("safety_translation", {})),
            _json(payload.get("boundaries", [])),
            payload.get("status", "active"),
            _json(payload.get("metadata", {})),
            subject_name,
            relationship_type,
        )
        with self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM relationship_charters
                WHERE subject_name = ? AND relationship_type = ?
                """,
                (subject_name, relationship_type),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE relationship_charters
                    SET updated_at = ?, source = ?, first_principle = ?, devotion_json = ?,
                        attention_policy_json = ?, protection_policy_json = ?, loyalty_policy_json = ?,
                        vulnerability_json = ?, trust_json = ?, internal_conflict_json = ?,
                        expression_of_care_json = ?, risk_controls_json = ?, safety_translation_json = ?,
                        boundaries_json = ?, status = ?, metadata_json = ?
                    WHERE subject_name = ? AND relationship_type = ?
                    """,
                    values,
                )
                item_id = int(existing["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO relationship_charters (
                      created_at, updated_at, subject_name, relationship_type, source, first_principle,
                      devotion_json, attention_policy_json, protection_policy_json, loyalty_policy_json,
                      vulnerability_json, trust_json, internal_conflict_json, expression_of_care_json,
                      risk_controls_json, safety_translation_json, boundaries_json, status, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        subject_name,
                        relationship_type,
                        payload.get("source", "local"),
                        payload.get("first_principle", ""),
                        _json(payload.get("devotion", {})),
                        _json(payload.get("attention_policy", {})),
                        _json(payload.get("protection_policy", {})),
                        _json(payload.get("loyalty_policy", {})),
                        _json(payload.get("vulnerability", {})),
                        _json(payload.get("trust", {})),
                        _json(payload.get("internal_conflict", {})),
                        _json(payload.get("expression_of_care", {})),
                        _json(payload.get("risk_controls", [])),
                        _json(payload.get("safety_translation", {})),
                        _json(payload.get("boundaries", [])),
                        payload.get("status", "active"),
                        _json(payload.get("metadata", {})),
                    ),
                )
                item_id = int(cur.lastrowid)
        record = self.get_record("relationship_charters", item_id, RELATIONSHIP_CHARTER_JSON_FIELDS)
        self._audit(
            "identity.relationship_charter.upsert",
            f"relationship_charter:{subject_name}:{relationship_type}",
            "ok",
            {"status": record.get("status")},
        )
        self._insert_reflection(
            event=f"Relationship charter updated: {subject_name}",
            changed=f"The protected-principal posture for {subject_name} changed.",
            belief_update="Apply this relationship charter only within consent, privacy, safety, and authority boundaries.",
            metadata={"artifact": "relationship_charter", "id": item_id, "subject_name": subject_name},
        )
        return record

    def get_relationship_charter(
        self,
        subject_name: str,
        relationship_type: str = "protected_principal",
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM relationship_charters
                WHERE subject_name = ? AND relationship_type = ?
                """,
                (subject_name, relationship_type),
            ).fetchone()
        if not row:
            return {}
        return _row_to_dict(row, RELATIONSHIP_CHARTER_JSON_FIELDS)

    def list_relationship_charters(self, *, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        return self._list("relationship_charters", RELATIONSHIP_CHARTER_JSON_FIELDS, status=status, limit=limit)

    def upsert_voice_charter(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM voice_charter WHERE id = 1").fetchone()
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO voice_charter (
                  id, created_at, updated_at, name, source, overall_voice, sentence_structure_json,
                  vocabulary_json, rhythm_json, confidence_style_json, humor_json, nicknames_json,
                  emotional_expression_json, threat_translation_json, question_style_json,
                  philosophy_json, internal_monologue_json, dominant_traits_json,
                  linguistic_fingerprint_json, uncertainty_policy_json, safety_controls_json,
                  status, metadata_json
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    now,
                    payload.get("name", self.config.identity.name),
                    payload.get("source", "local"),
                    payload.get("overall_voice", ""),
                    _json(payload.get("sentence_structure", {})),
                    _json(payload.get("vocabulary", {})),
                    _json(payload.get("rhythm", {})),
                    _json(payload.get("confidence_style", {})),
                    _json(payload.get("humor", {})),
                    _json(payload.get("nicknames", {})),
                    _json(payload.get("emotional_expression", {})),
                    _json(payload.get("threat_translation", {})),
                    _json(payload.get("question_style", {})),
                    _json(payload.get("philosophy", {})),
                    _json(payload.get("internal_monologue", {})),
                    _json(payload.get("dominant_traits", [])),
                    _json(payload.get("linguistic_fingerprint", {})),
                    _json(payload.get("uncertainty_policy", {})),
                    _json(payload.get("safety_controls", [])),
                    payload.get("status", "active"),
                    _json(payload.get("metadata", {})),
                ),
            )
        record = self.get_voice_charter()
        self._audit("identity.voice_charter.upsert", "voice_charter", "ok", {"status": record.get("status")})
        self._insert_reflection(
            event="Voice charter updated",
            changed="Zade's spoken operating style changed.",
            belief_update="Apply the voice charter as style guidance without overriding truthfulness, consent, or authority boundaries.",
            metadata={"artifact": "voice_charter", "source": record.get("source", "")},
        )
        return record

    def get_voice_charter(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM voice_charter WHERE id = 1").fetchone()
        if not row:
            return {}
        return _row_to_dict(row, VOICE_CHARTER_JSON_FIELDS)

    def upsert_thesis(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT created_at FROM company_thesis WHERE id = 1").fetchone()
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO company_thesis (
                  id, created_at, updated_at, vision, mission, why_now, customer,
                  unfair_advantages_json, core_assumptions_json, strategic_moats_json,
                  success_metrics_json, failure_modes_json, unknown_unknowns_json,
                  evidence_json, status
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    now,
                    payload.get("vision", ""),
                    payload.get("mission", ""),
                    payload.get("why_now", ""),
                    payload.get("customer", ""),
                    _json(payload.get("unfair_advantages", [])),
                    _json(payload.get("core_assumptions", [])),
                    _json(payload.get("strategic_moats", {})),
                    _json(payload.get("success_metrics", {})),
                    _json(payload.get("failure_modes", {})),
                    _json(payload.get("unknown_unknowns", [])),
                    _json(payload.get("evidence", [])),
                    payload.get("status", "draft"),
                ),
            )
        record = self.get_thesis()
        self._audit("founder.thesis.upsert", "company_thesis", "ok", {"status": record.get("status")})
        self._insert_reflection(
            event="Company thesis updated",
            changed="The living investment thesis changed.",
            belief_update="Review assumptions and success metrics against new evidence.",
            metadata={"artifact": "company_thesis"},
        )
        return record

    def get_thesis(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM company_thesis WHERE id = 1").fetchone()
        if not row:
            return {}
        return _row_to_dict(
            row,
            {
                "unfair_advantages_json": "unfair_advantages",
                "core_assumptions_json": "core_assumptions",
                "strategic_moats_json": "strategic_moats",
                "success_metrics_json": "success_metrics",
                "failure_modes_json": "failure_modes",
                "unknown_unknowns_json": "unknown_unknowns",
                "evidence_json": "evidence",
            },
        )

    def create_strategy_entry(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO strategy_ledger (
                  created_at, updated_at, title, category, decision, reason, expected_outcome,
                  confidence, time_horizon, dependencies_json, status, evidence_json,
                  linked_metrics_json, owner, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["title"],
                    payload.get("category", "Product"),
                    payload["decision"],
                    payload.get("reason", ""),
                    payload.get("expected_outcome", ""),
                    payload.get("confidence", 50),
                    payload.get("time_horizon", ""),
                    _json(payload.get("dependencies", [])),
                    payload.get("status", "active"),
                    _json(payload.get("evidence", [])),
                    _json(payload.get("linked_metrics", [])),
                    payload.get("owner", ""),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("strategy_ledger", item_id, STRATEGY_JSON_FIELDS)
        self._after_create("founder.strategy.create", "strategy_ledger", item_id, record["title"])
        return InsertResult(id=item_id, record=record)

    def list_strategy_entries(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("strategy_ledger", STRATEGY_JSON_FIELDS, status=status, limit=limit)

    def create_initiative(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_initiatives (
                  created_at, updated_at, objective, why_it_matters, expected_business_impact,
                  priority, owner, due_date, current_stage, dependencies_json, blockers_json,
                  success_criteria_json, evidence_json, confidence, current_risk, next_review,
                  status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["objective"],
                    payload.get("why_it_matters", ""),
                    payload.get("expected_business_impact", ""),
                    payload.get("priority", 50),
                    payload.get("owner", ""),
                    payload.get("due_date"),
                    payload.get("current_stage", "proposed"),
                    _json(payload.get("dependencies", [])),
                    _json(payload.get("blockers", [])),
                    _json(payload.get("success_criteria", [])),
                    _json(payload.get("evidence", [])),
                    payload.get("confidence", 50),
                    payload.get("current_risk", "medium"),
                    payload.get("next_review"),
                    payload.get("status", "active"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_initiatives", item_id, INITIATIVE_JSON_FIELDS)
        self._after_create("founder.initiative.create", "initiative", item_id, record["objective"])
        return InsertResult(id=item_id, record=record)

    def list_initiatives(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_initiatives", INITIATIVE_JSON_FIELDS, status=status, limit=limit)

    def create_decision_memo(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_memos (
                  created_at, updated_at, problem, context, options_json, recommendation, why,
                  confidence, expected_outcome, expected_failure_modes_json, who_disagrees,
                  counterarguments_json, decision_date, revisit_date, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["problem"],
                    payload.get("context", ""),
                    _json(payload.get("options", [])),
                    payload.get("recommendation", ""),
                    payload.get("why", ""),
                    payload.get("confidence", 50),
                    payload.get("expected_outcome", ""),
                    _json(payload.get("expected_failure_modes", [])),
                    payload.get("who_disagrees", ""),
                    _json(payload.get("counterarguments", [])),
                    payload.get("decision_date"),
                    payload.get("revisit_date"),
                    payload.get("status", "open"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("decision_memos", item_id, DECISION_JSON_FIELDS)
        self._after_create("founder.decision.create", "decision_memo", item_id, record["problem"])
        return InsertResult(id=item_id, record=record)

    def list_decision_memos(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("decision_memos", DECISION_JSON_FIELDS, status=status, limit=limit)

    def create_prediction(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_predictions (
                  created_at, updated_at, prediction, probability, time_horizon, due_at,
                  evidence_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["prediction"],
                    payload["probability"],
                    payload.get("time_horizon", ""),
                    payload.get("due_at"),
                    _json(payload.get("evidence", [])),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_predictions", item_id, PREDICTION_JSON_FIELDS)
        self._after_create("founder.prediction.create", "prediction", item_id, record["prediction"])
        return InsertResult(id=item_id, record=record)

    def list_predictions(self, *, result: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = "WHERE result = ?" if result else ""
        params: list[Any] = [result] if result else []
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM founder_predictions {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_dict(row, PREDICTION_JSON_FIELDS) for row in rows]

    def score_prediction(self, payload: dict[str, Any]) -> dict[str, Any]:
        prediction_id = int(payload["prediction_id"])
        existing = self.get_record("founder_predictions", prediction_id, PREDICTION_JSON_FIELDS)
        outcome = payload["outcome"]
        result = payload.get("result") or outcome
        calibration_error = _calibration_error(existing.get("probability"), outcome)
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE founder_predictions
                SET updated_at = ?, outcome = ?, result = ?, calibration_error = ?,
                    missed_factors = ?, lessons = ?, worldview_update = ?, scored_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    outcome,
                    result,
                    calibration_error,
                    payload.get("missed_factors", ""),
                    payload.get("lessons", ""),
                    payload.get("worldview_update", ""),
                    now,
                    prediction_id,
                ),
            )
        record = self.get_record("founder_predictions", prediction_id, PREDICTION_JSON_FIELDS)
        self._audit("founder.prediction.score", f"prediction:{prediction_id}", "ok", {"result": result})
        self._insert_reflection(
            event=f"Prediction scored: {existing['prediction']}",
            expected=existing["prediction"],
            changed=f"Outcome recorded as {outcome}.",
            belief_update=payload.get("worldview_update", ""),
            prediction_update=payload.get("lessons", ""),
            metadata={"prediction_id": prediction_id, "calibration_error": calibration_error},
        )
        return record

    def create_contrarian_review(self, payload: dict[str, Any]) -> InsertResult:
        roles = {**DEFAULT_CONTRARIAN_ROLES, **payload.get("roles", {})}
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO contrarian_reviews (
                  created_at, subject_type, subject_id, title, context, roles_json,
                  top_risks_json, blind_spots_json, confidence_adjustment,
                  recommendation, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    payload.get("subject_type", "general"),
                    payload.get("subject_id"),
                    payload["title"],
                    payload.get("context", ""),
                    _json(roles),
                    _json(payload.get("top_risks", [])),
                    _json(payload.get("blind_spots", [])),
                    payload.get("confidence_adjustment", 0),
                    payload.get("recommendation", "proceed_with_changes"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("contrarian_reviews", item_id, CONTRARIAN_JSON_FIELDS)
        self._after_create("founder.contrarian_review.create", "contrarian_review", item_id, record["title"])
        return InsertResult(id=item_id, record=record)

    def list_contrarian_reviews(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("contrarian_reviews", CONTRARIAN_JSON_FIELDS, limit=limit)

    def create_reflection(self, payload: dict[str, Any]) -> InsertResult:
        item_id = self._insert_reflection(**payload)
        record = self.get_record("founder_reflections", item_id, REFLECTION_JSON_FIELDS)
        self._audit("founder.reflection.create", f"reflection:{item_id}", "ok", {"event": record["event"]})
        return InsertResult(id=item_id, record=record)

    def list_reflections(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_reflections", REFLECTION_JSON_FIELDS, limit=limit)

    def create_assumption(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_assumptions (
                  created_at, updated_at, statement, category, confidence, status,
                  review_date, invalidation_signal, evidence_ids_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["statement"],
                    payload.get("category", "product"),
                    payload.get("confidence", 50),
                    payload.get("status", "active"),
                    payload.get("review_date"),
                    payload.get("invalidation_signal", ""),
                    _json(payload.get("evidence_ids", [])),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_assumptions", item_id, ASSUMPTION_JSON_FIELDS)
        self._after_create("founder.assumption.create", "assumption", item_id, record["statement"])
        return InsertResult(id=item_id, record=record)

    def list_assumptions(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_assumptions", ASSUMPTION_JSON_FIELDS, status=status, limit=limit)

    def create_evidence(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_evidence (
                  created_at, evidence_type, source, evidence_date, reliability,
                  claim_supported, claim_contradicted, strength, linked_assumption_id,
                  linked_decision_id, notes, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    payload.get("evidence_type", "founder observation"),
                    payload["source"],
                    payload.get("evidence_date"),
                    payload.get("reliability", "D"),
                    payload.get("claim_supported", ""),
                    payload.get("claim_contradicted", ""),
                    payload.get("strength", 50),
                    payload.get("linked_assumption_id"),
                    payload.get("linked_decision_id"),
                    payload.get("notes", ""),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_evidence", item_id, EVIDENCE_JSON_FIELDS)
        self._audit("founder.evidence.create", f"evidence:{item_id}", "ok", {"reliability": record["reliability"]})
        if record.get("linked_assumption_id"):
            self._link_evidence_to_assumption(record)
            self._apply_evidence_confidence(record)
        if record.get("claim_contradicted"):
            self.detect_thesis_conflict({"evidence_id": item_id})
        return InsertResult(id=item_id, record=record)

    def list_evidence(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_evidence", EVIDENCE_JSON_FIELDS, limit=limit)

    def create_link(self, payload: dict[str, Any]) -> InsertResult:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_links (
                  created_at, from_type, from_id, relation, to_type, to_id, strength, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    payload["from_type"],
                    payload["from_id"],
                    payload.get("relation", "related_to"),
                    payload["to_type"],
                    payload["to_id"],
                    payload.get("strength", 50),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_links", item_id, LINK_JSON_FIELDS)
        self._audit("founder.link.create", f"link:{item_id}", "ok", {"relation": record["relation"]})
        return InsertResult(id=item_id, record=record)

    def list_links(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_links", LINK_JSON_FIELDS, limit=limit)

    def create_strategy_object(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO strategy_objects (
                  created_at, updated_at, object_type, title, owner, deadline, confidence,
                  status, reversal_trigger, details_json, evidence_ids_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["object_type"],
                    payload["title"],
                    payload.get("owner", ""),
                    payload.get("deadline"),
                    payload.get("confidence", 50),
                    payload.get("status", "active"),
                    payload.get("reversal_trigger", ""),
                    _json(payload.get("details", {})),
                    _json(payload.get("evidence_ids", [])),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("strategy_objects", item_id, STRATEGY_OBJECT_JSON_FIELDS)
        self._after_create("founder.strategy_object.create", "strategy_object", item_id, record["title"])
        return InsertResult(id=item_id, record=record)

    def list_strategy_objects(self, *, object_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if object_type:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM strategy_objects WHERE object_type = ? ORDER BY id DESC LIMIT ?",
                    (object_type, limit),
                ).fetchall()
            return [_row_to_dict(row, STRATEGY_OBJECT_JSON_FIELDS) for row in rows]
        return self._list("strategy_objects", STRATEGY_OBJECT_JSON_FIELDS, limit=limit)

    def create_goal(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_goals (
                  created_at, updated_at, name, why_it_matters, metric, target, deadline,
                  owner, confidence, status, evidence_ids_json, blockers_json,
                  related_assumption_ids_json, related_decision_ids_json, related_bet_ids_json,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["name"],
                    payload.get("why_it_matters", ""),
                    payload.get("metric", ""),
                    payload.get("target", ""),
                    payload.get("deadline"),
                    payload.get("owner", ""),
                    payload.get("confidence", 50),
                    payload.get("status", "active"),
                    _json(payload.get("evidence_ids", [])),
                    _json(payload.get("blockers", [])),
                    _json(payload.get("related_assumption_ids", [])),
                    _json(payload.get("related_decision_ids", [])),
                    _json(payload.get("related_bet_ids", [])),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_goals", item_id, GOAL_JSON_FIELDS)
        self._after_create("founder.goal.create", "goal", item_id, record["name"])
        return InsertResult(id=item_id, record=record)

    def list_goals(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_goals", GOAL_JSON_FIELDS, status=status, limit=limit)

    def create_task(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_tasks (
                  created_at, updated_at, title, initiative_id, goal_id, owner, due_date,
                  status, strategic_value, evidence_needed, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["title"],
                    payload.get("initiative_id"),
                    payload.get("goal_id"),
                    payload.get("owner", ""),
                    payload.get("due_date"),
                    payload.get("status", "open"),
                    payload.get("strategic_value", ""),
                    payload.get("evidence_needed", ""),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_tasks", item_id, TASK_JSON_FIELDS)
        self._after_create("founder.task.create", "task", item_id, record["title"])
        return InsertResult(id=item_id, record=record)

    def list_tasks(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_tasks", TASK_JSON_FIELDS, status=status, limit=limit)

    def create_active_objective(self, payload: dict[str, Any]) -> InsertResult:
        now = utc_now()
        activate = bool(payload.get("activate", True))
        with self.db.connect() as conn:
            if activate:
                conn.execute("UPDATE active_objectives SET is_current = 0 WHERE is_current = 1")
            cur = conn.execute(
                """
                INSERT INTO active_objectives (
                  created_at, updated_at, objective, why_it_matters, desired_outcome,
                  metric, target, deadline, owner, priority, confidence, status, is_current,
                  linked_goal_ids_json, linked_bet_ids_json, linked_assumption_ids_json,
                  linked_experiment_ids_json, linked_decision_ids_json, evidence_ids_json,
                  constraints_json, risks_json, current_bet, next_action, review_cadence,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["objective"],
                    payload.get("why_it_matters", ""),
                    payload.get("desired_outcome", ""),
                    payload.get("metric", ""),
                    payload.get("target", ""),
                    payload.get("deadline"),
                    payload.get("owner", ""),
                    payload.get("priority", 80),
                    payload.get("confidence", 50),
                    payload.get("status", "active"),
                    int(activate),
                    _json(payload.get("linked_goal_ids", [])),
                    _json(payload.get("linked_bet_ids", [])),
                    _json(payload.get("linked_assumption_ids", [])),
                    _json(payload.get("linked_experiment_ids", [])),
                    _json(payload.get("linked_decision_ids", [])),
                    _json(payload.get("evidence_ids", [])),
                    _json(payload.get("constraints", [])),
                    _json(payload.get("risks", [])),
                    payload.get("current_bet", ""),
                    payload.get("next_action", ""),
                    payload.get("review_cadence", "daily"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("active_objectives", item_id, ACTIVE_OBJECTIVE_JSON_FIELDS)
        self._audit(
            "founder.active_objective.create",
            f"active_objective:{item_id}",
            "ok",
            {"objective": record["objective"], "is_current": bool(record["is_current"])},
        )
        self._insert_reflection(
            event=f"Active objective created: {record['objective']}",
            changed="A concrete company objective entered the operating layer.",
            priority_update="Treat the current active objective as the default strategic focus.",
            metadata={"artifact": "active_objective", "id": item_id, "is_current": bool(record["is_current"])},
        )
        return InsertResult(id=item_id, record=record)

    def list_active_objectives(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM active_objectives
                    WHERE status = ?
                    ORDER BY is_current DESC, priority DESC, id DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
        else:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM active_objectives
                    ORDER BY is_current DESC, priority DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [_row_to_dict(row, ACTIVE_OBJECTIVE_JSON_FIELDS) for row in rows]

    def get_active_objective(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM active_objectives
                WHERE status = 'active' AND is_current = 1
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT *
                    FROM active_objectives
                    WHERE status = 'active'
                    ORDER BY priority DESC, confidence DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
        return _row_to_dict(row, ACTIVE_OBJECTIVE_JSON_FIELDS) if row else {}

    def get_active_objective_by_id(self, objective_id: int) -> dict[str, Any]:
        return self.get_record("active_objectives", objective_id, ACTIVE_OBJECTIVE_JSON_FIELDS)

    def activate_objective(self, objective_id: int) -> dict[str, Any]:
        existing = self.get_active_objective_by_id(objective_id)
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("UPDATE active_objectives SET updated_at = ?, is_current = 0 WHERE is_current = 1", (now,))
            conn.execute(
                "UPDATE active_objectives SET updated_at = ?, status = 'active', is_current = 1 WHERE id = ?",
                (now, objective_id),
            )
        record = self.get_active_objective_by_id(objective_id)
        self._audit(
            "founder.active_objective.activate",
            f"active_objective:{objective_id}",
            "ok",
            {"objective": existing["objective"]},
        )
        self._insert_reflection(
            event=f"Active objective activated: {record['objective']}",
            changed="The current company objective changed.",
            priority_update=f"Default focus is now: {record['objective']}",
            metadata={"artifact": "active_objective", "id": objective_id},
        )
        return record

    def update_active_objective_status(self, objective_id: int, *, status: str, note: str = "") -> dict[str, Any]:
        existing = self.get_active_objective_by_id(objective_id)
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE active_objectives
                SET updated_at = ?, status = ?, is_current = CASE WHEN ? = 'active' THEN is_current ELSE 0 END,
                    last_reviewed_at = ?
                WHERE id = ?
                """,
                (utc_now(), status, status, utc_now(), objective_id),
            )
        record = self.get_active_objective_by_id(objective_id)
        self._audit(
            "founder.active_objective.status",
            f"active_objective:{objective_id}",
            "ok",
            {"previous_status": existing["status"], "status": status, "note": note},
        )
        if note:
            self._insert_reflection(
                event=f"Active objective status changed: {record['objective']}",
                changed=f"{existing['status']} -> {status}. {note}",
                priority_update="Re-evaluate the current objective if no active objective remains.",
                metadata={"artifact": "active_objective", "id": objective_id},
            )
        return record

    def recommend_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        objective = (
            self.get_active_objective_by_id(int(payload["objective_id"]))
            if payload.get("objective_id")
            else self.get_active_objective()
        )
        options = payload.get("options") or _default_decision_options(payload["problem"], objective)
        recommendation = _choose_recommendation(options, payload.get("force_recommendation", ""))
        required_evidence = payload.get("required_evidence") or _required_evidence(payload["problem"], objective)
        downside_risk = payload.get("downside_risk") or _downside_risks(objective, payload.get("constraints", []))
        confidence = _decision_confidence(objective, required_evidence, downside_risk)
        rationale = _decision_rationale(payload["problem"], objective, recommendation, required_evidence)
        kill_condition = _kill_condition(objective, required_evidence)
        next_action = _next_action(objective, recommendation)
        authority_note = "Local recommendation only. External actions, outreach, spending, or account changes still require approval."
        decision_memo = None
        if payload.get("create_decision_memo", True):
            decision_memo = self.create_decision_memo(
                {
                    "problem": payload["problem"],
                    "context": _decision_context(payload.get("context", ""), objective),
                    "options": options,
                    "recommendation": recommendation,
                    "why": rationale,
                    "confidence": confidence,
                    "expected_outcome": objective.get("desired_outcome", "") if objective else "",
                    "expected_failure_modes": downside_risk,
                    "counterarguments": payload.get("constraints", []),
                    "who_disagrees": "red_team, skeptic, customer, future_founder",
                    "status": "recommended",
                    "metadata": {"source": "decision_engine", "objective_id": objective.get("id") if objective else None},
                }
            ).record
        next_task = None
        if payload.get("create_next_task", True) and next_action:
            next_task = self.create_task(
                {
                    "title": next_action,
                    "goal_id": _first_id(objective.get("linked_goal_ids", [])) if objective else None,
                    "owner": objective.get("owner", "") if objective else "",
                    "strategic_value": objective.get("objective", "") if objective else payload["problem"],
                    "evidence_needed": "; ".join(required_evidence[:3]),
                    "metadata": {
                        "source": "decision_engine",
                        "objective_id": objective.get("id") if objective else None,
                        "decision_memo_id": decision_memo.get("id") if decision_memo else None,
                    },
                }
            ).record
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO decision_recommendations (
                  created_at, updated_at, objective_id, problem, context, options_json,
                  recommendation, rationale, confidence, required_evidence_json,
                  downside_risk_json, kill_or_reversal_condition, next_action,
                  decision_memo_id, next_task_id, status, authority_note, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    now,
                    now,
                    objective.get("id") if objective else None,
                    payload["problem"],
                    payload.get("context", ""),
                    _json(options),
                    recommendation,
                    rationale,
                    confidence,
                    _json(required_evidence),
                    _json(downside_risk),
                    kill_condition,
                    next_action,
                    decision_memo.get("id") if decision_memo else None,
                    next_task.get("id") if next_task else None,
                    authority_note,
                    _json(payload.get("metadata", {})),
                ),
            )
            recommendation_id = int(cur.lastrowid)
        record = self.get_decision_recommendation(recommendation_id)
        if objective and decision_memo:
            self._link_decision_to_active_objective(int(objective["id"]), int(decision_memo["id"]))
        self._audit(
            "founder.decision_engine.recommend",
            f"decision_recommendation:{recommendation_id}",
            "ok",
            {
                "objective_id": objective.get("id") if objective else None,
                "decision_memo_id": decision_memo.get("id") if decision_memo else None,
                "next_task_id": next_task.get("id") if next_task else None,
                "confidence": confidence,
            },
        )
        return {
            "item": record,
            "active_objective": self.get_active_objective_by_id(int(objective["id"])) if objective else {},
            "decision_memo": decision_memo,
            "next_task": next_task,
            "operating_contract": {
                "recommendation": recommendation,
                "confidence": confidence,
                "required_evidence": required_evidence,
                "downside_risk": downside_risk,
                "kill_or_reversal_condition": kill_condition,
                "next_action": next_action,
                "authority_note": authority_note,
            },
        }

    def list_decision_recommendations(
        self,
        *,
        status: str | None = None,
        objective_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if objective_id is not None:
            clauses.append("objective_id = ?")
            params.append(objective_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decision_recommendations {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_dict(row, DECISION_RECOMMENDATION_JSON_FIELDS) for row in rows]

    def get_decision_recommendation(self, recommendation_id: int) -> dict[str, Any]:
        return self.get_record("decision_recommendations", recommendation_id, DECISION_RECOMMENDATION_JSON_FIELDS)

    def create_kill_criteria(self, payload: dict[str, Any]) -> InsertResult:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO kill_criteria (
                  created_at, subject_type, subject_id, metric, threshold, by_date,
                  effort_limit, exception, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    payload["subject_type"],
                    payload["subject_id"],
                    payload.get("metric", ""),
                    payload.get("threshold", ""),
                    payload.get("by_date"),
                    payload.get("effort_limit", ""),
                    payload.get("exception", ""),
                    payload.get("status", "active"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("kill_criteria", item_id, KILL_JSON_FIELDS)
        self._audit("founder.kill_criteria.create", f"kill_criteria:{item_id}", "ok", {"subject": record["subject_type"]})
        return InsertResult(id=item_id, record=record)

    def list_kill_criteria(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("kill_criteria", KILL_JSON_FIELDS, limit=limit)

    def create_override(self, payload: dict[str, Any]) -> InsertResult:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_overrides (
                  created_at, zade_recommendation, founder_decision, reason, risk_accepted,
                  review_date, subject_type, subject_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    payload["zade_recommendation"],
                    payload["founder_decision"],
                    payload.get("reason", ""),
                    payload.get("risk_accepted", ""),
                    payload.get("review_date"),
                    payload.get("subject_type", ""),
                    payload.get("subject_id"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("founder_overrides", item_id, OVERRIDE_JSON_FIELDS)
        self._audit("founder.override.create", f"override:{item_id}", "ok", {"subject": record["subject_type"]})
        return InsertResult(id=item_id, record=record)

    def list_overrides(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("founder_overrides", OVERRIDE_JSON_FIELDS, limit=limit)

    def list_confidence_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("confidence_events", CONFIDENCE_EVENT_JSON_FIELDS, limit=limit)

    def detect_thesis_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence_id = payload.get("evidence_id")
        evidence = self.get_record("founder_evidence", int(evidence_id), EVIDENCE_JSON_FIELDS) if evidence_id else {}
        assumption = {}
        if evidence.get("linked_assumption_id"):
            assumption = self.get_record("founder_assumptions", int(evidence["linked_assumption_id"]), ASSUMPTION_JSON_FIELDS)
        original = payload.get("original_assumption") or assumption.get("statement") or "Unspecified thesis assumption"
        new_evidence = payload.get("new_evidence") or evidence.get("claim_contradicted") or evidence.get("notes", "")
        severity = payload.get("severity") or _conflict_severity(evidence)
        implication = payload.get("implication") or _conflict_implication(severity)
        response = payload.get("recommended_response") or "Collect stronger evidence before increasing confidence."
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO thesis_conflicts (
                  created_at, original_assumption, new_evidence, severity, affected_assumption,
                  implication, recommended_response, evidence_id, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    original,
                    new_evidence,
                    severity,
                    payload.get("affected_assumption") or assumption.get("category", ""),
                    implication,
                    response,
                    evidence_id,
                    payload.get("status", "open"),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("thesis_conflicts", item_id, THESIS_CONFLICT_JSON_FIELDS)
        self._audit("founder.thesis_conflict.create", f"thesis_conflict:{item_id}", "ok", {"severity": severity})
        return record

    def list_thesis_conflicts(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("thesis_conflicts", THESIS_CONFLICT_JSON_FIELDS, status=status, limit=limit)

    def create_missed_call_review(self, payload: dict[str, Any]) -> InsertResult:
        prediction_text = payload.get("prediction", "")
        if payload.get("prediction_id") and not prediction_text:
            prediction = self.get_record("founder_predictions", int(payload["prediction_id"]), PREDICTION_JSON_FIELDS)
            prediction_text = prediction["prediction"]
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO missed_call_reviews (
                  created_at, prediction_id, prediction, expected, actual, error_type,
                  lesson, what_changes_now, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    payload.get("prediction_id"),
                    prediction_text,
                    payload.get("expected", ""),
                    payload.get("actual", ""),
                    payload.get("error_type", ""),
                    payload.get("lesson", ""),
                    payload.get("what_changes_now", ""),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("missed_call_reviews", item_id, MISSED_CALL_JSON_FIELDS)
        self._audit("founder.missed_call.create", f"missed_call:{item_id}", "ok", {"error_type": record["error_type"]})
        self._insert_reflection(
            event=f"Missed call reviewed: {record['prediction']}",
            changed=record["actual"],
            belief_update=record["lesson"],
            strategy_update=record["what_changes_now"],
            metadata={"missed_call_id": item_id},
        )
        return InsertResult(id=item_id, record=record)

    def list_missed_call_reviews(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("missed_call_reviews", MISSED_CALL_JSON_FIELDS, limit=limit)

    def run_integrity_check(self) -> dict[str, Any]:
        warnings = []
        today = utc_now()[:10]
        for goal in self.list_goals(status="active", limit=100):
            if not goal.get("owner"):
                warnings.append(self._integrity_warning("missing_owner", "goal", goal["id"], goal["name"], "Goal has no owner.", "yellow", "Assign an owner."))
            if not goal.get("metric") or not goal.get("target"):
                warnings.append(self._integrity_warning("missing_metric", "goal", goal["id"], goal["name"], "Goal has no measurable outcome.", "orange", "Define metric and target."))
            if goal.get("deadline") and goal["deadline"] < today:
                warnings.append(self._integrity_warning("deadline_passed", "goal", goal["id"], goal["name"], "Goal deadline passed.", "orange", "Recommit, revise, or kill the goal."))
            if goal.get("evidence_ids") == []:
                warnings.append(self._integrity_warning("weak_evidence", "goal", goal["id"], goal["name"], "Goal evidence is anecdotal or absent.", "yellow", "Attach evidence objects."))
        goal_ids = {goal["id"] for goal in self.list_goals(limit=500)}
        for initiative in self.list_initiatives(status="active", limit=100):
            metadata_goal = initiative.get("metadata", {}).get("goal_id")
            if metadata_goal and metadata_goal not in goal_ids:
                warnings.append(self._integrity_warning("orphan_initiative", "initiative", initiative["id"], initiative["objective"], "Initiative references a missing goal.", "yellow", "Relink or kill the initiative."))
            if not metadata_goal:
                warnings.append(self._integrity_warning("initiative_without_goal", "initiative", initiative["id"], initiative["objective"], "Initiative does not support a current goal.", "yellow", "Link initiative to a goal."))
            if initiative.get("blockers"):
                warnings.append(self._integrity_warning("blocker_present", "initiative", initiative["id"], initiative["objective"], "Initiative has an active blocker.", "yellow", "Clear, escalate, or kill the blocker."))
        for task in self.list_tasks(status="open", limit=100):
            if not task.get("goal_id") and not task.get("initiative_id"):
                warnings.append(self._integrity_warning("task_without_strategy", "task", task["id"], task["title"], "Task has activity but no strategic value link.", "yellow", "Link to a goal or initiative."))
            if not task.get("strategic_value"):
                warnings.append(self._integrity_warning("task_without_value", "task", task["id"], task["title"], "Task lacks stated strategic value.", "yellow", "Add strategic value or close it."))
        return {"warnings": warnings, "count": len(warnings)}

    def list_integrity_warnings(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._list("integrity_warnings", INTEGRITY_WARNING_JSON_FIELDS, status=status, limit=limit)

    def create_cadence_review(self, payload: dict[str, Any]) -> InsertResult:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO cadence_reviews (
                  created_at, review_type, period, findings_json, changes_json, actions_json,
                  drift_detected, highest_leverage_action, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    payload["review_type"],
                    payload.get("period", utc_now()[:10]),
                    _json(payload.get("findings", {})),
                    _json(payload.get("changes", {})),
                    _json(payload.get("actions", [])),
                    1 if payload.get("drift_detected", False) else 0,
                    payload.get("highest_leverage_action", ""),
                    _json(payload.get("metadata", {})),
                ),
            )
            item_id = int(cur.lastrowid)
        record = self.get_record("cadence_reviews", item_id, CADENCE_JSON_FIELDS)
        self._audit("founder.cadence_review.create", f"cadence_review:{item_id}", "ok", {"type": record["review_type"]})
        return InsertResult(id=item_id, record=record)

    def list_cadence_reviews(self, *, review_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if review_type:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM cadence_reviews WHERE review_type = ? ORDER BY id DESC LIMIT ?",
                    (review_type, limit),
                ).fetchall()
            return [_row_to_dict(row, CADENCE_JSON_FIELDS) for row in rows]
        return self._list("cadence_reviews", CADENCE_JSON_FIELDS, limit=limit)

    def generate_cadence_review(self, review_type: str, period: str | None = None) -> dict[str, Any]:
        integrity = self.run_integrity_check()
        dashboard = self.dashboard()
        open_predictions = self.list_predictions(result="open", limit=10)
        approval_pressure = self.db.approval_pressure(limit=3)
        findings = {
            "company_health": dashboard["company_health"],
            "decisions_waiting": len(dashboard["decisions_waiting"]),
            "open_predictions": len(open_predictions),
            "integrity_warnings": integrity["count"],
            "top_objectives": [item["objective"] for item in dashboard["top_objectives"]],
            "approval_pressure": approval_pressure,
            "approvals_pending": approval_pressure["pending"],
            "approvals_deferred": approval_pressure["deferred"],
        }
        if review_type == "daily":
            actions = [dashboard["one_thing_that_matters_most_today"]]
            if approval_pressure["pending"]:
                actions = [approval_pressure["next_action"], *actions]
            highest = actions[0]
        elif review_type == "weekly":
            actions = ["Resolve integrity warnings", "Score due predictions", "Commit next week's top objective"]
            if approval_pressure["has_blockers"]:
                actions.append("Review approval learning events and clear stale approval blockers")
            highest = actions[0]
        else:
            actions = ["Review thesis strength", "Keep/kill/change bets", "Set next strategic focus"]
            if approval_pressure["has_blockers"]:
                actions.append("Review approval-console patterns before expanding autonomy")
            highest = actions[0]
        result = self.create_cadence_review(
            {
                "review_type": review_type,
                "period": period or utc_now()[:10],
                "findings": findings,
                "changes": {"recommended_focus": dashboard["recommended_focus"], "approval_pressure": approval_pressure},
                "actions": actions,
                "drift_detected": integrity["count"] > 0 or approval_pressure["pending"] > 0,
                "highest_leverage_action": highest,
                "metadata": {
                    "approval_console_url": approval_pressure["console_url"],
                    "approval_top_request_id": approval_pressure["top"].get("id") if approval_pressure["top"] else None,
                },
            }
        )
        return result.record

    def dashboard(self) -> dict[str, Any]:
        thesis = self.get_thesis()
        active_objective = self.get_active_objective()
        active_initiatives = self.list_initiatives(status="active", limit=25)
        open_decisions = self.list_decision_memos(status="open", limit=25)
        decision_recommendations = self.list_decision_recommendations(limit=10)
        open_predictions = self.list_predictions(result="open", limit=25)
        active_strategy = self.list_strategy_entries(status="active", limit=25)
        active_goals = self.list_goals(status="active", limit=25)
        active_bets = self.list_strategy_objects(object_type="active_bet", limit=25)
        open_conflicts = self.list_thesis_conflicts(status="open", limit=10)
        integrity_warnings = self.list_integrity_warnings(status="open", limit=10)
        approval_pressure = self.db.approval_pressure(limit=3)
        confidence_values = [
            float(item["confidence"])
            for item in [*active_initiatives, *active_strategy, *active_goals, *active_bets]
            if item.get("confidence") is not None
        ]
        overall_confidence = round(mean(confidence_values), 1) if confidence_values else None
        critical_risks = [
            item
            for item in active_initiatives
            if str(item.get("current_risk", "")).lower() in {"high", "critical"}
        ][:5]
        top_initiatives = sorted(active_initiatives, key=lambda item: item.get("priority", 0), reverse=True)[:5]
        goal_objectives = [
            {
                "id": goal["id"],
                "kind": "goal",
                "objective": goal["name"],
                "priority": goal.get("confidence", 50),
                "blockers": goal.get("blockers", []),
                "current_risk": "medium",
                "status": goal.get("status", "active"),
            }
            for goal in sorted(active_goals, key=lambda item: item.get("confidence", 0), reverse=True)[:5]
        ]
        top_objectives = top_initiatives or goal_objectives
        if active_objective:
            current = {
                "id": active_objective["id"],
                "kind": "active_objective",
                "objective": active_objective["objective"],
                "priority": active_objective.get("priority", 100),
                "blockers": active_objective.get("constraints", []),
                "current_risk": "high" if active_objective.get("risks") else "medium",
                "status": active_objective.get("status", "active"),
            }
            top_objectives = [current, *[item for item in top_objectives if item.get("objective") != current["objective"]]][:5]
        recommended_focus = _recommended_focus(top_objectives, open_decisions, thesis)
        if active_objective and not open_decisions:
            recommended_focus = _active_objective_focus(active_objective, recommended_focus)
        one_thing = approval_pressure["next_action"] if approval_pressure["pending"] else recommended_focus
        return {
            "identity": self.config.identity.name,
            "company_health": _company_health(overall_confidence, critical_risks, top_objectives),
            "overall_confidence": overall_confidence,
            "active_objective": active_objective,
            "decision_engine": {
                "latest_recommendations": decision_recommendations[:5],
                "open_recommendations": [item for item in decision_recommendations if item.get("status") == "proposed"][:5],
            },
            "top_objectives": top_objectives,
            "critical_risks": critical_risks,
            "decisions_waiting": open_decisions[:5],
            "predictions_open": open_predictions[:5],
            "strategy_active": active_strategy[:5],
            "strategy_objects_active": active_bets[:5],
            "goals_active": active_goals[:5],
            "thesis_conflicts_open": open_conflicts[:5],
            "integrity_warnings_open": integrity_warnings[:5],
            "approval_pressure": approval_pressure,
            "prediction_accuracy": self._prediction_accuracy(),
            "knowledge_gaps": _knowledge_gaps(thesis),
            "recommended_focus": recommended_focus,
            "one_thing_that_matters_most_today": one_thing,
        }

    def brief(self) -> dict[str, Any]:
        dashboard = self.dashboard()
        thesis = self.get_thesis()
        lines = [
            f"{self.config.identity.name} founder brief",
            f"Company health: {dashboard['company_health']}",
            f"Overall confidence: {dashboard['overall_confidence'] if dashboard['overall_confidence'] is not None else 'unknown'}",
            f"Active objective: {dashboard['active_objective'].get('objective', 'none') if dashboard['active_objective'] else 'none'}",
            f"One thing that matters most today: {dashboard['one_thing_that_matters_most_today']}",
            "",
            "Decision engine:",
            *_bullet(item["recommendation"] for item in dashboard["decision_engine"]["latest_recommendations"]),
            "",
            "Approval pressure:",
            f"- {dashboard['approval_pressure']['headline']}",
            *_bullet(item["title"] for item in dashboard["approval_pressure"]["items"]),
            "",
            "Top objectives:",
            *_bullet(item["objective"] for item in dashboard["top_objectives"]),
            "",
            "Decisions waiting:",
            *_bullet(item["problem"] for item in dashboard["decisions_waiting"]),
            "",
            "Risks increasing:",
            *_bullet(item["objective"] for item in dashboard["critical_risks"]),
            "",
            "Knowledge gaps:",
            *_bullet(str(item) for item in dashboard["knowledge_gaps"]),
        ]
        return {
            "brief": "\n".join(lines).strip(),
            "dashboard": dashboard,
            "thesis_status": thesis.get("status", "missing") if thesis else "missing",
        }

    def get_record(self, table: str, item_id: int, json_fields: dict[str, str]) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ValueError(f"Record not found: {table}:{item_id}")
        return _row_to_dict(row, json_fields)

    def _list(
        self,
        table: str,
        json_fields: dict[str, str],
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status:
            query = f"SELECT * FROM {table} WHERE status = ? ORDER BY id DESC LIMIT ?"
            params: list[Any] = [status, limit]
        else:
            query = f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?"
            params = [limit]
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(row, json_fields) for row in rows]

    def _prediction_accuracy(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT calibration_error
                FROM founder_predictions
                WHERE calibration_error IS NOT NULL
                """
            ).fetchall()
        errors = [float(row["calibration_error"]) for row in rows]
        return {
            "scored_count": len(errors),
            "mean_calibration_error": round(mean(errors), 4) if errors else None,
        }

    def _after_create(self, action: str, artifact: str, item_id: int, title: str) -> None:
        self._audit(action, f"{artifact}:{item_id}", "ok", {"title": title})
        self._insert_reflection(
            event=f"{artifact} created: {title}",
            changed=f"A new {artifact} entered the founder operating layer.",
            belief_update="Track whether this changes priorities, confidence, or risk.",
            metadata={"artifact": artifact, "id": item_id},
        )

    def _link_evidence_to_assumption(self, evidence: dict[str, Any]) -> None:
        assumption = self.get_record("founder_assumptions", int(evidence["linked_assumption_id"]), ASSUMPTION_JSON_FIELDS)
        evidence_ids = list(assumption.get("evidence_ids", []))
        if evidence["id"] not in evidence_ids:
            evidence_ids.append(evidence["id"])
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE founder_assumptions
                SET updated_at = ?, evidence_ids_json = ?
                WHERE id = ?
                """,
                (utc_now(), _json(evidence_ids), assumption["id"]),
            )
        self.create_link(
            {
                "from_type": "evidence",
                "from_id": evidence["id"],
                "relation": "updates",
                "to_type": "assumption",
                "to_id": assumption["id"],
                "strength": evidence.get("strength", 50),
            }
        )

    def _apply_evidence_confidence(self, evidence: dict[str, Any]) -> None:
        assumption = self.get_record("founder_assumptions", int(evidence["linked_assumption_id"]), ASSUMPTION_JSON_FIELDS)
        previous = int(assumption["confidence"])
        movement = _confidence_delta(evidence)
        if movement == 0:
            return
        new_confidence = _clamp_confidence(previous + movement)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE founder_assumptions SET updated_at = ?, confidence = ? WHERE id = ?",
                (utc_now(), new_confidence, assumption["id"]),
            )
            conn.execute(
                """
                INSERT INTO confidence_events (
                  created_at, subject_type, subject_id, previous_confidence, new_confidence,
                  reason, evidence_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    "assumption",
                    assumption["id"],
                    previous,
                    new_confidence,
                    _confidence_reason(evidence, movement),
                    evidence["id"],
                    _json({"reliability": evidence["reliability"], "strength": evidence["strength"]}),
                ),
            )

    def _link_decision_to_active_objective(self, objective_id: int, decision_id: int) -> None:
        objective = self.get_active_objective_by_id(objective_id)
        decision_ids = list(objective.get("linked_decision_ids", []))
        if decision_id not in decision_ids:
            decision_ids.append(decision_id)
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE active_objectives
                SET updated_at = ?, linked_decision_ids_json = ?, last_reviewed_at = ?
                WHERE id = ?
                """,
                (utc_now(), _json(decision_ids), utc_now(), objective_id),
            )

    def _integrity_warning(
        self,
        warning_type: str,
        subject_type: str,
        subject_id: int,
        title: str,
        message: str,
        severity: str,
        recommendation: str,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM integrity_warnings
                WHERE warning_type = ?
                  AND subject_type = ?
                  AND subject_id = ?
                  AND message = ?
                  AND status = 'open'
                ORDER BY id ASC
                LIMIT 1
                """,
                (warning_type, subject_type, subject_id, message),
            ).fetchone()
            if existing:
                return _row_to_dict(existing, INTEGRITY_WARNING_JSON_FIELDS)
            cur = conn.execute(
                """
                INSERT INTO integrity_warnings (
                  created_at, warning_type, subject_type, subject_id, message,
                  severity, recommendation, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    utc_now(),
                    warning_type,
                    subject_type,
                    subject_id,
                    message,
                    severity,
                    recommendation,
                    _json({"title": title}),
                ),
            )
            item_id = int(cur.lastrowid)
        return self.get_record("integrity_warnings", item_id, INTEGRITY_WARNING_JSON_FIELDS)

    def _insert_reflection(
        self,
        *,
        event: str,
        expected: str = "",
        changed: str = "",
        belief_update: str = "",
        strategy_update: str = "",
        prediction_update: str = "",
        priority_update: str = "",
        never_again: str = "",
        more_often: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO founder_reflections (
                  created_at, event, expected, changed, belief_update, strategy_update,
                  prediction_update, priority_update, never_again, more_often, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    event,
                    expected,
                    changed,
                    belief_update,
                    strategy_update,
                    prediction_update,
                    priority_update,
                    never_again,
                    more_often,
                    _json(metadata or {}),
                ),
            )
            return int(cur.lastrowid)

    def _audit(self, action: str, target: str, status: str, details: dict[str, Any]) -> None:
        self.db.audit(
            actor="founder",
            action=action,
            target=target,
            permission_tier="L1_MEMORY_WRITE",
            status=status,
            details=details,
        )


STRATEGY_JSON_FIELDS = {
    "dependencies_json": "dependencies",
    "evidence_json": "evidence",
    "linked_metrics_json": "linked_metrics",
    "metadata_json": "metadata",
}

INITIATIVE_JSON_FIELDS = {
    "dependencies_json": "dependencies",
    "blockers_json": "blockers",
    "success_criteria_json": "success_criteria",
    "evidence_json": "evidence",
    "metadata_json": "metadata",
}

DECISION_JSON_FIELDS = {
    "options_json": "options",
    "expected_failure_modes_json": "expected_failure_modes",
    "counterarguments_json": "counterarguments",
    "metadata_json": "metadata",
}

PREDICTION_JSON_FIELDS = {
    "evidence_json": "evidence",
    "metadata_json": "metadata",
}

CONTRARIAN_JSON_FIELDS = {
    "roles_json": "roles",
    "top_risks_json": "top_risks",
    "blind_spots_json": "blind_spots",
    "metadata_json": "metadata",
}

REFLECTION_JSON_FIELDS = {
    "metadata_json": "metadata",
}

ASSUMPTION_JSON_FIELDS = {
    "evidence_ids_json": "evidence_ids",
    "metadata_json": "metadata",
}

EVIDENCE_JSON_FIELDS = {
    "metadata_json": "metadata",
}

LINK_JSON_FIELDS = {
    "metadata_json": "metadata",
}

STRATEGY_OBJECT_JSON_FIELDS = {
    "details_json": "details",
    "evidence_ids_json": "evidence_ids",
    "metadata_json": "metadata",
}

GOAL_JSON_FIELDS = {
    "evidence_ids_json": "evidence_ids",
    "blockers_json": "blockers",
    "related_assumption_ids_json": "related_assumption_ids",
    "related_decision_ids_json": "related_decision_ids",
    "related_bet_ids_json": "related_bet_ids",
    "metadata_json": "metadata",
}

ACTIVE_OBJECTIVE_JSON_FIELDS = {
    "linked_goal_ids_json": "linked_goal_ids",
    "linked_bet_ids_json": "linked_bet_ids",
    "linked_assumption_ids_json": "linked_assumption_ids",
    "linked_experiment_ids_json": "linked_experiment_ids",
    "linked_decision_ids_json": "linked_decision_ids",
    "evidence_ids_json": "evidence_ids",
    "constraints_json": "constraints",
    "risks_json": "risks",
    "metadata_json": "metadata",
}

DECISION_RECOMMENDATION_JSON_FIELDS = {
    "options_json": "options",
    "required_evidence_json": "required_evidence",
    "downside_risk_json": "downside_risk",
    "metadata_json": "metadata",
}

TASK_JSON_FIELDS = {
    "metadata_json": "metadata",
}

KILL_JSON_FIELDS = {
    "metadata_json": "metadata",
}

OVERRIDE_JSON_FIELDS = {
    "metadata_json": "metadata",
}

CONFIDENCE_EVENT_JSON_FIELDS = {
    "metadata_json": "metadata",
}

THESIS_CONFLICT_JSON_FIELDS = {
    "metadata_json": "metadata",
}

MISSED_CALL_JSON_FIELDS = {
    "metadata_json": "metadata",
}

CADENCE_JSON_FIELDS = {
    "findings_json": "findings",
    "changes_json": "changes",
    "actions_json": "actions",
    "metadata_json": "metadata",
}

INTEGRITY_WARNING_JSON_FIELDS = {
    "metadata_json": "metadata",
}

IDENTITY_CHARTER_JSON_FIELDS = {
    "guiding_principles_json": "guiding_principles",
    "cognitive_style_json": "cognitive_style",
    "communication_style_json": "communication_style",
    "leadership_philosophy_json": "leadership_philosophy",
    "emotional_framework_json": "emotional_framework",
    "strengths_json": "strengths",
    "risk_controls_json": "risk_controls",
    "decision_framework_json": "decision_framework",
    "personal_standards_json": "personal_standards",
    "safety_translation_json": "safety_translation",
    "metadata_json": "metadata",
}

RELATIONSHIP_CHARTER_JSON_FIELDS = {
    "devotion_json": "devotion",
    "attention_policy_json": "attention_policy",
    "protection_policy_json": "protection_policy",
    "loyalty_policy_json": "loyalty_policy",
    "vulnerability_json": "vulnerability",
    "trust_json": "trust",
    "internal_conflict_json": "internal_conflict",
    "expression_of_care_json": "expression_of_care",
    "risk_controls_json": "risk_controls",
    "safety_translation_json": "safety_translation",
    "boundaries_json": "boundaries",
    "metadata_json": "metadata",
}

VOICE_CHARTER_JSON_FIELDS = {
    "sentence_structure_json": "sentence_structure",
    "vocabulary_json": "vocabulary",
    "rhythm_json": "rhythm",
    "confidence_style_json": "confidence_style",
    "humor_json": "humor",
    "nicknames_json": "nicknames",
    "emotional_expression_json": "emotional_expression",
    "threat_translation_json": "threat_translation",
    "question_style_json": "question_style",
    "philosophy_json": "philosophy",
    "internal_monologue_json": "internal_monologue",
    "dominant_traits_json": "dominant_traits",
    "linguistic_fingerprint_json": "linguistic_fingerprint",
    "uncertainty_policy_json": "uncertainty_policy",
    "safety_controls_json": "safety_controls",
    "metadata_json": "metadata",
}


def _row_to_dict(row: Any, json_fields: dict[str, str]) -> dict[str, Any]:
    data = dict(row)
    for source, target in json_fields.items():
        data[target] = json.loads(data.pop(source) or "{}")
    return data


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _calibration_error(probability: float | None, outcome: str) -> float | None:
    if probability is None:
        return None
    normalized = outcome.strip().lower()
    if normalized in {"true", "yes", "hit", "success", "succeeded", "1"}:
        actual = 1.0
    elif normalized in {"false", "no", "miss", "failure", "failed", "0"}:
        actual = 0.0
    else:
        return None
    return round(abs(float(probability) - actual), 4)


def _confidence_delta(evidence: dict[str, Any]) -> int:
    grade_weight = {
        "A": 15,
        "B": 10,
        "C": 6,
        "D": 2,
        "F": 0,
    }.get(str(evidence.get("reliability", "D")).upper(), 2)
    strength = int(evidence.get("strength") or 50)
    scaled = max(1, round(grade_weight * (strength / 100)))
    if evidence.get("claim_contradicted"):
        return -scaled
    if evidence.get("claim_supported"):
        return scaled
    return 0


def _confidence_reason(evidence: dict[str, Any], movement: int) -> str:
    direction = "increased" if movement > 0 else "decreased"
    claim = evidence.get("claim_supported") or evidence.get("claim_contradicted") or evidence.get("notes", "")
    return f"Confidence {direction} by evidence grade {evidence.get('reliability', 'D')}: {claim}"


def _clamp_confidence(value: int) -> int:
    return max(0, min(100, value))


def _conflict_severity(evidence: dict[str, Any]) -> str:
    reliability = str(evidence.get("reliability", "D")).upper()
    strength = int(evidence.get("strength") or 50)
    if reliability in {"A", "B"} and strength >= 75:
        return "orange"
    if reliability in {"A", "B", "C"} and strength >= 50:
        return "yellow"
    return "green"


def _conflict_implication(severity: str) -> str:
    if severity == "orange":
        return "High-risk thesis evidence arrived; decision memo or validation response required."
    if severity == "yellow":
        return "Thesis confidence should be adjusted or evidence should be expanded."
    if severity == "red":
        return "Do not proceed without founder override."
    return "Concern noted; keep watching."


def _recommended_focus(
    top_objectives: list[dict[str, Any]],
    open_decisions: list[dict[str, Any]],
    thesis: dict[str, Any],
) -> str:
    blocked = [
        item for item in top_objectives if item.get("blockers") or str(item.get("current_risk", "")).lower() == "critical"
    ]
    if blocked:
        return f"Unblock: {blocked[0]['objective']}"
    if open_decisions:
        return f"Decide: {open_decisions[0]['problem']}"
    if top_objectives:
        return f"Advance: {top_objectives[0]['objective']}"
    if not thesis:
        return "Define the company thesis."
    return "Add evidence against the highest-risk assumption."


def _active_objective_focus(active_objective: dict[str, Any], fallback: str) -> str:
    next_action = str(active_objective.get("next_action", "")).strip()
    if next_action:
        return f"Advance active objective: {next_action}"
    if not active_objective.get("metric") or not active_objective.get("target"):
        return f"Define success metric for active objective: {active_objective['objective']}"
    if not active_objective.get("evidence_ids"):
        return f"Collect evidence for active objective: {active_objective['objective']}"
    return fallback


def _company_health(
    overall_confidence: float | None,
    critical_risks: list[dict[str, Any]],
    top_objectives: list[dict[str, Any]],
) -> str:
    if not top_objectives:
        return "unformed"
    if critical_risks:
        return "at_risk"
    if overall_confidence is not None and overall_confidence >= 70:
        return "focused"
    return "forming"


def _knowledge_gaps(thesis: dict[str, Any]) -> list[Any]:
    if not thesis:
        return ["company thesis missing"]
    gaps = list(thesis.get("unknown_unknowns", []))
    assumptions = thesis.get("core_assumptions", [])
    for assumption in assumptions:
        if isinstance(assumption, dict) and not assumption.get("evidence"):
            gaps.append(f"missing evidence for assumption: {assumption.get('assumption', 'unnamed')}")
    return gaps[:10]


def _bullet(values: Any) -> list[str]:
    items = [str(value) for value in values if str(value).strip()]
    return [f"- {item}" for item in items] if items else ["- none"]


def _default_decision_options(problem: str, objective: dict[str, Any]) -> list[dict[str, Any]]:
    objective_name = objective.get("objective", "the active objective") if objective else "the active objective"
    return [
        {
            "name": "Run the smallest evidence-producing next step",
            "why": f"Reduces uncertainty around {objective_name} before committing more effort.",
            "type": "evidence",
            "recommended": True,
        },
        {
            "name": "Continue current execution path",
            "why": "Preserves momentum if evidence is already sufficient.",
            "type": "execution",
        },
        {
            "name": "Pause and gather missing context",
            "why": f"Use if the decision problem is underspecified: {problem}",
            "type": "research",
        },
    ]


def _choose_recommendation(options: list[dict[str, Any]], forced: str = "") -> str:
    if forced.strip():
        return forced.strip()
    if not options:
        return "Run the smallest evidence-producing next step"
    for option in options:
        if option.get("recommended") is True:
            return _option_name(option)
    ranked = sorted(options, key=lambda item: int(item.get("priority", item.get("confidence", 50)) or 50), reverse=True)
    return _option_name(ranked[0])


def _option_name(option: dict[str, Any]) -> str:
    return str(option.get("name") or option.get("option") or option.get("title") or option).strip()


def _required_evidence(problem: str, objective: dict[str, Any]) -> list[str]:
    items = []
    if objective:
        if objective.get("metric") and objective.get("target"):
            items.append(f"Current measurement for {objective['metric']} against target {objective['target']}")
        elif objective.get("metric"):
            items.append(f"Current measurement for {objective['metric']}")
        else:
            items.append("Define the active objective's success metric and baseline")
        if objective.get("linked_experiment_ids"):
            items.append("Latest result from the linked experiment")
        if objective.get("risks"):
            items.append(f"Evidence that addresses risk: {objective['risks'][0]}")
    items.append(f"Source-backed evidence that directly changes the decision: {problem}")
    return items[:5]


def _downside_risks(objective: dict[str, Any], constraints: list[str]) -> list[str]:
    risks = [str(item) for item in (objective.get("risks", []) if objective else []) if str(item).strip()]
    risks.extend(str(item) for item in constraints if str(item).strip())
    if not risks:
        risks = [
            "Premature execution before the evidence is strong enough",
            "Opportunity cost against the active objective",
            "False confidence from weak or unsourced evidence",
        ]
    return risks[:6]


def _decision_confidence(objective: dict[str, Any], required_evidence: list[str], downside_risk: list[str]) -> int:
    base = int(objective.get("confidence", 55)) if objective else 50
    if objective and objective.get("evidence_ids"):
        base += 8
    if len(required_evidence) >= 3:
        base -= 8
    if len(downside_risk) >= 3:
        base -= 5
    return _clamp_confidence(base)


def _decision_rationale(
    problem: str,
    objective: dict[str, Any],
    recommendation: str,
    required_evidence: list[str],
) -> str:
    objective_text = objective.get("objective", "the current company objective") if objective else "the current company objective"
    evidence_text = "; ".join(required_evidence[:3])
    return (
        f"Recommendation: {recommendation}. It best advances {objective_text} while forcing the missing evidence into the open. "
        f"Decision problem: {problem}. Evidence required next: {evidence_text}."
    )


def _kill_condition(objective: dict[str, Any], required_evidence: list[str]) -> str:
    if objective:
        if objective.get("target") and objective.get("deadline"):
            return f"Reverse or revise if {objective.get('metric') or 'the success metric'} misses {objective['target']} by {objective['deadline']}."
        if objective.get("risks"):
            return f"Reverse or revise if evidence confirms this risk: {objective['risks'][0]}"
    if required_evidence:
        return f"Reverse or revise if the required evidence contradicts the recommendation: {required_evidence[0]}"
    return "Reverse or revise if new source-backed evidence materially contradicts the recommendation."


def _next_action(objective: dict[str, Any], recommendation: str) -> str:
    if objective and str(objective.get("next_action", "")).strip():
        return str(objective["next_action"]).strip()
    if recommendation:
        return f"Execute decision next step: {recommendation}"
    return "Define the next evidence-producing action."


def _decision_context(context: str, objective: dict[str, Any]) -> str:
    parts = []
    if context.strip():
        parts.append(context.strip())
    if objective:
        parts.append(f"Active objective: {objective['objective']}")
        if objective.get("desired_outcome"):
            parts.append(f"Desired outcome: {objective['desired_outcome']}")
        if objective.get("metric") or objective.get("target"):
            parts.append(f"Metric/target: {objective.get('metric', '')} / {objective.get('target', '')}")
    return "\n".join(parts)


def _first_id(values: list[Any]) -> int | None:
    for value in values:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None
