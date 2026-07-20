# Project-Autonomy Planning Recovery Design

## Goal

Recover project intake from two planner failure modes without weakening evidence,
founder-boundary, or local-first guarantees:

1. A resolved founder decision is asked again during a later planning attempt.
2. Invalid model-generated MVP criteria escape as worker errors and retry indefinitely.

## Scope

This change covers only MVP-plan creation in `ProjectAutonomyOrchestrator` and
`ProjectMvpPlanner`, plus their tests and live-state repair. It also makes the
founder-supplied Dark Index design handoff usable as a planning source. It does
not change delegation behavior, external authority policy, or MVP completion
requirements.

## Design

### Resolved decisions are planning constraints

Before planning, the orchestrator will derive a deterministic list of accepted
founder answers from the project's `decision_applied` event history. The planner
prompt will label these as binding constraints and explicitly prohibit asking a
question whose choice is already resolved by one of them.

If the model nevertheless returns a decision request that semantically repeats
an accepted answer, the orchestrator will not create a new founder work item.
It will record a bounded planning failure with the duplicate question and the
matching accepted answer as evidence. A future retry must use the same binding
answers and cannot spam the founder with equivalent decisions.

### Invalid plans become controlled blockers

The planner remains the validation boundary for criterion IDs, sources,
dependencies, and cycles. The orchestrator will catch validation failures from
`planner.plan`, report one durable `blocked` autonomy state with the exact
validation message, and return a structured blocked result. This prevents an
unhandled worker error from being retried by the heartbeat.

The blocker explains that the local planner emitted an invalid dependency graph;
it does not fabricate or silently remove criteria. A subsequent explicit wake
can re-plan after the planner produces valid output.

### Dark Index design handoff is explicit planning input

The Dark Index archive is a founder-authored, high-fidelity Flutter UI handoff:
`The Dark Index Mobile App.zip`. Its `README.md` is the authoritative written
MVP interface specification and its prototype/design-system HTML files are
visual references. The current document scanner ignores ZIP archives, so the
planner cannot discover this material today.

The recovery will preserve the archive and place its Markdown handoff in an
eligible, source-controlled project document path. The planner will therefore
derive criteria from the exact UI scope without parsing arbitrary archive or
HTML content. The HTML prototype remains available to the implementation worker
as a visual reference, not as a source of inferred requirements.

### Live recovery

After code verification, repair only the two affected projects through existing
reporter/orchestrator APIs:

- Same Ground: clear the duplicate ABI decision state and re-run planning with
  its recorded ABI answer as a binding constraint.
- The Dark Index: clear the invalid planning failure and re-run planning with
  the recorded cloud-sync answer and the extracted UI handoff. It may proceed
  only if it returns a valid dependency graph.

No criterion, answer, completion, or approval will be forged during recovery.

## Tests

- Planner prompt test: accepted answers are marked binding and prohibit duplicate
  decision requests.
- Orchestrator test: a duplicate decision is not filed as a new founder work
  item and is reported as a controlled planning failure.
- Orchestrator test: an unknown criterion dependency becomes one durable blocked
  result rather than an uncaught worker error.
- Project-document test: the extracted Dark Index design handoff is available to
  the planner while the ZIP and prototype HTML remain non-planning artifacts.
- Existing autonomy, planner, and intake test suites remain green.

## Acceptance Criteria

- The worker never surfaces a raw `ValueError` for invalid MVP planner output.
- A previously resolved ABI or cloud-sync choice is not re-filed as a new
  founder decision for an unchanged project revision.
- The two live projects leave their current retry/error loop without claiming
  false MVP progress.
- The Dark Index MVP plan cites the founder's written UI handoff for its
  interface criteria.
