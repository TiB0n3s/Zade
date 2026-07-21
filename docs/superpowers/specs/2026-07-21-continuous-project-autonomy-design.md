# Continuous Project Autonomy Design

## Goal

Make MVP completion a verified delivery milestone rather than an autonomy stop condition. Zade must continue each registered project through all remaining documented, internally executable work and pause only at a real founder-controlled external boundary.

## Scope

This changes the LocalAICofounder project-intake autonomy lifecycle. It applies to every registered project, including Same Ground and The Dark Index. It does not authorize production deployment, store submission, paid services, third-party account creation, credential entry, or legal/publication actions.

## Lifecycle

1. Existing MVP work is completed only with the same fresh mechanical evidence required today.
2. The completed MVP attestation is preserved in durable milestone history, including its commit, verification summary, and time.
3. Completion automatically enters a continuation-planning phase. The local planner derives the next documented internal scope after being given the completed criterion IDs and the current project documents.
4. If that scope contains safe internal criteria, the worker executes them through the existing one-criterion, clean-Git, verification, repair, and decision path.
5. When no internal criterion remains, the project becomes externally gated rather than falsely blocked. The portfolio identifies the exact outstanding external boundaries.
6. A project-document change reopens continuation planning and wakes the worker. Existing completed criteria remain history; changed or newly documented work is planned as a new continuation scope.

## Data Model

The active criterion collection remains backward compatible. Durable state gains `scope_kind` (`mvp` or `continuation`), append-only `milestones`, historical `mvp_achieved`, and `continuation_source_hash`.

`mvp_complete` remains compatible with historical data but is no longer a runnable-state veto. Portfolio projections distinguish `mvp_complete`, `continuing_delivery`, and `awaiting_external_boundary`.

## Planner Contract

For continuation, the local planner returns only explicit, not-yet-complete internal requirements. It must not reissue completed MVP criteria, invent scope, or turn an external action into a local criterion. It continues to emit typed founder decisions and approved external boundaries.

## Safety Rules

- Continuation uses the existing local-only delegation, clean Git checkpoint, protected-manifest, verification, and bounded-repair controls.
- Store submissions, production deployment, external credentials/accounts, paid commitments, legal publication, and irreversible public actions remain approval-gated.
- Replanning runs only when entering continuation or after a project-document fingerprint changes.
- No remaining internal work must produce an honest external-boundary state, never a fabricated task.

## Verification

Regression tests must prove that a completed MVP becomes a runnable continuation, its attestation remains intact, external-only continuation performs no delegation, document changes reopen continuation planning, and Same Ground/The Dark Index receive live continuation scopes without receiving external authority.
