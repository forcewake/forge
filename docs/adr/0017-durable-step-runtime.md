# ADR-0017: Durable step runtime (inbox/outbox as the path, not the ledger)

Status: accepted (2026-09-13)
Context: review findings F08–F12, F17; the v0.1 tables exist but the runtime
still routes through the Redis queue and imperative RunService chains.

## Decision

1. **Transactional ingress.** A webhook/command is answered `202` only
   after one transaction persists: native delivery identity, the command
   (with actor + authority decision), and the next scheduled step/outbox
   row. Redis dedup becomes an accelerator in front of that transaction, not
   the dedup authority; the no-Redis BackgroundTasks path is dev-only.
2. **Atomic step ownership.** The scheduler claims due steps with
   `FOR UPDATE SKIP LOCKED` (or conditional UPDATE on status/version),
   assigns lease owner + expiry + a monotonic fence token. External calls
   happen outside DB transactions; heartbeats renew the step's lease, not a
   worker-global key. A worker that lost ownership cannot create new effects
   — the publisher checks the fence.
3. **Every nonterminal state has a recovery.** Persisted step inputs/outputs
   plus recovery handlers for proposing / validating / committing /
   ensuring_draft_mr / evaluating_ci / reviewing / notification: a crashed
   process is replaced by any worker, the run completes or becomes
   explicitly `blocked`. Resumption looks for the already-existing effect
   before creating a new one.
4. **DB invariants.** One active run per (connection, repository, issue)
   via a partial unique index over the active statuses; one gate per
   (run, approval generation); unique step attempts. Retry delays are real
   (`next_due_at`), stored deadlines fire independently of poll outcomes.
5. **Failure-injection exit bar.** Two real worker processes on real
   Postgres; kill after every transition and after every external effect;
   the survivor converges to exactly one consistent outcome. Sequential
   fakes and SQLite remain the fast unit profile, not the proof.

## Consequences

- The Redis queue is demoted to a wake-up signal; its DLQ stops being a
  run-state authority (run state is Postgres only).
- `RunService`'s imperative chains are sliced into bounded steps with
  persisted results — the mechanical precondition for splitting contracts
  out of it later ([ADR-0019](0019-source-execution-adapters.md)).
- Temporal is explicitly out of scope until long-running multi-timer
  workflows appear; the adversarial contract suite would be identical.
