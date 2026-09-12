# 0004 — The controller owns the lifecycle; the implementer agent only proposes changes

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

The core factory loop is `commit → pipeline → wait → fix`, possibly repeated,
with human approval and review in between. Hiding that loop inside a single
long-running `Agent.run` call makes crashes, retries, timeouts, cancellations,
and observability unmanageable: if the process dies mid-loop, the state of the
run is unknown; if the loop waits an hour for CI, a worker is occupied for an
hour; and "the agent decided to stop" is not an auditable lifecycle.

Two clarifications prevent common misreadings:

- Waiting must not consume compute: a run waiting for CI or for a human must
  release its worker.
- Cycle limits must count real work, not model invocations: reads, planning,
  tool use, and readonly reviews are all model calls, but they are not commit
  attempts.

## Decision

A typed, durable **controller** owns the run lifecycle. Agents (planner,
proposer, reviewer) are functions the controller calls; they propose changes
but never advance state on their own. Every transition is persisted
separately and is individually observable and resumable.

The run state machine:

```text
accepted → preflight → planning → waiting_approval
  → proposing → validating → committing → ensuring_draft_mr
  → waiting_ci → evaluating_ci
       ├─ code_failure          → proposing_repair → validating → committing → waiting_ci
       ├─ infrastructure_failure → blocked_infrastructure
       └─ checks_passed         → reviewing → ready_for_human

At any valid point: blocked / failed / cancellation_requested / cancelled.
```

Operational rules:

- Long waits (CI, human gate) free the worker. A worker executes one bounded
  step; it never holds a coroutine for the entire run.
- Webhooks accelerate transitions; a periodic reconciler closes transitions
  whose notifications were lost ([ADR-0005](0005-durable-execution-and-unknown-outcome.md)).
- Wait timeouts are durable timestamps evaluated by the controller, not
  in-process timers.

Budget semantics: `max_commit_cycles=3` means one initial candidate commit
plus at most two code-repair commits. It is **not** a model-call budget.
Model calls, input/output tokens, wall-clock time, cost, pipeline attempts,
and work-in-progress are limited separately
([ADR-0013](0013-budgets-and-usage-ledger-in-core.md)).

## Consequences

- **Positive:** runs are crash-safe, observable, cancellable, and bounded;
  repair loops cannot run away; waiting is cheap; every state change has an
  audit trail.
- **Negative:** more moving parts than a single agent loop — each transition
  needs durable state, and lost notifications need reconciliation; the
  controller is real infrastructure, not glue code.

## Amendment (2026-09-12, during M1 implementation)

The reference graph above names three auxiliary states that the v1
implementation collapses into `status_reason` on a parent state, to keep
the enum and migration surface small:

- `proposing_repair` → `proposing` with reason `repair`
- `blocked_infrastructure` → `blocked` with reason `infrastructure_failure`
- `cancellation_requested` → `cancelled` (transitions are checked before
  side effects; an in-flight HTTP call is reconciled per [ADR-0005],
  not modelled as a separate waiting state)

If finer-grained observability is needed later this is an enum + graph +
CHECK migration, not a semantic change.
