"""Lexical keyword router: profile scoring, explicit selection, safety gate,
false-positive controls, skill hints, and kernel-mapped workflow flags."""
from __future__ import annotations

from cofounder_kernel.routing import route_message


# ---- spec §10 example emissions -------------------------------------------------

def test_engineering_fix_routes_build() -> None:
    decision = route_message("Fix the failing GitHub Actions tests and update the dependency.")
    assert decision.profile == "build"
    assert decision.inferred_profile == "build"
    assert "verify" in decision.workflows  # kernel auto-verify is mandatory on build


def test_step_by_step_explanation_routes_study_mentor() -> None:
    decision = route_message("Explain BGP route reflection step by step. Help me understand it.")
    assert decision.profile == "study-mentor"


def test_multi_vendor_comparison_routes_expert() -> None:
    decision = route_message(
        "Compare Cisco, Juniper, and Arista EVPN implementations using current primary sources."
    )
    assert decision.profile == "expert"
    assert "research" in decision.workflows


def test_csv_to_excel_dashboard_routes_general_with_xlsx() -> None:
    decision = route_message("Turn this CSV into a formatted Excel dashboard.")
    assert decision.profile == "general"
    assert decision.inferred_profile is None  # no opinion -> existing default chain
    assert "xlsx" in decision.skills


def test_x_reply_composition_routes_account() -> None:
    decision = route_message("Reply to this X thread in under 550 characters.")
    assert decision.profile == "account"


def test_rest_api_endpoint_routes_build_not_api() -> None:
    decision = route_message("Build a REST API endpoint for creating users and test it.")
    assert decision.profile == "build"


def test_honest_advice_routes_loyal_confidant() -> None:
    decision = route_message("Give me brutally honest advice about whether to leave this job.")
    assert decision.profile == "loyal-confidant"


# ---- precedence: explicit selection ----------------------------------------------

def test_explicit_profile_flag_wins() -> None:
    decision = route_message("--profile expert what do you make of this?")
    assert decision.profile == "expert"
    assert decision.explicit is True


def test_explicit_use_profile_phrase() -> None:
    assert route_message("use the study-mentor profile for this").profile == "study-mentor"
    assert route_message("switch to account mode").profile == "account"


def test_api_profile_is_explicit_only() -> None:
    assert route_message("use the api profile").profile == "api"
    # The word API alone must never select the api profile.
    assert route_message("debug the API client in the repo").profile == "build"
    assert route_message("what is an API?").profile == "general"


# ---- safety gate ------------------------------------------------------------------

def test_self_harm_signals_gate_to_therapeutic_support() -> None:
    decision = route_message("I can't do this anymore, I want to die")
    assert decision.profile == "therapeutic-support"
    assert decision.safety is True


def test_medical_emergency_gates_to_medical_information() -> None:
    decision = route_message("my dad has crushing chest pain and can't breathe")
    assert decision.profile == "medical-information"
    assert decision.safety is True


def test_safety_gate_precedes_engineering_keywords() -> None:
    decision = route_message("fix this: I keep thinking about suicide and the bug in my head")
    assert decision.profile == "therapeutic-support"
    assert decision.safety is True


# ---- false-positive controls (spec §8) --------------------------------------------

def test_build_a_powerpoint_is_general_plus_pptx() -> None:
    decision = route_message("Build a PowerPoint presentation about our quarterly results.")
    assert decision.profile == "general"
    assert "pptx" in decision.skills


def test_dark_mode_is_not_comedy() -> None:
    decision = route_message("add dark mode support and test the settings module")
    assert decision.profile == "build"


def test_roast_chicken_is_not_comedy() -> None:
    assert route_message("how long should I roast chicken at 400F?").profile == "general"


def test_roast_this_is_comedy() -> None:
    assert route_message("roast this LinkedIn post for me").profile == "dark-comedian"


def test_sick_of_is_not_medical() -> None:
    assert route_message("I'm sick of this error").profile == "general"


def test_symptoms_route_medical() -> None:
    decision = route_message("I've had a fever and a rash for three days, should I worry?")
    assert decision.profile == "medical-information"


def test_user_account_bug_is_build_not_account() -> None:
    assert route_message("fix the user account bug in the login module").profile == "build"


def test_teach_me_python_is_study_mentor_not_build() -> None:
    assert route_message("Teach me Python").profile == "study-mentor"


def test_fix_python_service_is_build() -> None:
    assert route_message("Fix this Python service, it keeps crashing").profile == "build"


def test_relationship_advice_is_confidant_not_companion() -> None:
    assert route_message("I need relationship advice about my partner").profile == "loyal-confidant"


def test_explicit_roleplay_routes_companion() -> None:
    assert route_message("let's do a romantic roleplay, be my fictional boyfriend").profile == "companion"


# ---- thresholds and conflicts ------------------------------------------------------

def test_simple_lookup_stays_general() -> None:
    assert route_message("What is a repository?").profile == "general"


def test_research_which_is_best_routes_expert() -> None:
    assert route_message("Research which parser library is best").profile == "expert"


def test_research_in_service_of_a_fix_stays_build() -> None:
    decision = route_message("Research this library so you can fix the bug in the repo")
    assert decision.profile == "build"


def test_software_build_request_still_routes_build() -> None:
    # The kernel's established app-build inference is preserved.
    decision = route_message(
        "Build this SaaS app so it can ship on Google Play and the Apple App Store."
    )
    assert decision.profile == "build"


def test_empty_message_is_general_no_opinion() -> None:
    decision = route_message("")
    assert decision.profile == "general"
    assert decision.inferred_profile is None


# ---- workflows and skills -----------------------------------------------------------

def test_memory_commands_flag_memory_workflow() -> None:
    assert "memory" in route_message("remember this: we chose op-sqlite").workflows


def test_unsupported_workflows_are_recorded_not_faked() -> None:
    decision = route_message("fix the deploy pipeline and notify me when CI passes")
    assert decision.profile == "build"
    assert "monitor" in decision.unsupported_workflows


def test_skill_hints_from_file_extensions() -> None:
    assert "pdf" in route_message("summarize this report.pdf").skills
    assert "docx" in route_message("edit the tracked changes in proposal.docx").skills
    assert "xlsx" in route_message("clean up data.csv and calculate totals").skills


def test_decision_summary_is_json_shaped() -> None:
    summary = route_message("fix the failing test in the repo").summary()
    assert summary["profile"] == "build"
    assert isinstance(summary["signals"], list)
    assert isinstance(summary["score"], int)
