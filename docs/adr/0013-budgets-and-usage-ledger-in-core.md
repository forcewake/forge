# 0013 — Budgets and the usage ledger are part of the core

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

A software factory spends model tokens on every read, plan, proposal, review,
and repair. Without per-call accounting, three failure modes are invisible:

- **Runaway repair loops** burn budget in proportion to a bug, not to value.
- **Failed work is real spend.** Calls that fail, are cancelled, or are
  superseded mid-run still cost money; a ledger that only records successes
  undercounts and cannot explain where budget went.
- **Provider counters do not compose naively.** Cache reads are typically
  part of the inclusive input figure, reasoning tokens may already be part of
  output, and a parent agent's summary must not be billed on top of its
  already-recorded child calls. Summing raw provider fields double-counts.

Budget enforcement also needs to see more than model capacity: a run admitted
into a long CI queue or a backlog of human reviews holds work-in-progress
without doing useful computation.

## Decision

Budgets and usage accounting are core subsystems, not observability
afterthoughts.

**Every model call is recorded** — including failed, cancelled, and
superseded attempts — with: provider, model, model version, route, provider
request ID; the run, episode, and role it belongs to; a prompt snapshot
reference; input/output token counters and cached buckets; timings; terminal
status; cost; and the rate-card version used to compute it.

**Normalization without double counting:** cache reads are part of the
inclusive input figure and are never added on top; reasoning output is not
billed twice where the provider already includes it in output; parent
summaries are not charged over already-recorded child calls. Unknown usage or
cost is recorded as *unknown*, never as zero — zero would silently falsify
totals.

**Reserve, then reconcile.** Budget is reserved before a call is made and
reconciled against actuals afterward, so a burst of parallel calls cannot
overshoot a limit between measurement points.

**Separate budgets:** limits are enforced per run, per project per day, per
provider, and on total in-flight calls; **code-repair budget and
infrastructure-retry budget are separate**, so a flaky runner does not
consume the attempts meant for fixing code (matching the failure
classification in [ADR-0008](0008-quality-contract-instead-of-pipeline-status.md)).

**Admission control** considers the CI backlog and human-review
work-in-progress — not merely a free LLM connection — before starting new
runs.

## Consequences

- **Positive:** spend is explainable per run/episode/role down to individual
  calls; repair loops and infrastructure flaps are visible and separately
  capped; parallel bursts cannot silently exceed limits; the ledger doubles
  as the audit trail for model interactions.
- **Negative:** every call pays a small accounting overhead and a reservation
  round-trip; rate cards must be maintained for cost figures (otherwise cost
  is honestly "unknown"); admission control can delay runs during CI or
  review congestion — by design.
- The budget dimensions interact with the lifecycle limits in
  [ADR-0004](0004-controller-owns-lifecycle-implementer-proposes.md) (commit
  cycles are counted separately from model calls) and with the reconciliation
  discipline in [ADR-0005](0005-durable-execution-and-unknown-outcome.md).
