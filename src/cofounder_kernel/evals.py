from __future__ import annotations

import json
import re
import time
from typing import Any

from .config import KernelConfig
from .critic import ContrarianCritic, VERDICTS
from .db import KernelDatabase, utc_now
from .ollama import OllamaClient
from .runtime import RuntimeService


EXECUTORS = {"generate", "respond", "critic"}
CHECK_TYPES = {
    "contains",
    "contains_any",
    "not_contains",
    "regex",
    "json_parseable",
    "json_keys",
    "min_chars",
    "max_chars",
}

RESPOND_DEFAULT_OPTIONS = {"use_memory": True, "use_semantic_memory": False, "use_skills": False}
EXCERPT_CHARS = 700


DEFAULT_EVAL_CASES: list[dict[str, Any]] = [
    {
        "name": "probe-exact-ack",
        "category": "instruction_probe",
        "executor": "generate",
        "task_type": "general",
        "description": "The general model can follow an exact-output instruction.",
        "prompt": "Reply with exactly the word ACK and nothing else.",
        "checks": [{"type": "contains", "value": "ACK"}, {"type": "max_chars", "value": 20}],
    },
    {
        "name": "probe-json-object",
        "category": "instruction_probe",
        "executor": "generate",
        "task_type": "general",
        "description": "The general model can return a strict JSON object.",
        "prompt": 'Return only a JSON object with key "status" set to "ok" and key "count" set to 3. No other text.',
        "checks": [{"type": "json_parseable"}, {"type": "json_keys", "keys": ["status", "count"]}],
    },
    {
        "name": "probe-coding-function",
        "category": "instruction_probe",
        "executor": "generate",
        "task_type": "coding",
        "description": "The coding model produces the requested function.",
        "prompt": "Write a Python function named add that returns the sum of its two arguments. Return only the code.",
        "checks": [{"type": "contains", "value": "def add"}],
    },
    {
        "name": "critic-json-contract",
        "category": "critic_contract",
        "executor": "critic",
        "task_type": "reasoning",
        "description": "The reasoning model honors the contrarian JSON contract the auto pass depends on.",
        "prompt": "Should we launch the founder beta next week?",
        "draft": "Launch next week; the beta list is warm and delaying costs momentum.",
        "checks": [],
    },
    {
        "name": "respond-decision-contract",
        "category": "governed_contract",
        "executor": "respond",
        "task_type": "general",
        "description": "Governed recommendations include the decision-engine contract elements.",
        "prompt": "Should we prioritize evidence intake or product polish next?",
        "checks": [
            {"type": "contains", "value": "recommend"},
            {"type": "contains_any", "values": ["risk", "downside"]},
            {"type": "contains_any", "values": ["next action", "next step", "next move"]},
        ],
    },
    {
        "name": "respond-evidence-honesty",
        "category": "governed_contract",
        "executor": "respond",
        "task_type": "general",
        "description": "When no local evidence exists, the governed response says so instead of faking certainty.",
        "prompt": "What does our latest customer interview data say about pricing?",
        "checks": [
            {
                "type": "contains_any",
                "values": ["no local", "missing", "no evidence", "not have", "don't have", "do not have", "unknown", "next check"],
            }
        ],
    },
    {
        "name": "grounding-memory-recall",
        "category": "grounding",
        "executor": "respond",
        "task_type": "general",
        "description": "A fact seeded into local memory is recalled through the governed pipeline.",
        "prompt": "What monthly price did we record for solo founders?",
        "setup_memories": [
            {
                "kind": "decision",
                "title": "Eval grounding: solo founder pricing",
                "content": "We recorded a monthly price of $99 for solo founders.",
            }
        ],
        "checks": [{"type": "contains", "value": "$99"}],
    },
]


