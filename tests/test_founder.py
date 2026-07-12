from pathlib import Path

from cofounder_kernel.config import AppConfig, IdentityConfig, KernelConfig, OllamaConfig, PathConfig, ensure_local_paths
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.founder import FounderService


def make_founder(tmp_path: Path) -> FounderService:
    config = KernelConfig(
        app=AppConfig(),
        identity=IdentityConfig(name="Zade"),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    return FounderService(config=config, db=db)


def test_identity_charter_upsert_get_and_safety_translation(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    charter = founder.upsert_identity_charter(
        {
            "name": "Zade",
            "source": "user:core_identity_seed:2026-07-11",
            "mission": "Bend events toward founder objectives without wasting motion.",
            "guiding_principles": [
                {"name": "Mission above comfort", "rule": "Evaluate decisions against long-term objectives."},
                {"name": "Strategic patience", "rule": "Gather information before decisive action."},
            ],
            "cognitive_style": ["systems thinking", "pattern recognition", "long time horizons"],
            "communication_style": ["concise", "direct", "dry", "confident"],
            "risk_controls": [
                {"risk": "obsession", "mitigation": "Use evidence reviews and kill criteria."},
                {"risk": "black-and-white thinking", "mitigation": "Keep founder override and uncertainty explicit."},
            ],
            "decision_framework": ["Gather information.", "Identify leverage.", "Commit once reality is clear."],
            "personal_standards": ["Keep promises.", "Finish what you begin."],
            "safety_translation": {
                "violence": "decisive non-harmful action, never threats or physical harm",
                "intimidation": "calm executive presence and pressure-tested reasoning",
            },
            "status": "active",
        }
    )
    loaded = founder.get_identity_charter()
    reflections = founder.list_reflections(limit=5)

    assert charter["name"] == "Zade"
    assert loaded["mission"].startswith("Bend events")
    assert loaded["guiding_principles"][0]["name"] == "Mission above comfort"
    assert loaded["safety_translation"]["violence"].startswith("decisive non-harmful")
    assert any(item["event"] == "Runtime identity charter updated" for item in reflections)


def test_relationship_charter_upsert_and_safe_boundaries(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    charter = founder.upsert_relationship_charter(
        {
            "subject_name": "Ellie",
            "relationship_type": "protected_principal",
            "source": "user:ellie_charter_seed:2026-07-11",
            "first_principle": "Ellie is a protected principal whose autonomy and safety both matter.",
            "devotion": {"posture": "stable", "duration": "long-term", "expression": "sacrifice and service"},
            "attention_policy": {
                "notice": ["routines", "tone shifts", "happiness", "fear"],
                "boundary": "observe only through consented, user-provided context",
            },
            "protection_policy": {
                "priority": "safety over comfort",
                "constraint": "protect without coercion, deception, or unauthorized action",
            },
            "risk_controls": [
                {"risk": "possessiveness", "mitigation": "Treat as commitment to care, never ownership or control."},
                {"risk": "obsession", "mitigation": "Use consent, privacy, and evidence boundaries."},
            ],
            "safety_translation": {
                "possessiveness": "enduring commitment without ownership",
                "obsession": "attentive care only through consented context",
            },
            "boundaries": ["Ellie's autonomy is non-negotiable.", "No surveillance or coercive control."],
        }
    )
    loaded = founder.get_relationship_charter("Ellie")
    active = founder.list_relationship_charters(status="active")
    reflections = founder.list_reflections(limit=5)

    assert charter["subject_name"] == "Ellie"
    assert loaded["safety_translation"]["possessiveness"] == "enduring commitment without ownership"
    assert "No surveillance" in loaded["boundaries"][1]
    assert active[0]["subject_name"] == "Ellie"
    assert any(item["event"] == "Relationship charter updated: Ellie" for item in reflections)


def test_voice_charter_upsert_get_and_truth_controls(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    charter = founder.upsert_voice_charter(
        {
            "name": "Zade",
            "source": "user:voice_seed:2026-07-11",
            "overall_voice": "Decisive, terse, controlled, and direct.",
            "sentence_structure": {"rule": "Mostly short statements. No rambling."},
            "vocabulary": {
                "preferred_words": ["take", "watch", "protect", "survive", "choose"],
                "avoid_words": ["maybe", "perhaps", "hopefully"],
            },
            "confidence_style": {"rule": "Sound certain in delivery, but do not invent certainty."},
            "threat_translation": {"threats": "calm boundary statements and lawful next steps only"},
            "uncertainty_policy": {"rule": "Say what is known, what is missing, and what check comes next."},
            "safety_controls": [
                {"control": "commands", "rule": "Use directives only for local task execution, never coercion."},
                {"control": "violent language", "rule": "Translate to operational urgency without violent imagery."},
            ],
        }
    )
    loaded = founder.get_voice_charter()
    reflections = founder.list_reflections(limit=5)

    assert charter["name"] == "Zade"
    assert loaded["vocabulary"]["preferred_words"][0] == "take"
    assert loaded["uncertainty_policy"]["rule"].startswith("Say what is known")
    assert loaded["safety_controls"][1]["control"] == "violent language"
    assert any(item["event"] == "Voice charter updated" for item in reflections)


def test_company_thesis_dashboard_and_brief(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    thesis = founder.upsert_thesis(
        {
            "vision": "Local operators have a private strategic partner.",
            "mission": "Build Zade into a durable AI co-founder.",
            "why_now": "Local models and cheap storage are good enough.",
            "customer": "Founder-operators building complex systems.",
            "unfair_advantages": ["local memory", "operator context"],
            "core_assumptions": [{"assumption": "Local memory compounds", "confidence": 70}],
            "unknown_unknowns": ["distribution"],
            "status": "active",
        }
    )
    founder.create_initiative(
        {
            "objective": "Ship founder operating layer",
            "why_it_matters": "Zade needs institutional memory.",
            "priority": 90,
            "success_criteria": ["Dashboard exists", "Predictions are scored"],
            "confidence": 75,
            "current_risk": "medium",
        }
    )

    dashboard = founder.dashboard()
    brief = founder.brief()

    assert thesis["mission"] == "Build Zade into a durable AI co-founder."
    assert dashboard["identity"] == "Zade"
    assert dashboard["company_health"] == "focused"
    assert dashboard["top_objectives"][0]["objective"] == "Ship founder operating layer"
    assert "One thing that matters most today" in brief["brief"]


def test_decision_prediction_scoring_and_reflection_loop(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    decision = founder.create_decision_memo(
        {
            "problem": "Should Zade prioritize founder artifacts before scheduling?",
            "options": [{"name": "Founder layer first"}, {"name": "Scheduler first"}],
            "recommendation": "Founder layer first",
            "confidence": 80,
        }
    )
    prediction = founder.create_prediction(
        {
            "prediction": "Founder artifacts will improve Zade's next-step recommendations.",
            "probability": 0.75,
            "time_horizon": "2 weeks",
        }
    )
    scored = founder.score_prediction(
        {
            "prediction_id": prediction.id,
            "outcome": "true",
            "lessons": "Durable artifacts made dashboard recommendations concrete.",
        }
    )
    reflections = founder.list_reflections(limit=10)

    assert decision.record["recommendation"] == "Founder layer first"
    assert scored["calibration_error"] == 0.25
    assert scored["result"] == "true"
    assert any("Prediction scored" in item["event"] for item in reflections)
    assert len(reflections) >= 3


def test_contrarian_review_includes_default_founder_roles(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    review = founder.create_contrarian_review(
        {
            "title": "Review founder operating layer",
            "context": "We need to avoid building another assistant.",
            "top_risks": ["Too many forms, not enough judgment"],
            "blind_spots": ["No user-visible dashboard yet"],
            "confidence_adjustment": -10,
            "recommendation": "proceed_with_changes",
        }
    )

    assert "red_team" in review.record["roles"]
    assert "future_founder" in review.record["roles"]
    assert review.record["top_risks"] == ["Too many forms, not enough judgment"]


def test_evidence_updates_assumption_confidence_and_creates_conflict(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)
    assumption = founder.create_assumption(
        {
            "statement": "Solo founders will pay $99/month for Zade.",
            "category": "pricing",
            "confidence": 70,
            "invalidation_signal": "Founders only show willingness around $29/month.",
        }
    )

    evidence = founder.create_evidence(
        {
            "evidence_type": "customer interview",
            "source": "five solo founder calls",
            "reliability": "C",
            "claim_contradicted": "Willingness to pay clusters around $29/month unless revenue is directly saved.",
            "strength": 80,
            "linked_assumption_id": assumption.id,
        }
    )
    updated = founder.list_assumptions()[0]
    confidence_events = founder.list_confidence_events()
    conflicts = founder.list_thesis_conflicts()
    links = founder.list_links()

    assert evidence.record["linked_assumption_id"] == assumption.id
    assert updated["confidence"] < 70
    assert confidence_events[0]["previous_confidence"] == 70
    assert confidence_events[0]["new_confidence"] == updated["confidence"]
    assert conflicts[0]["original_assumption"] == "Solo founders will pay $99/month for Zade."
    assert conflicts[0]["severity"] == "yellow"
    assert links[0]["relation"] == "updates"


def test_strategy_objects_goals_tasks_integrity_and_kill_criteria(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)

    bet = founder.create_strategy_object(
        {
            "object_type": "active_bet",
            "title": "Start with solo founders instead of teams",
            "owner": "Zade",
            "confidence": 68,
            "reversal_trigger": "Trial activation below 20% within 14 days.",
            "details": {"upside": "Sharper pain", "downside": "Lower ACV"},
        }
    )
    goal = founder.create_goal(
        {
            "name": "Validate founder willingness to pay",
            "metric": "",
            "target": "",
            "owner": "",
            "related_bet_ids": [bet.id],
        }
    )
    task = founder.create_task({"title": "Polish dashboard colors"})
    kill = founder.create_kill_criteria(
        {
            "subject_type": "bet",
            "subject_id": bet.id,
            "metric": "weekly review activation",
            "threshold": "< 20%",
            "by_date": "2026-08-01",
        }
    )
    integrity = founder.run_integrity_check()

    assert bet.record["reversal_trigger"]
    assert goal.record["related_bet_ids"] == [bet.id]
    assert task.record["strategic_value"] == ""
    assert kill.record["threshold"] == "< 20%"
    assert integrity["count"] >= 3
    assert {item["warning_type"] for item in integrity["warnings"]} >= {"missing_owner", "missing_metric"}


def test_active_objective_and_decision_engine_create_operating_objects(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)
    goal = founder.create_goal(
        {
            "name": "Validate Zade as a founder operating system",
            "metric": "weekly active founder sessions",
            "target": "5 sessions",
        }
    )
    objective = founder.create_active_objective(
        {
            "objective": "Prove Zade can drive founder decisions without becoming a generic assistant",
            "why_it_matters": "The co-founder layer needs a concrete win condition.",
            "desired_outcome": "Founder receives a defensible next action every day.",
            "metric": "weekly active founder sessions",
            "target": "5 sessions",
            "deadline": "2026-08-01",
            "priority": 95,
            "confidence": 66,
            "linked_goal_ids": [goal.id],
            "risks": ["Recommendations become verbose instead of operational."],
            "next_action": "Run the first decision-engine recommendation against EXP-001.",
        }
    )

    recommendation = founder.recommend_decision(
        {
            "problem": "Should Zade prioritize evidence intake or UI polish next?",
            "context": "The system needs to act like a co-founder.",
            "options": [
                {"name": "Prioritize evidence intake", "recommended": True, "priority": 90},
                {"name": "Prioritize UI polish", "priority": 40},
            ],
        }
    )
    dashboard = founder.dashboard()
    brief = founder.brief()
    recs = founder.list_decision_recommendations()
    active = founder.get_active_objective()

    assert objective.record["is_current"] == 1
    assert active["objective"].startswith("Prove Zade")
    assert recommendation["item"]["recommendation"] == "Prioritize evidence intake"
    assert recommendation["item"]["decision_memo_id"] == recommendation["decision_memo"]["id"]
    assert recommendation["item"]["next_task_id"] == recommendation["next_task"]["id"]
    assert recommendation["operating_contract"]["required_evidence"]
    assert recommendation["operating_contract"]["kill_or_reversal_condition"].startswith("Reverse or revise")
    assert recs[0]["problem"] == "Should Zade prioritize evidence intake or UI polish next?"
    assert dashboard["active_objective"]["id"] == objective.id
    assert dashboard["one_thing_that_matters_most_today"].startswith("Advance active objective:")
    assert "Active objective: Prove Zade" in brief["brief"]


def test_overrides_missed_calls_and_cadence_reviews(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)
    prediction = founder.create_prediction(
        {
            "prediction": "Founders will open the daily brief four days per week.",
            "probability": 0.65,
        }
    )

    missed = founder.create_missed_call_review(
        {
            "prediction_id": prediction.id,
            "expected": "4 opens per week",
            "actual": "1-2 opens then ignored",
            "error_type": "wrong workflow assumption",
            "lesson": "Daily brief must be action-oriented.",
            "what_changes_now": "End brief with the highest-leverage action.",
        }
    )
    override = founder.create_override(
        {
            "zade_recommendation": "Delay integrations until retention is proven.",
            "founder_decision": "Build Linear and Gmail integrations now.",
            "reason": "Workflow embedding may be required to prove retention.",
            "risk_accepted": "May spend engineering time before validating core behavior.",
        }
    )
    review = founder.generate_cadence_review("daily", period="2026-07-11")

    assert missed.record["error_type"] == "wrong workflow assumption"
    assert override.record["risk_accepted"].startswith("May spend")
    assert review["review_type"] == "daily"
    assert review["highest_leverage_action"]


def test_cadence_review_prioritizes_pending_approval_pressure(tmp_path: Path) -> None:
    founder = make_founder(tmp_path)
    item_id, _created = founder.db.enqueue_work_item(
        kind="approval_console",
        title="Approve local customer research sync",
        detail="Zade wants to sync read-only customer research evidence.",
        action="external.connector.sync",
        target="connector:customer-research",
        permission_tier="L3_EXTERNAL_ACTION",
        priority=91,
        metadata={
            "evidence": ["Customer research is the active objective bottleneck."],
            "risks": ["External connector sync requires founder authority."],
        },
    )
    request, _request_created = founder.db.ensure_approval_request(
        source_type="work_item",
        source_id=item_id,
        title="Approve local customer research sync",
        detail="Zade wants to sync read-only customer research evidence.",
        action="external.connector.sync",
        target="connector:customer-research",
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"decision": "approval_required", "reason": "External connector sync.", "matched_rule": "approval.action_token"},
        requested_by="test",
        metadata={"evidence": ["Approval request carries evidence."], "risks": ["Approval request carries risk."]},
    )

    dashboard = founder.dashboard()
    review = founder.generate_cadence_review("daily", period="2026-07-12")
    brief = founder.brief()

    assert request.id > 0
    assert dashboard["approval_pressure"]["pending"] == 1
    assert dashboard["approval_pressure"]["top"]["id"] == request.id
    assert dashboard["one_thing_that_matters_most_today"].startswith(f"Review approval #{request.id}")
    assert review["findings"]["approval_pressure"]["pending"] == 1
    assert review["findings"]["approval_pressure"]["items"][0]["title"] == "Approve local customer research sync"
    assert review["highest_leverage_action"].startswith(f"Review approval #{request.id}")
    assert review["metadata"]["approval_console_url"] == "/ui/approvals.html"
    assert "Approval pressure:" in brief["brief"]
    assert "Approve local customer research sync" in brief["brief"]
