# 0005 — Durable execution and unknown_outcome

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge performs external side effects — creating commits and merge requests —
over HTTP. HTTP calls can time out *after* the server has executed them, so
"the request failed" does not mean "nothing happened". Retrying blindly can
duplicate commits or MRs; assuming failure can lose work.

Task delivery is at-least-once in any realistic deployment: workers crash,
queues redeliver, webhooks arrive twice. The upstream queue semantics — a
non-atomic claim (fetch from the pending sorted set, then move to processing
in a separate call), immediate requeue on failure, and Redis-TTL-only
deduplication — are insufficient once the worker performs writes.

No amount of engineering makes HTTP exactly-once. The achievable goal is
idempotent command handling plus provable reconciliation of side effects.

## Decision

**Durable execution on Postgres:**

- A state transition and its outbox record are written in **one Postgres
  transaction**. Redis remains a wake-up/delivery channel only: losing its
  contents never loses business state.
- Redelivered tasks check the expected step version before executing, making
  command handling idempotent.
- Worker leases are atomic, renewed by heartbeat, and carry fencing/version
  checks; a worker that lost its lease cannot continue mutating a run.
- Durable timers (wait timeouts) live in the database, not in process memory.

**Every external action is journaled** with its intent, correlation key,
request digest, and the known remote result.

**unknown_outcome.** If `create_commit`/`create_mr` times out, the outcome is
recorded as `unknown_outcome`. The controller then reconciles *before*
retrying: did the expected branch/commit/MR appear? Retry happens only after
the uncertainty is resolved. If the result cannot be established, the run is
stopped — spawning alternative branches or duplicate MRs is explicitly
rejected.

**Honest guarantees.** No global exactly-once for HTTP is promised. Fencing
protects the controller's state; the GitLab API is not required to understand
fencing tokens. An in-flight HTTP request from a worker that lost its lease is
reconciled after the fact, not assumed to have been cancelled. The goal is
idempotent processing of commands and provable reconciliation of side
effects.

## Consequences

- **Positive:** commits and MRs are neither lost nor duplicated across the
  modeled failure set; crash windows and lost webhooks recover automatically
  or park visibly; duplicate events collapse.
- **Negative:** more infrastructure (outbox, leases, fencing, reconciliation
  passes); runs can wait in `unknown_outcome` and require operator attention
  — that is the intended trade-off against silent duplication; periodic
  reconciliation adds load proportional to event volume.
