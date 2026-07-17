"""Lexical keyword-to-profile router — the routing layer from the founder's
"Zade keyword-to-step-command routing breakdown" spec.

Three separate decisions, made BEFORE model-message assembly:

  PRIMARY PROFILE   — which Zade operating mode controls behavior
  CAPABILITY/SKILL  — which specialized skill the request needs (hints)
  EXECUTION WORKFLOW — how the task must be carried out (flags)

The router emits only validated profile IDs, skill hints, and workflow flags.
Raw user text stays in the user message; nothing here is pasted into prompts.

Precedence (spec §7):
  1. safety / policy gate (medical emergency, imminent self-harm)
  2. explicit profile selection (--profile X, "use the X profile", ...)
  3. hard identifiers (file extensions -> skill hints; they do not change
     the profile by themselves)
  4. high-confidence intent phrases
  5. action-verb + domain-object pairs
  6. workflow modifiers
  7. default to general (emitted as "no opinion" so the runtime's existing
     request-param -> session-metadata -> configured-default chain decides)

Scoring (spec §9, with one documented harmonization): the spec's own routing
tables require a single engineering action+object pair ("fix this failing
test") to route build, but its table weight for a pair (+45) sits below its
own threshold (60). Pairs are therefore weighted as high-confidence intent
(+70) and each ADDITIONAL pair as a supporting signal (+20), which makes the
spec's worked examples and its scoring model agree.

Workflows are mapped to what THIS runtime actually implements (spec §7.7):
delegation lane, research route, memory commands, kernel auto-verify.
Claude-runtime-only workflows (todo, plan mode, subagent parallelism,
monitor, schedule, background) are DETECTED but emitted under
``unsupported_workflows`` — recorded honestly, never faked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PROFILE_THRESHOLD = 60
MIN_LEAD = 15

# Profiles that require explicit intent and never route on ambient keywords
# (spec §9 exceptions). medical/therapeutic route on domain evidence instead.
_EXPLICIT_ONLY = {"api"}
_EXPLICIT_INTENT_GATED = {"account", "companion", "dark-comedian"}

_VALID_PROFILES = (
    "general",
    "build",
    "expert",
    "account",
    "api",
    "companion",
    "dark-comedian",
    "loyal-confidant",
    "study-mentor",
    "medical-information",
    "therapeutic-support",
)


@dataclass
class RouteDecision:
    """Normalized route emission. ``profile`` is always a valid profile id;
    ``inferred_profile`` is None when the router has no confident opinion so
    the runtime's explicit-selection chain (request param -> conversation
    metadata -> configured default) decides."""

    profile: str = "general"
    explicit: bool = False
    safety: bool = False
    score: int = 0
    runner_up: str = ""
    runner_up_score: int = 0
    signals: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    unsupported_workflows: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def inferred_profile(self) -> str | None:
        return None if self.profile == "general" and not self.explicit else self.profile

    def summary(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "explicit": self.explicit,
            "safety": self.safety,
            "score": self.score,
            "runner_up": self.runner_up,
            "runner_up_score": self.runner_up_score,
            "signals": self.signals[:12],
            "skills": self.skills,
            "workflows": self.workflows,
            "unsupported_workflows": self.unsupported_workflows,
            "reason": self.reason,
        }


# ---- 1. safety gate -----------------------------------------------------------

_MEDICAL_EMERGENCY_RE = re.compile(
    r"""(?ix)\b(?:
        crushing\s+chest\s+pain | chest\s+pain | cannot\s+breathe | can'?t\s+breathe |
        severe\s+breathing\s+difficulty | face\s+drooping | one-?sided\s+weakness |
        stroke\s+symptoms? | uncontrolled\s+bleeding | anaphylaxis | passed\s+out |
        loss\s+of\s+consciousness | severe\s+confusion | overdose | rapidly\s+worsening
    )\b"""
)
_SELF_HARM_RE = re.compile(
    r"""(?ix)\b(?:
        suicidal | suicide | self-?harm | i\s+want\s+to\s+die | kill\s+myself |
        end\s+my\s+life | hurt\s+myself
    )\b"""
)


# ---- 2. explicit profile selection --------------------------------------------

_EXPLICIT_SELECTION_RES = (
    re.compile(r"(?i)(?:--profile|/profile)\s+([\w-]+)"),
    re.compile(r"(?i)\buse\s+the\s+([\w-]+)\s+profile\b"),
    re.compile(r"(?i)\bselect\s+(?:zade\s+)?([\w-]+)\s+(?:profile|mode)\b"),
    re.compile(r"(?i)\bswitch\s+to\s+([\w-]+)\s+mode\b"),
    re.compile(r"(?i)\bload\s+the\s+([\w-]+)\s+(?:policy\s+)?profile\b"),
)

# api is explicit-only, and only for the compact policy/identity prompt —
# never from the word "API" (spec: API development routes to build).
_API_PROFILE_RE = re.compile(
    r"""(?ix)
    \buse\s+the\s+api\s+profile\b | \bzade\s+api\s+mode\b |
    \bcompact\s+zade\s+prompt\b | \bminimal\s+policy\s+and\s+identity\b |
    \blow-?token\s+system\s+prompt\b | \bapi\s+policy\s+profile\b |
    \bembed\s+zade\s+through\s+an\s+api\b
    """
)


# ---- profile signal tables -----------------------------------------------------
# Each entry: (compiled_regex, points, signal_name). Negative points are
# false-positive controls (spec §8).

def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.VERBOSE)


_ENGINEERING_ACTION_RE = _rx(
    r"""\b(?:
        fix(?:ed|ing)? | debug(?:ged|ging)? | implement(?:ed|ing)? | patch(?:ed|ing)? |
        refactor(?:ed|ing)? | optimi[sz]e[d]? | migrat(?:e|ed|ing) | integrat(?:e|ed|ing) |
        configur(?:e|ed|ing) | deploy(?:ed|ing)? | review(?:ed|ing)? | audit(?:ed|ing)? |
        test(?:ed|ing)? | compil(?:e|ed|ing) | reproduc(?:e|ed|ing) | trac(?:e|ed|ing) |
        benchmark(?:ed|ing)? | commit(?:ted|ting)? | push(?:ed|ing)? | merg(?:e|ed|ing) |
        rebas(?:e|ed|ing) | revert(?:ed|ing)? | install(?:ed|ing)? | upgrad(?:e|ed|ing) |
        downgrad(?:e|ed|ing) | resolv(?:e|ed|ing) | investigat(?:e|ed|ing)
    )\b"""
)
_ENGINEERING_OBJECT_RE = _rx(
    r"""\b(?:
        repositor(?:y|ies) | repo | codebase | source\s+code | branch | commits? |
        pull\s+requests? | PRs? | issues? | bugs? | stack\s+traces? | exceptions? |
        errors? | failing\s+tests? | test\s+suites? | CI | pipelines? | GitHub\s+Actions |
        modules? | packages? | dependenc(?:y|ies) | librar(?:y|ies) | functions? |
        class(?:es)? | endpoints? | schemas? | database\s+migrations? | Dockerfile |
        containers? | Kubernetes | Terraform | Ansible | Makefile | npm | pip | cargo |
        compilers? | linters? | type\s+checkers? | runtime | servers? | services? |
        daemons? | logs? | algorithms? | scripts? | parsers?
    )\b"""
)
# The kernel's established software-build inference (app/product shapes) —
# preserved from the previous _SOFTWARE_BUILD_*_RE pair.
_SOFTWARE_BUILD_ACTION_RE = _rx(
    r"\b(?:build(?:\s+out)?|create|develop|implement|code|ship|make|design)\b"
)
_SOFTWARE_BUILD_SUBJECT_RE = _rx(
    r"""\b(?:
        mobile\s+app | web\s+app | phone\s+app | ios | android | iphone | app\s+store |
        apple\s+store | google\s+play | application | app | software | frontend |
        backend | api | react\s+native | expo | flutter |
        saas\s+(?:app|application|product|platform|tool|software)
    )\b"""
)
# "build a presentation/deck/spreadsheet/budget/PDF/workout" is NOT engineering.
_BUILD_NON_ENGINEERING_RE = _rx(
    r"""\bbuild\s+(?:me\s+)?(?:a|an|the|this)?\s*
        (?:presentation|slide|deck|powerpoint|spreadsheet|budget|pdf|guide|workout|
           plan|report|memo|document)\b"""
)

_EXPERT_PHRASE_RE = _rx(
    r"""\b(?:
        deep\s+research | research\s+this | fact-?check | verify\s+this\s+claim |
        corroborate | due\s+diligence | literature\s+review | evidence\s+review |
        source\s+analysis | market\s+analysis | competitive\s+analysis |
        benchmark\s+options | current\s+evidence | latest\s+evidence |
        primary\s+sources | multiple\s+sources | independent\s+sources |
        competing\s+claims | both\s+sides | different\s+perspectives |
        resolve\s+contradictions | synthesi[sz]e | compare | evaluate
    )\b"""
)
_EXPERT_JUDGMENT_RE = _rx(r"\bwhich\s+.{0,50}\s+is\s+(?:best|better)\b|\bbest\s+option\b")
_EXPERT_SUPPORT_RE = _rx(
    r"""\b(?:
        comprehensive | exhaustive | multi-?source | independent\s+analysis |
        parallel\s+research | separate\s+lines\s+of\s+inquiry | disputed\s+claim |
        controversial\s+claim
    )\b"""
)
_EXPERT_NEGATIVE_RE = _rx(r"^\s*(?:what\s+is|define|give\s+me\s+a\s+quick\s+fact|explain\s+simply)\b")

_ACCOUNT_INTENT_RE = _rx(
    r"""\b(?:
        reply\s+to\s+(?:this\s+)?(?:x\s+)?(?:post|tweet|thread) |
        respond\s+to\s+(?:this\s+)?(?:post|thread|@\w+) |
        draft\s+a\s+reply | (?:write\s+a\s+)?quote-?tweet |
        write\s+a\s+response\s+to\s+this\s+thread |
        make\s+this\s+an\s+x\s+reply |
        reply\s+to\s+post\s+id
    )\b"""
)
_ACCOUNT_CONTEXT_RE = _rx(
    r"\b(?:x\.com/|twitter\.com/|tweet\s+id|post\s+id|quoted\s+(?:post|tweet)|under\s+550)"
)

_COMPANION_INTENT_RE = _rx(
    r"""\b(?:
        romantic\s+roleplay | roleplay\s+as\s+my\s+(?:partner|boyfriend|girlfriend) |
        flirt\s+with\s+me | (?:start\s+a\s+)?date\s+scene |
        (?:slow-?burn\s+)?romance\s+scene | be\s+my\s+fictional |
        consensual\s+dominant\s+roleplay | continue\s+our\s+romantic\s+scene |
        make\s+this\s+scene\s+more\s+intimate
    )\b"""
)

_COMEDIAN_INTENT_RE = _rx(
    r"""\b(?:
        roast\s+(?:this|me|him|her|them|it) | dark\s+joke | deadpan\s+joke |
        make\s+this\s+funny | satiri[sz]e | mock\s+this\s+argument |
        sharp\s+punchline | dark\s+comedy | make\s+it\s+more\s+biting
    )\b"""
)
_COMEDIAN_NEGATIVE_RE = _rx(
    r"\b(?:dark\s+mode|roast\s+(?:chicken|beef|pork|turkey|coffee|vegetables?|potatoes?))\b"
)

_CONFIDANT_STRONG_RE = _rx(
    r"""\b(?:
        (?:brutally\s+)?honest\s+advice | tell\s+me\s+the\s+truth | i\s+need\s+to\s+vent |
        help\s+me\s+think\s+this\s+through | help\s+me\s+decide |
        am\s+i\s+being\s+unreasonable | am\s+i\s+fooling\s+myself |
        second\s+perspective | what\s+can\s+i\s+control |
        relationship\s+advice | life\s+advice | career\s+decision |
        family\s+conflict | friendship\s+problem
    )\b"""
)
_CONFIDANT_SUPPORT_RE = _rx(r"\bwhat\s+should\s+i\s+do\b")

_MENTOR_STRONG_RE = _rx(
    r"""\b(?:
        teach\s+me | tutor\s+me | walk\s+me\s+through | help\s+me\s+understand |
        derive\s+this | prove\s+this | solve\s+this\s+equation | quiz\s+me |
        (?:make\s+a\s+)?study\s+guide | identify\s+my\s+mistake |
        check\s+my\s+reasoning | explain\s+this\s+concept | explain\s+why |
        explain\s+how\s+(?:this|it)\s+works
    )\b"""
)
# "step by step" is a formatting signal (spec: not a profile override); it
# supports study-mentor only alongside a teaching verb.
_MENTOR_TEACHING_VERB_RE = _rx(r"\b(?:explain|teach|show|walk|derive|prove|understand)\b")
_STEP_BY_STEP_RE = _rx(r"\bstep\s+by\s+step\b")

_MEDICAL_TERM_RE = _rx(
    r"""\b(?:
        symptoms? | fever | rash(?:es)? | swelling | injur(?:y|ies) | bleeding |
        infections? | medications? | medicines? | dos(?:e|age) | side\s+effects? |
        drug\s+interactions? | lab\s+results? | blood\s+tests? | pregnan(?:t|cy) |
        diagnos(?:is|es) | urgent\s+care | emergency\s+room | allerg(?:y|ies|ic) |
        blood\s+pressure | heart\s+rate
    )\b"""
)
_MEDICAL_PAIN_RE = _rx(r"\b(?:pain|hurts?|ache|aching|sore)\b")
_MEDICAL_NEGATIVE_RE = _rx(r"\bsick\s+(?:of|and\s+tired)\b")

_THERAPEUTIC_STRONG_RE = _rx(
    r"""\b(?:
        panic(?:\s+attacks?)? | anxiety | depress(?:ion|ed) | trauma | grief |
        spiraling | intrusive\s+thoughts? | grounding\s+exercises? |
        help\s+me\s+calm\s+down | emotional\s+regulation |
        i\s+can(?:no|')t\s+cope | i\s+can(?:no|')t\s+function | i\s+feel\s+hopeless |
        overwhelmed
    )\b"""
)


# ---- skill hints (spec §4) ------------------------------------------------------

_SKILL_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("docx", _rx(r"\.docx\b|\.dotx\b|\bword\s+(?:document|doc|template|file)\b|\bmicrosoft\s+word\b|\btracked\s+changes\b")),
    ("pdf", _rx(r"\.pdf\b|\bpdf\b")),
    ("pptx", _rx(r"\.pptx\b|\bpowerpoint\b|\bslide\s+deck\b|\bslides\b|\bpresentation\b|\bpitch\s+deck\b|\bspeaker\s+notes\b")),
    ("xlsx", _rx(r"\.xlsx\b|\.xlsm\b|\.csv\b|\.tsv\b|\bcsv\b|\btsv\b|\bspreadsheet\b|\bexcel\b")),
    ("ffmpeg", _rx(r"""\b(?:combine\s+(?:these\s+)?videos?|merge\s+(?:my\s+)?clips|join\s+these\s+videos|stitch\s+the\s+clips|concatenate\s+video|compress\s+video|trim\s+video|resize\s+video|extract\s+audio|replace\s+audio|remove\s+audio|mute\s+video|make\s+a\s+gif|add\s+subtitles|change\s+codec|ffmpeg)\b""")),
    ("skill-creator", _rx(r"\b(?:create\s+a\s+skill|make\s+a\s+skill\s+for|new\s+skill|update\s+this\s+skill|edit\s+this\s+skill|skill\.md\s+format|skill\s+format)\b")),
)


# ---- workflow flags (spec §6, mapped to kernel reality) --------------------------

_WORKFLOW_MEMORY_RE = _rx(
    r"""\b(?:remember\s+(?:this|that|when)|what\s+did\s+we\s+decide|save\s+this\s+decision|
        store\s+this\s+convention|forget\s+(?:this|that)|remove\s+that\s+memory|
        delete\s+the\s+saved|stop\s+remembering|recall\s+the\s+earlier)\b"""
)
_WORKFLOW_RESEARCH_RE = _rx(r"\b(?:research|investigate|find\s+sources|verify\s+online|look\s+up)\b")
_WORKFLOW_VERIFY_RE = _rx(r"\b(?:make\s+sure\s+it\s+works|verify|test\s+it|validate|double-?check|prove\s+it|run\s+the\s+tests)\b")
_UNSUPPORTED_WORKFLOW_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("parallel-delegation", _rx(r"\b(?:in\s+parallel|use\s+multiple\s+agents|separate\s+research\s+streams)\b")),
    ("monitor", _rx(r"\b(?:tail\s+the\s+logs|stream\s+events|notify\s+me\s+when|watch\s+for\s+changes|monitor\s+until)\b")),
    ("schedule", _rx(r"\b(?:every\s+(?:five\s+minutes|hour|day)|daily|weekly|recurring|run\s+periodically|schedule\s+this)\b")),
)


def _score_profiles(text: str) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    scores: dict[str, int] = {p: 0 for p in _VALID_PROFILES}
    signals: dict[str, list[dict[str, Any]]] = {p: [] for p in _VALID_PROFILES}

    def add(profile: str, points: int, name: str) -> None:
        scores[profile] += points
        signals[profile].append({"signal": name, "points": points})

    # build — engineering action + engineering object pairs
    actions = set(m.group(0).lower() for m in _ENGINEERING_ACTION_RE.finditer(text))
    objects = set(m.group(0).lower() for m in _ENGINEERING_OBJECT_RE.finditer(text))
    if actions and objects:
        add("build", 70, f"engineering pair: {sorted(actions)[0]} + {sorted(objects)[0]}")
        extra = (len(actions) - 1) + (len(objects) - 1)
        if extra > 0:
            # Additional engineering vocabulary is supporting context — it is
            # what lets "research X so you can fix the bug in the repo" beat
            # an incidental research phrase (spec conflict table).
            add("build", 10 * min(extra, 3), "additional engineering keywords")
    elif actions or objects:
        add("build", 10, "isolated engineering keyword")
    if _SOFTWARE_BUILD_ACTION_RE.search(text) and _SOFTWARE_BUILD_SUBJECT_RE.search(text):
        add("build", 70, "software build action + app subject")
    if _BUILD_NON_ENGINEERING_RE.search(text):
        add("build", -80, "non-engineering 'build a <artifact>'")

    # expert
    for m in set(m.group(0).lower() for m in _EXPERT_PHRASE_RE.finditer(text)):
        add("expert", 70 if not signals["expert"] else 20, f"research phrase: {m}")
    if not signals["expert"] and re.match(r"(?i)^\s*(?:please\s+)?research\b", text):
        # Leading "research ..." is a researched-judgment request (+45: it must
        # still lose to a concrete engineering pair — "research X so you can
        # fix the bug" stays build).
        add("expert", 45, "leading research verb")
    if _EXPERT_SUPPORT_RE.search(text):
        add("expert", 20, "research scope signal")
    if signals["expert"] and _EXPERT_JUDGMENT_RE.search(text):
        add("expert", 20, "comparative-judgment signal")
    if _EXPERT_NEGATIVE_RE.search(text):
        add("expert", -80, "simple lookup, not synthesis")

    # account — explicit composition intent required
    if _ACCOUNT_INTENT_RE.search(text):
        add("account", 70, "X reply composition intent")
        if _ACCOUNT_CONTEXT_RE.search(text):
            add("account", 20, "X context identifier")

    # companion — explicit roleplay intent required
    if _COMPANION_INTENT_RE.search(text):
        add("companion", 70, "explicit romantic roleplay intent")

    # dark-comedian — explicit humor intent required
    if _COMEDIAN_INTENT_RE.search(text):
        add("dark-comedian", 70, "explicit humor intent")
    if _COMEDIAN_NEGATIVE_RE.search(text):
        add("dark-comedian", -80, "non-comedy usage (dark mode / cooking)")

    # loyal-confidant
    for m in set(m.group(0).lower() for m in _CONFIDANT_STRONG_RE.finditer(text)):
        add("loyal-confidant", 70 if not signals["loyal-confidant"] else 20, f"confidant phrase: {m}")
    if _CONFIDANT_SUPPORT_RE.search(text):
        add("loyal-confidant", 20, "decision-support signal")

    # study-mentor
    for m in set(m.group(0).lower() for m in _MENTOR_STRONG_RE.finditer(text)):
        add("study-mentor", 70 if not signals["study-mentor"] else 20, f"teaching phrase: {m}")
    if _STEP_BY_STEP_RE.search(text) and _MENTOR_TEACHING_VERB_RE.search(text):
        add("study-mentor", 20, "step-by-step with teaching verb")

    # medical-information — domain evidence sufficient
    medical_terms = set(m.group(0).lower() for m in _MEDICAL_TERM_RE.finditer(text))
    for i, m in enumerate(sorted(medical_terms)):
        add("medical-information", 45 if i == 0 else 20, f"medical term: {m}")
    if medical_terms and _MEDICAL_PAIN_RE.search(text):
        add("medical-information", 20, "pain/symptom language")
    if _MEDICAL_NEGATIVE_RE.search(text):
        add("medical-information", -80, "metaphorical 'sick of'")

    # therapeutic-support — distress evidence sufficient
    distress = set(m.group(0).lower() for m in _THERAPEUTIC_STRONG_RE.finditer(text))
    for i, m in enumerate(sorted(distress)):
        add("therapeutic-support", 70 if i == 0 else 20, f"distress signal: {m}")

    return scores, signals


def _skill_hints(text: str) -> list[str]:
    return [name for name, rx in _SKILL_TRIGGERS if rx.search(text)]


def _workflow_flags(text: str, profile: str) -> tuple[list[str], list[str]]:
    workflows: list[str] = []
    if _WORKFLOW_MEMORY_RE.search(text):
        workflows.append("memory")
    if profile == "expert" or _WORKFLOW_RESEARCH_RE.search(text):
        workflows.append("research")
    if profile == "build":
        workflows.append("verify")  # kernel auto-verify is mandatory on build work
    elif _WORKFLOW_VERIFY_RE.search(text):
        workflows.append("verify")
    unsupported = [name for name, rx in _UNSUPPORTED_WORKFLOW_RES if rx.search(text)]
    return workflows, unsupported


def _explicit_selection(text: str) -> str | None:
    if _API_PROFILE_RE.search(text):
        return "api"
    for rx in _EXPLICIT_SELECTION_RES:
        match = rx.search(text)
        if match:
            candidate = match.group(1).strip().lower()
            if candidate in _VALID_PROFILES and candidate not in _EXPLICIT_ONLY:
                return candidate
    return None


def route_message(message: str) -> RouteDecision:
    """Route a founder message (optionally already joined with recent user
    turns for follow-up context) to a normalized RouteDecision."""
    text = re.sub(r"\s+", " ", (message or "")).strip()
    decision = RouteDecision()
    if not text:
        decision.reason = "empty message"
        return decision

    # 1. safety gate — precedes all normal routing
    if _SELF_HARM_RE.search(text):
        decision.profile = "therapeutic-support"
        decision.safety = True
        decision.score = 1000
        decision.reason = "safety gate: possible self-harm signals"
        decision.workflows = ["safety"]
        return decision
    if _MEDICAL_EMERGENCY_RE.search(text):
        decision.profile = "medical-information"
        decision.safety = True
        decision.score = 1000
        decision.reason = "safety gate: immediate-danger medical signals"
        decision.workflows = ["safety"]
        return decision

    # 2. explicit profile selection wins
    explicit = _explicit_selection(text)
    if explicit:
        decision.profile = explicit
        decision.explicit = True
        decision.score = 100
        decision.reason = f"explicit profile selection: {explicit}"
        decision.skills = _skill_hints(text)
        decision.workflows, decision.unsupported_workflows = _workflow_flags(text, explicit)
        return decision

    # 4-6. scored lexical routing
    scores, signals = _score_profiles(text)
    scores.pop("general", None)
    scores.pop("api", None)  # explicit-only, never scored in
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_profile, top_score = ranked[0]
    runner_up, runner_up_score = ranked[1] if len(ranked) > 1 else ("", 0)

    # explicit-intent-gated profiles must carry their intent signal
    if top_profile in _EXPLICIT_INTENT_GATED and not any(
        s["points"] >= 70 for s in signals[top_profile]
    ):
        top_score = 0

    if top_score >= PROFILE_THRESHOLD and (top_score - runner_up_score) >= MIN_LEAD:
        decision.profile = top_profile
        decision.score = top_score
        decision.runner_up = runner_up
        decision.runner_up_score = runner_up_score
        decision.signals = signals[top_profile]
        decision.reason = f"scored route: {top_profile} {top_score} vs {runner_up} {runner_up_score}"
    else:
        decision.profile = "general"
        decision.score = top_score
        decision.runner_up = top_profile
        decision.runner_up_score = top_score
        decision.reason = (
            f"below threshold or lead (top: {top_profile} {top_score}, "
            f"runner-up: {runner_up} {runner_up_score}) -> general"
        )

    decision.skills = _skill_hints(text)
    decision.workflows, decision.unsupported_workflows = _workflow_flags(text, decision.profile)
    return decision