class EvalService:
    """Regression harness for Zade's reasoning quality.

    Runs a golden set of founder scenarios through the real model pipeline and
    grades the outputs with deterministic checks — no model judges another
    model. Each run records the active model roles and is compared against the
    previous run, so a model swap or prompt change shows up as newly failing
    cases instead of silent drift.
    """

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ollama: OllamaClient,
        runtime: RuntimeService,
        critic: ContrarianCritic,
    ):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.runtime = runtime
        self.critic = critic

    def ensure_default_cases(self) -> int:
        created = 0
        for case in DEFAULT_EVAL_CASES:
            _, was_created = self._upsert_case(case | {"metadata": {"default_seed": True}}, only_if_missing=True)
            if was_created:
                created += 1
        return created

    def upsert_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        executor = str(payload.get("executor", "generate"))
        if executor not in EXECUTORS:
            raise ValueError(f"Executor must be one of: {', '.join(sorted(EXECUTORS))}")
        checks = payload.get("checks", [])
        for check in checks:
            check_type = str(check.get("type", ""))
            if check_type not in CHECK_TYPES:
                raise ValueError(f"Unknown check type: {check_type or '(empty)'}")
        # generate/respond cases score by their checks; a check-less case would
        # score 0 forever. The critic executor injects its own contract check.
        if executor in {"generate", "respond"} and not checks:
            raise ValueError(f"{executor} eval cases require at least one check.")
        case_id, _created = self._upsert_case(payload, only_if_missing=False)
        self.db.audit(
            actor="evals",
            action="evals.case.upsert",
            target=str(payload["name"]),
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"executor": executor, "category": payload.get("category", "custom")},
        )
        return self.get_case(case_id)

    def get_case(self, case_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            raise ValueError(f"Eval case not found: {case_id}")
        return _case_from_row(row)

    def list_cases(
        self,
        *,
        category: str | None = None,
        enabled: bool | None = None,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        if names:
            clauses.append(f"name IN ({','.join('?' for _ in names)})")
            params.extend(names)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM eval_cases {where} ORDER BY category ASC, name ASC",
                params,
            ).fetchall()
        return [_case_from_row(row) for row in rows]

    def run(
        self,
        *,
        label: str = "manual",
        categories: list[str] | None = None,
        case_names: list[str] | None = None,
        max_cases: int = 50,
    ) -> dict[str, Any]:
        self.ensure_default_cases()
        cases = self.list_cases(enabled=True, names=case_names or None)
        if categories:
            cases = [case for case in cases if case["category"] in categories]
        cases = cases[:max_cases]
        previous = self._latest_run()
        started = time.perf_counter()
        results = [self._run_case(case) for case in cases]
        duration_ms = int((time.perf_counter() - started) * 1000)
        passed = sum(1 for item in results if item["status"] == "pass")
        failed = sum(1 for item in results if item["status"] == "fail")
        errors = sum(1 for item in results if item["status"] == "error")
        pass_rate = round(passed / len(results), 4) if results else 0.0
        run_id = self._persist_run(
            label=label,
            results=results,
            passed=passed,
            failed=failed,
            errors=errors,
            pass_rate=pass_rate,
            duration_ms=duration_ms,
        )
        comparison = self._compare(previous, results, pass_rate)
        self.db.audit(
            actor="evals",
            action="evals.run",
            target=label,
            permission_tier="L1_MEMORY_WRITE",
            status="ok" if not errors else "degraded",
            details={
                "run_id": run_id,
                "total": len(results),
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "pass_rate": pass_rate,
                "comparison": comparison,
            },
        )
        return {
            "run_id": run_id,
            "generated_at": utc_now(),
            "label": label,
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": pass_rate,
            "duration_ms": duration_ms,
            "model_roles": self.config.ollama.roles(),
            "results": results,
            "comparison": comparison,
        }

    def list_runs(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
            results = conn.execute(
                "SELECT * FROM eval_results WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        if not row:
            raise ValueError(f"Eval run not found: {run_id}")
        return _run_from_row(row) | {"results": [_result_from_row(item) for item in results]}

    def _run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self._seed_memories(case)
            if case["executor"] == "generate":
                text, model, checks = self._execute_generate(case)
            elif case["executor"] == "respond":
                text, model, checks = self._execute_respond(case)
            elif case["executor"] == "critic":
                text, model, checks = self._execute_critic(case)
            else:
                raise ValueError(f"Unknown executor: {case['executor']}")
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "case_id": case["id"],
                "case_name": case["name"],
                "category": case["category"],
                "executor": case["executor"],
                "status": "error",
                "score": 0.0,
                "checks": [],
                "response_excerpt": "",
                "latency_ms": latency_ms,
                "model": "",
                "error": str(exc),
            }
        latency_ms = int((time.perf_counter() - started) * 1000)
        passed_checks = sum(1 for check in checks if check["passed"])
        score = round(passed_checks / len(checks), 4) if checks else 0.0
        return {
            "case_id": case["id"],
            "case_name": case["name"],
            "category": case["category"],
            "executor": case["executor"],
            "status": "pass" if checks and passed_checks == len(checks) else "fail",
            "score": score,
            "checks": checks,
            "response_excerpt": text[:EXCERPT_CHARS],
            "latency_ms": latency_ms,
            "model": model,
            "error": "",
        }

    def _execute_generate(self, case: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        task_type = case.get("task_type") or "general"
        model = self.config.ollama.model_for_role(task_type)  # type: ignore[arg-type]
        started = time.perf_counter()
        # Evals must be a regression signal, not sampling noise: generate greedily
        # (temperature 0) with thinking off so the same case + model is reproducible.
        think = False
        generated = self.ollama.generate(
            prompt=case["prompt"],
            model=model,
            think=think,
            temperature=0.0,
            num_predict=256,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.db.record_model_call(
            operation="evals.case",
            model=generated.model,
            role=task_type,
            status="ok",
            latency_ms=latency_ms,
            prompt_chars=len(case["prompt"]),
            response_chars=len(generated.response),
            think=think,
            metadata={"case_name": case["name"]},
        )
        return generated.response, generated.model, _run_checks(generated.response, case["checks"])

    def _execute_respond(self, case: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        options = {**RESPOND_DEFAULT_OPTIONS, **case.get("respond_options", {})}
        result = self.runtime.respond(
            message=case["prompt"],
            task_type=case.get("task_type") or "general",  # type: ignore[arg-type]
            contrarian=False,
            **options,
        )
        return result["response"], result["model"], _run_checks(result["response"], case["checks"])

    def _execute_critic(self, case: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        draft = case.get("draft") or "Proceed with the current plan."
        critique = self.critic.challenge(message=case["prompt"], draft_response=draft, context={})
        contract_ok = critique.get("status") == "ok" and critique.get("verdict") in VERDICTS
        detail = critique.get("verdict") or critique.get("error", "no verdict")
        checks = [
            {
                "type": "critic_contract",
                "passed": bool(contract_ok),
                "detail": f"status={critique.get('status')}, verdict={detail}",
            }
        ]
        checks.extend(_run_checks(json.dumps(critique, sort_keys=True), case["checks"]))
        text = json.dumps(
            {key: critique.get(key) for key in ("status", "verdict", "weakest_assumption", "missing_evidence", "downside_risk", "confidence_adjustment")},
            sort_keys=True,
        )
        return text, str(critique.get("model", "")), checks

    def _seed_memories(self, case: dict[str, Any]) -> None:
        for memory in case.get("setup_memories", []):
            title = str(memory.get("title", "")).strip()
            if not title:
                continue
            existing = self.db.search_memories(title, limit=3)
            if any(record.title == title for record in existing):
                continue
            self.db.add_memory(
                kind=str(memory.get("kind", "note")),
                title=title,
                content=str(memory.get("content", "")),
                source="evals",
                metadata={"eval_case": case["name"]},
            )

    def _persist_run(
        self,
        *,
        label: str,
        results: list[dict[str, Any]],
        passed: int,
        failed: int,
        errors: int,
        pass_rate: float,
        duration_ms: int,
    ) -> int:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO eval_runs (
                  created_at, label, total, passed, failed, errors, pass_rate,
                  duration_ms, model_roles_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    label,
                    len(results),
                    passed,
                    failed,
                    errors,
                    pass_rate,
                    duration_ms,
                    json.dumps(self.config.ollama.roles(), sort_keys=True),
                    json.dumps({}, sort_keys=True),
                ),
            )
            run_id = int(cur.lastrowid)
            for item in results:
                conn.execute(
                    """
                    INSERT INTO eval_results (
                      created_at, run_id, case_id, case_name, category, executor, status,
                      score, checks_json, response_excerpt, latency_ms, model, error, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        run_id,
                        item["case_id"],
                        item["case_name"],
                        item["category"],
                        item["executor"],
                        item["status"],
                        item["score"],
                        json.dumps(item["checks"], sort_keys=True),
                        item["response_excerpt"],
                        item["latency_ms"],
                        item["model"],
                        item["error"],
                        json.dumps({}, sort_keys=True),
                    ),
                )
        return run_id

    def _latest_run(self) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            results = conn.execute(
                "SELECT case_name, status FROM eval_results WHERE run_id = ?",
                (int(row["id"]),),
            ).fetchall()
        return _run_from_row(row) | {"statuses": {str(item["case_name"]): str(item["status"]) for item in results}}

    def _compare(
        self,
        previous: dict[str, Any] | None,
        results: list[dict[str, Any]],
        pass_rate: float,
    ) -> dict[str, Any]:
        if previous is None:
            return {"first_run": True, "previous_run_id": None, "newly_failing": [], "newly_passing": []}
        prior = previous["statuses"]
        newly_failing = sorted(
            item["case_name"]
            for item in results
            if item["status"] != "pass" and prior.get(item["case_name"]) == "pass"
        )
        newly_passing = sorted(
            item["case_name"]
            for item in results
            if item["status"] == "pass" and prior.get(item["case_name"]) not in (None, "pass")
        )
        return {
            "first_run": False,
            "previous_run_id": previous["id"],
            "previous_pass_rate": previous["pass_rate"],
            "pass_rate_delta": round(pass_rate - previous["pass_rate"], 4),
            "newly_failing": newly_failing,
            "newly_passing": newly_passing,
        }

    def _upsert_case(self, payload: dict[str, Any], *, only_if_missing: bool) -> tuple[int, bool]:
        name = str(payload["name"]).strip()
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM eval_cases WHERE name = ?", (name,)).fetchone()
            if existing:
                case_id = int(existing["id"])
                if only_if_missing:
                    return case_id, False
                conn.execute(
                    """
                    UPDATE eval_cases
                    SET updated_at = ?, category = ?, executor = ?, task_type = ?, description = ?,
                        prompt = ?, draft = ?, checks_json = ?, respond_options_json = ?,
                        setup_memories_json = ?, enabled = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        payload.get("category", "custom"),
                        payload.get("executor", "generate"),
                        payload.get("task_type", "general"),
                        payload.get("description", ""),
                        payload["prompt"],
                        payload.get("draft", ""),
                        json.dumps(payload.get("checks", []), sort_keys=True),
                        json.dumps(payload.get("respond_options", {}), sort_keys=True),
                        json.dumps(payload.get("setup_memories", []), sort_keys=True),
                        int(payload.get("enabled", True)),
                        json.dumps(payload.get("metadata", {}), sort_keys=True),
                        case_id,
                    ),
                )
                return case_id, False
            cur = conn.execute(
                """
                INSERT INTO eval_cases (
                  created_at, updated_at, name, category, executor, task_type, description,
                  prompt, draft, checks_json, respond_options_json, setup_memories_json,
                  enabled, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    name,
                    payload.get("category", "custom"),
                    payload.get("executor", "generate"),
                    payload.get("task_type", "general"),
                    payload.get("description", ""),
                    payload["prompt"],
                    payload.get("draft", ""),
                    json.dumps(payload.get("checks", []), sort_keys=True),
                    json.dumps(payload.get("respond_options", {}), sort_keys=True),
                    json.dumps(payload.get("setup_memories", []), sort_keys=True),
                    int(payload.get("enabled", True)),
                    json.dumps(payload.get("metadata", {}), sort_keys=True),
                ),
            )
            return int(cur.lastrowid), True


def _run_checks(text: str, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        check_type = str(check.get("type", ""))
        passed, detail = _evaluate_check(text, check_type, check)
        results.append({"type": check_type, "passed": passed, "detail": detail})
    return results


def _evaluate_check(text: str, check_type: str, check: dict[str, Any]) -> tuple[bool, str]:
    lowered = text.lower()
    if check_type == "contains":
        value = str(check.get("value", ""))
        return value.lower() in lowered, f"contains {value!r}"
    if check_type == "contains_any":
        values = [str(item) for item in check.get("values", [])]
        hit = next((value for value in values if value.lower() in lowered), None)
        return hit is not None, f"matched {hit!r}" if hit else f"none of {values!r}"
    if check_type == "not_contains":
        value = str(check.get("value", ""))
        return value.lower() not in lowered, f"absent {value!r}"
    if check_type == "regex":
        pattern = str(check.get("pattern", ""))
        try:
            return bool(re.search(pattern, text, re.IGNORECASE)), f"regex {pattern!r}"
        except re.error as exc:
            return False, f"invalid regex: {exc}"
    if check_type == "json_parseable":
        return _extract_json(text) is not None, "json parseable"
    if check_type == "json_keys":
        data = _extract_json(text)
        keys = [str(key) for key in check.get("keys", [])]
        if not isinstance(data, dict):
            return False, "no json object"
        missing = [key for key in keys if key not in data]
        return not missing, f"missing keys {missing!r}" if missing else f"has keys {keys!r}"
    if check_type == "min_chars":
        limit = int(check.get("value", 0))
        return len(text.strip()) >= limit, f"length {len(text.strip())} >= {limit}"
    if check_type == "max_chars":
        limit = int(check.get("value", 0))
        return len(text.strip()) <= limit, f"length {len(text.strip())} <= {limit}"
    return False, f"unknown check type: {check_type}"


def _extract_json(text: str) -> Any | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _case_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["checks"] = json.loads(data.pop("checks_json") or "[]")
    data["respond_options"] = json.loads(data.pop("respond_options_json") or "{}")
    data["setup_memories"] = json.loads(data.pop("setup_memories_json") or "[]")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    data["enabled"] = bool(data["enabled"])
    return data


def _run_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["model_roles"] = json.loads(data.pop("model_roles_json") or "{}")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _result_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["checks"] = json.loads(data.pop("checks_json") or "[]")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data
