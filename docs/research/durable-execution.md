# Durable Workflow / Step Execution on PostgreSQL — Implementation Reference (2026)

Scope: rewriting a Redis-queue-based worker into a Postgres-durable step runtime for a Python/FastAPI/SQLAlchemy-async service. Everything below assumes a single PostgreSQL cluster as the source of truth (13+ features, 15+ where noted, 18+ where noted). Each section ends with its sources.

Core postulate used throughout: **Postgres is already your most reliable, most observable, transactionally-consistent component. A `SELECT ... FOR UPDATE SKIP LOCKED` loop on a table is a durable, at-least-once queue; a row is a durable timer; a unique index is a distributed mutex.** This is the same premise behind graphile-worker, pg-boss, procrastinate, River, Oban, PGMQ, and Absurd.

---

## 1. Transactional outbox + inbox

### The rule

The domain write and the event/job insert must happen in **one transaction**. If the transaction commits, the side effect *will* eventually be attempted (at-least-once). If it rolls back, the side effect never becomes visible. This removes the dual-write problem (DB committed / queue send failed, or vice versa). Staging jobs in the DB and draining them after commit is the standard cure for "job fired but the data it reads isn't committed yet" (Brandur).

### Outbox schema

```sql
CREATE TABLE outbox_events (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(), -- uuidv7() on PG18+ for index locality
    event_type     text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id   text NOT NULL,
    payload        jsonb NOT NULL,
    -- dispatch bookkeeping (lease-style, see §3)
    claimed_at     timestamptz,
    locked_until   timestamptz,
    attempts       int NOT NULL DEFAULT 0,
    processed_at   timestamptz,
    last_error     text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
-- Only unprocessed rows live in this index -> it stays small, minimal vacuum churn
CREATE INDEX outbox_unprocessed_idx
    ON outbox_events (created_at)
    WHERE processed_at IS NULL;
```

Notes:
- The partial index is the performance-critical choice: once `processed_at` is set, the row leaves the index, so the dispatcher's scan stays hot no matter how big the table grows (same trick DBOS uses for dequeue indexes).
- `event id` must be **globally unique** (UUID), not per-entity: one entity emits many events, and consumers key dedup on it.

### Dispatcher (relay) polling

Two viable transaction shapes; pick one and be consistent:

**A. Claim → deliver → mark (recommended for slow/unreliable side effects such as HTTP or email):**

```sql
-- tx 1 (short): claim a batch with a lease
UPDATE outbox_events SET claimed_at = now(), locked_until = now() + interval '60 seconds', attempts = attempts + 1
WHERE id IN (
    SELECT id FROM outbox_events
    WHERE processed_at IS NULL
      AND (locked_until IS NULL OR locked_until < now())
    ORDER BY created_at
    LIMIT 100
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
-- COMMIT, then deliver outside any DB transaction
-- tx 2: mark delivered
UPDATE outbox_events SET processed_at = now() WHERE id = ANY($ids);
```

Crash between tx1 and tx2 → lease expires → row redelivered. **At-least-once, by construction.** There is no exactly-once without an idempotent consumer (below).

**B. Lock rows, deliver inside the transaction, mark, commit** (`SELECT ... FOR UPDATE SKIP LOCKED` + send + `UPDATE ... SET processed_at` + commit). Simpler, and used widely in tutorials, but it holds row locks and an open transaction across external I/O. Acceptable only for fast, reliable, latency-capped deliveries with small batches; it was the cause of real incidents (lock contention, DB CPU burn) at Aurora scale. Do not do this for network calls with unbounded latency.

`FOR UPDATE SKIP LOCKED` semantics (official): `FOR UPDATE` takes row locks; `SKIP LOCKED` makes the statement skip rows that cannot be locked immediately instead of waiting — so N relay processes never block or double-claim each other.

### Inbox (idempotent consumers)

At-least-once means duplicates. The consumer dedups in the **same transaction as its business effect**:

```sql
CREATE TABLE inbox_events (
    event_id     uuid PRIMARY KEY,          -- outbox_events.id
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- consumer transaction:
WITH inserted AS (
    INSERT INTO inbox_events (event_id) VALUES ($1)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
)
SELECT EXISTS (SELECT 1 FROM inserted) AS is_first_delivery;
-- if false: roll back / no-op; the business effect and the inbox row commit atomically
```

Retention: a few hours to a few days is enough (you only need to outlive redelivery windows, not forever).

### Cleanup / compaction

- Delete (or archive) processed outbox rows promptly; keep rows only if you need a debugging trail. An unbounded outbox silently degrades the system: it is an *implicit queue* and needs the same monitoring as an explicit one — unsent-event average age, inflow/outflow rate, relay error rate. A documented production incident: slow outbox processing created timing gaps that let users overspend; root causes were running the relay inside every API replica and having no queue monitoring.
- Run the relay as its own deployment unit (one or two instances), not co-hosted in every API replica. API replicas scale with user load, the relay scales with write throughput — different profiles. Co-hosting 10–30 replicas polling every 25–50 ms burns DB CPU on lock management.
- Ordering is not guaranteed across relay instances or retries. If consumers need order, put a monotonically increasing `sequence` / entity version in the payload and resolve conflicts by version at the consumer.
- For very high volume: partition the outbox by range(created_at) and drop old partitions instead of DELETE (Absurd's postmortem names missing partitioning as its biggest production pain; note `DETACH PARTITION CONCURRENTLY` cannot run inside a transaction, which complicates pg_cron-driven lifecycle).

Sources: [npiontko.pro — Outbox: From Theory to Production](https://www.npiontko.pro/2025/05/19/outbox-pattern), [PostgreSQL SELECT docs (FOR UPDATE SKIP LOCKED)](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE), [Brandur — Transactionally Staged Job Drains in Postgres](https://brandur.org/job-drain), [gmhafiz — Transactional Outbox Pattern](https://www.gmhafiz.com/blog/transactional-outbox-pattern/), [Scaling the Outbox Pattern with PostgreSQL](https://oltionzefi.com/en/blog/scaling-outbox-postgres-part-1/)

---

## 2. Job queue on Postgres

### Reference schema (converged shape used by graphile-worker / procrastinate / pg-boss / PGMQ)

```sql
CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    queue         text NOT NULL DEFAULT 'default',
    task          text NOT NULL,               -- handler name
    payload       jsonb NOT NULL,
    priority      int  NOT NULL DEFAULT 0,    -- higher runs first
    run_at        timestamptz NOT NULL DEFAULT now(),  -- visible when run_at <= now()
    attempts      int NOT NULL DEFAULT 0,
    max_attempts  int NOT NULL DEFAULT 25,     -- poison-pill ceiling
    locked_by     uuid,                        -- lease owner identity (worker/task, see §3)
    locked_at     timestamptz,
    lease_expires_at timestamptz,              -- NULL = not leased
    last_error    text,
    dedup_key     text,                        -- optional one-ready-job-per-key
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- Dequeue index: partial (only ready rows are in it) and pre-sorted so the claim
-- query does no sort step (DBOS: this was their #1 CPU fix at scale)
CREATE INDEX jobs_ready_idx
    ON jobs (priority DESC, run_at)
    WHERE locked_by IS NULL;                  -- or status = 'ready' if you use a status column
-- dedup: at most one pending (never-attempted, unleased) job per key;
-- a failed job (attempts > 0) does not block deferring a fresh one
CREATE UNIQUE INDEX jobs_one_pending_idx ON jobs (dedup_key)
    WHERE dedup_key IS NOT NULL AND locked_by IS NULL AND attempts = 0;
```

### The claim query

```sql
WITH next_job AS (
    SELECT id FROM jobs
    WHERE run_at <= now()
      AND locked_by IS NULL
    ORDER BY priority DESC, run_at
    LIMIT $batch
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs j
SET locked_by = $worker_id,
    locked_at = now(),
    lease_expires_at = now() + make_interval(secs => $lease_secs),
    attempts = attempts + 1,
    updated_at = now()
FROM next_job
WHERE j.id = next_job.id
RETURNING j.id, j.task, j.payload, j.attempts, j.max_attempts;
```

This is the exact mechanism of graphile-worker (docs: `SKIP LOCKED` to find jobs, `LISTEN/NOTIFY` for ~3 ms enqueue→execute latency, up to ~10,000 jobs/s) and of procrastinate (Python; claims with `SELECT ... FOR UPDATE SKIP LOCKED`, state machine `todo → doing → succeeded/failed/cancelled/aborted`, one lock per job "which conveniently makes deadlocks between jobs impossible").

### Lease semantics vs visibility timeout — pick lease

- **Visibility timeout** (SQS / PGMQ `vt`): the job silently becomes fetchable again N seconds after fetch. No owner identity, no way to ask "am I still the owner?" before a side effect.
- **Lease** (graphile `locked_by/locked_at`, pg-boss fetch-lock + maintenance that fails stuck actives, procrastinate worker heartbeats): ownership is an *explicit row field with identity and expiry*, renewed by heartbeat, reclaimed by a reaper. You can (a) fence side effects on it (§3), (b) attribute stuck jobs to a specific owner, (c) distinguish "worker crashed" from "job too slow".

For a step runtime doing external writes, lease is strictly better: visibility timeouts alone re-run steps while a zombie may still be mid-flight, with nothing to detect it.

### Retries with backoff

```sql
-- on failure: schedule the retry instead of releasing immediately
UPDATE jobs
SET locked_by = NULL, locked_at = NULL, lease_expires_at = NULL,
    run_at = now() + least(
        make_interval(secs => 3600),
        (power(2, attempts) * interval '1 second') * random()  -- exponential * full jitter
    ),
    last_error = $err
WHERE id = $id;
-- when attempts >= max_attempts: move to dead letter (see §6)
```

Procrastinate formalizes this as pluggable `RetryStrategy` objects returning `RetryDecision(retry_in=..., retry_at=..., ...)`, optionally changing priority/queue/lock. Graphile marks a job permanently failed when `attempts >= max_attempts` (and exposes `permanentlyFailJobs()` for admin dead-lettering). Always add jitter: synchronized backoff across N workers produces thundering-herd retry storms (RudderStack measured a retry storm inserting 2M rows and bloating the polling loop's index).

### Priority and fairness

- `ORDER BY priority DESC, run_at` is sufficient for coarse tiers; bake `priority` into the index so the claim needs no sort node.
- Naive global ordering starves low priorities. For multi-tenant fairness, claim per-queue round-robin (rotate the queue name you claim from each tick, or `SELECT DISTINCT queue` via recursive-CTE skip-scan — Postgres has no native loose scan, RudderStack emulates `SELECT DISTINCT` with a recursive CTE) and/or cap in-flight jobs per tenant.
- Dedup ("at most one ready job per key") via partial unique index + `INSERT ... ON CONFLICT DO NOTHING`; procrastinate calls this `queueing_lock` and raises a catchable `AlreadyEnqueued`.

### LISTEN/NOTIFY

Use it for latency (ms-level pickup vs seconds-level polling), never for correctness: notifications are delivered only to connected live sessions and are lost on disconnect/restart, so a polling fallback loop is mandatory. `NOTIFY` payloads cap at ~8 KB — send only the job id (or nothing) and let the poller pick up the rest.

### Monitoring queue depth

Scrape on an interval:

```sql
SELECT count(*)                                        AS ready,
       max(now() - run_at) FILTER (WHERE run_at < now()) AS oldest_ready_age,
       count(*) FILTER (WHERE attempts > 0)             AS retrying,
       count(*) FILTER (WHERE lease_expires_at < now())  AS expired_leases
FROM jobs WHERE locked_by IS NULL;
-- plus: failed/dead counts, claim rate, completion rate (deltas of updated_at histogram)
```

Every implicit queue (outbox, timers, dead letters) needs its own depth/age gauge — this rule is enforced by at least one published outage postmortem.

### When is Redis redundant? When is it not?

**Redundant** (the common case here): jobs originate as rows in the same DB (must be transactional with domain writes), consumers already need the DB, throughput is under a few thousand jobs/s, and you want exactly one source of truth. Postgres-native queues demonstrably scale: DBOS reports >30,000 workflow executions/s (~80B/month) after fixing SKIP LOCKED, isolation levels, and indexes; RudderStack runs ~100,000 events/s with dataset-partitioned tables. Adding Redis buys a second durability domain (AOF/replication semantics weaker than WAL), non-transactional enqueue, a second ops surface, and loss of SQL observability.

**Not redundant**: sub-millisecond fan-out latency, >10k jobs/s sustained with tiny payloads, workloads with zero persistence needs (pure cache-invalidation fanout), heavy pub/sub semantics, or when Postgres is already the bottleneck for unrelated reasons and cannot be scaled.

Sources: [graphile-worker docs](https://worker.graphile.org/docs) / [jobs view](https://worker.graphile.org/docs/jobs-view) / [admin functions](https://worker.graphile.org/docs/admin-functions), [procrastinate docs](https://procrastinate.readthedocs.io/) / [retry strategies](https://procrastinate.readthedocs.io/en/stable/howto/advanced/retry.html) / [retry stalled jobs](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html), [pg-boss (npm)](https://www.npmjs.com/package/pg-boss), [DBOS — Making Postgres Queues Scale](https://www.dbos.dev/blog/making-postgres-queues-scale), [RudderStack — Scaling Postgres queues to 100k events/s](https://www.rudderstack.com/blog/scaling-postgres-queue/), [Postgres as a Queue in 2026](https://blog.rajpoot.dev/posts/postgresql/postgres-as-queue-2026), [PostgreSQL NOTIFY docs (payload limit)](https://www.postgresql.org/docs/current/sql-notify.html)

---

## 3. Fencing tokens & lease ownership

### Why a lease is not enough

A lease can expire while its holder is still running (GC pause, VM freeze, event-loop stall). The "zombie" then writes state concurrently with the new owner. Kleppmann's canonical fix: every time a lock/lease is *granted*, the grantor issues a **strictly monotonically increasing fencing token**; the storage layer rejects any write carrying a token older than the latest it has seen. With Postgres as both grantor and storage, the fence check is a CAS on the same row — you get zombie prevention without a new system.

### Concrete schema on the run/task row

```sql
ALTER TABLE runs ADD COLUMN fence          bigint NOT NULL DEFAULT 0;  -- monotonic per row
ALTER TABLE runs ADD COLUMN lease_owner    uuid;                       -- worker/task identity
ALTER TABLE runs ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE runs ADD CONSTRAINT lease_shape_chk CHECK (
    (lease_owner IS NULL AND lease_expires_at IS NULL)
    OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
);
```

Per-row `fence` (not a global sequence) is sufficient and race-free because it is only ever incremented by the same CAS that grants the lease — Postgres row-level locking serializes granters. Use a global sequence (or a counter table) only if a fence must be compared across different rows.

### Acquire (with fencing)

```sql
UPDATE runs
SET lease_owner = $worker_id,
    lease_expires_at = now() + make_interval(secs => $lease_secs),
    fence = fence + 1
WHERE id = $run_id
  AND (lease_expires_at IS NULL OR lease_expires_at < now() OR lease_owner = $worker_id)
RETURNING fence;
-- 0 rows = someone else holds a live lease -> do not run
```

Renewal-after-pause by the *same* owner is allowed by the `lease_owner = $worker_id` branch, but it **increments the fence**, so any in-flight writes the paused self made with the old fence are rejected — exactly the semantics you want.

### Heartbeat: per-task, not per-worker

Renew the lease of the specific run you are executing, on an interval (procrastinate defaults: heartbeat every 10 s; a worker with no heartbeat for 30 s is "stalled", and a periodic `retry_stalled_jobs` task requeues its jobs — this is per-job recovery driven by per-worker heartbeats plus per-task claims):

```sql
UPDATE runs SET lease_expires_at = now() + make_interval(secs => $lease_secs)
WHERE id = $run_id AND lease_owner = $worker_id AND fence = $my_fence;
-- 0 rows = lease lost/expired/reassigned -> abort the step immediately
```

A single global "worker heartbeat" cannot stop a zombie: worker W1 can be alive-and-heartbeating while its lease on run R was already taken over by W2 because W1's *task-level* renewal stalled. Renewal must be bound to the task.

### Check the fence before (and during) side effects

```sql
-- gate every DB-visible side effect on ownership:
UPDATE runs SET status = 'succeeded', output = $out
WHERE id = $run_id AND fence = $my_fence AND lease_owner = $worker_id;
-- rowcount == 0 -> raise LostLease; discard or compensate
```

For side effects **outside** the DB (HTTP calls), you cannot fence the call itself — you make it *replay-safe* instead: scope the outbound idempotency key to the fence/attempt (§7), e.g. `f"{run_id}:{step_key}:{fence}"`. If a zombie and the new owner both fire the call, they carry different keys; the new owner's key is the only one whose result gets committed, and the zombie's commit to Postgres fails the fence CAS afterward. Anything the zombie's call did server-side is either deduped by the API (same key would be needed — it differs) or is handled by reconciliation (§6).

### Expiry races and what actually protects you

There is a window where two owners both believe they hold run R (old owner hasn't noticed expiry; new owner just took over). Fencing does not prevent both from *starting* work; it guarantees only one of them can *commit*. Acceptable ordering: grant → old owner loses on next CAS. If a side effect is truly un-compensatable and not idempotency-keyable, block it behind a DB-write first (e.g., reserve the external slot in a row protected by the fence, call, then commit the result) so the fence check is provably before the irreversible action.

Related tool: `pg_advisory_xact_lock(key)` for single-flight sections inside one transaction (transaction-scoped, auto-released). Procrastinate's design keeps one job lock per transaction, which makes deadlocks structurally impossible — adopt the same discipline (never take two run locks in one transaction, or always take them in a fixed global order).

Sources: [Kleppmann — How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html), [jolynch — Distributed Systems Shibboleths (attach fencing/idempotency tokens to leases)](https://jolynch.github.io/posts/distsys_shibboleths/), [procrastinate — Retry stalled jobs](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html), [PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS), [Temporal internals — shard range_id fencing](https://backend.how/posts/temporal-under-the-hood/) (Temporal fences stale writers with a `range_id` bumped under `FOR SHARE` — same idea at cluster scale)

---

## 4. DB invariants: partial unique indexes, ON CONFLICT, gates

### One active row per key (one active run per issue)

```sql
CREATE UNIQUE INDEX runs_one_active_per_issue
    ON runs (issue_id)
    WHERE status IN ('pending', 'running');
```

Gotchas (all documented in the wild):
- **NULLs are distinct**: `UNIQUE (issue_id, archived_at)` does *not* enforce "one active row" because every `NULL archived_at` is distinct from every other. Either use the partial index above (`WHERE archived_at IS NULL`), or PG15+ `UNIQUE NULLS NOT DISTINCT` — but then *all* columns become NULL-strict, which is usually wrong. Prefer the partial index.
- **ON CONFLICT must match the partial predicate**: `INSERT ... ON CONFLICT (issue_id) DO NOTHING` will not target a partial index unless you repeat the predicate: `ON CONFLICT (issue_id) WHERE status IN ('pending','running') DO NOTHING`. Without it you get a plain unique-violation error instead of a no-op — either handle `UniqueViolationError` explicitly (that's the idempotency mechanism, below) or write the full inference.
- **The index enforces at commit, not at INSERT time**: two concurrent transactions can both INSERT and one fails at commit with a unique violation. Treat `UniqueViolationError` as a first-class control-flow signal: catch → re-read the existing row → join it or abort your attempt.
- Negative-test both directions: duplicate active rows must be rejected, and *inactive* rows must still be allowed to duplicate.

### Unique violation / ON CONFLICT as idempotency mechanism

```python
try:
    await session.execute(
        insert(Run).values(run_id=rid, issue_id=issue, status="pending", ...)
        .on_conflict_do_nothing(index_elements=["run_id"])
    )
    await session.commit()
except UniqueViolationError:
    existing = await get_run(rid)   # find-then-act
```

Pattern: **state-changing endpoints/jobs create-then-find, never find-then-create blindly.** The INSERT is the arbiter; the unique index is the mutex. (Stripe does exactly this internally with `(user_id, idempotency_key)`.)

### Gate generation (monotonic per-key counters)

```sql
CREATE TABLE gates (
    run_id     uuid NOT NULL,
    name       text NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, name)
);

-- advance a gate; the returned generation fences everything downstream of it:
UPDATE gates SET generation = generation + 1
WHERE run_id = $run_id AND name = 'approval'
RETURNING generation;
```

Use for "the invariant may be re-checked and the counter advances" cases (retry generations, approval rounds, requeue epochs): consumers record `seen_generation` and ignore anything stale. For "the gate may only ever pass once" use a partial unique index instead (a `UNIQUE (run_id, gate_name) WHERE passed` table, or an `INSERT ... ON CONFLICT DO NOTHING RETURNING` as the mutex).

Sources: [PostgreSQL INSERT ... ON CONFLICT (index inference incl. partial-index predicate)](https://www.postgresql.org/docs/current/sql-insert.html), [Queryplane — ON CONFLICT in practice (partial-index target gotcha)](https://queryplane.com/blog/postgres-upsert/), [Stack Overflow — enforce one active row per user (concurrency + partial unique)](https://stackoverflow.com/questions/79908535/how-do-you-correctly-enforce-only-one-active-row-per-user-with-doctrine-and-post), [QA Skills — partial unique index negative tests](https://qaskills.sh/blog/partial-unique-index-negative-tests-soft-delete), [Brandur — Stripe-like idempotency keys (unique index as arbiter)](https://brandur.org/idempotency-keys)

---

## 5. Crash recovery: recoverable step design

### Persist step inputs/outputs (checkpoint replay, not deterministic replay)

```sql
CREATE TABLE run_steps (
    run_id      uuid NOT NULL REFERENCES runs(id),
    step_key    text NOT NULL,                 -- stable, code-level name
    status      text NOT NULL DEFAULT 'pending', -- pending|running|succeeded|failed|dead
    input       jsonb,
    output      jsonb,
    error       jsonb,
    attempts    int NOT NULL DEFAULT 0,
    started_at  timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (run_id, step_key)
);
```

Two schools:

- **Deterministic replay** (Temporal): store the event history, re-execute workflow code against it; the code must be deterministic. Buys signals/timers/versioning machinery; costs determinism discipline and heavy per-activity write amplification (~4 framing + 6 events per activity).
- **Checkpoint replay** (Absurd — 5 months of production use): each completed step's output is cached; on crash, resume from the last completed step, loading outputs and skipping. **Code between steps may be non-deterministic** (random, now(), direct calls). This is the right fit for a hand-rolled Python runtime — no determinism linter needed, and the resume path is just "find-then-skip".

Step execution protocol (find-then-create, race-safe):

```python
res = await tx.execute(
    text("""INSERT INTO run_steps (run_id, step_key, status, input, attempts, started_at)
             VALUES (:run, :key, 'running', :inp, 1, now())
             ON CONFLICT (run_id, step_key) DO NOTHING RETURNING output"""),
    {...})
row = res.first()
if row is None or row.output is None:
    ... execute the step ...
else:
    return row.output           # already done — replay
```

Absurd's hardening added explicit `beginStep()/completeStep()` so a handler can *check* whether a step already ran before committing — you need the same two-phase shape whenever the step wraps an external call (write intent → call → write result, with the fence check between).

### Timers as rows, never as scans of history

```sql
CREATE TABLE timers (
    id      bigserial PRIMARY KEY,
    run_id  uuid NOT NULL,
    due_at  timestamptz NOT NULL,
    kind    text NOT NULL,               -- 'sleep' | 'deadline' | 'retry'
    payload jsonb,
    fired_at timestamptz
);
CREATE INDEX timers_due_idx ON timers (due_at) WHERE fired_at IS NULL;
```

A scheduler loop claims due timers (`WHERE due_at <= now() AND fired_at IS NULL ORDER BY due_at ... FOR UPDATE SKIP LOCKED LIMIT n`), fires them (advance run state under its fence), marks `fired_at`. This replaces scanning `runs`/history for deadlines: the due index is partial and tiny. `runs.deadline_at` (one row, run-level timeout) plus `timers` (N pending sleeps) covers both patterns; Absurd stores sleeps and event waits as rows with `expires_at` and pulls them when due.

### Poison pills / dead letter

```sql
ALTER TABLE runs ADD COLUMN dead_reason text;
ALTER TABLE runs ADD COLUMN dead_at timestamptz;

-- after a step/run failure:
UPDATE runs
SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
    dead_reason = CASE WHEN attempts >= max_attempts THEN $err ELSE NULL END,
    dead_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
    run_at = now() + <backoff>            -- still scheduled when not dead
WHERE id = $id AND fence = $fence;
```

Rules: dead rows are *kept* (they are your incident record: `last_error`, payload, attempt list); alert on `count(*) WHERE status='dead'` delta; provide a manual requeue (reset `attempts`, `status='pending'`) as the only path out of dead. Graphile's model is identical: `attempts >= max_attempts` ⇒ permanently failed.

### What must survive a crash

Worker memory contains nothing authoritative: claim state (jobs/runs rows), progress (run_steps rows), timers (timers rows). Any step not yet in `run_steps` with `succeeded` is simply re-executed after crash — which is why step handlers must be idempotent or key their external calls (§7). Timeouts as backstops: set `idle_in_transaction_session_timeout` and `statement_timeout` so a crashed client can never hold a transaction (and its locks) forever.

Sources: [Absurd in production — five months](https://news.lavx.hu/article/absurd-in-production-five-months-of-durable-execution-with-postgres), [backend.how — Temporal under the hood (event counts, replay)](https://backend.how/posts/temporal-under-the-hood/), [graphile-worker admin functions](https://worker.graphile.org/docs/admin-functions), [Postgres as a Queue in 2026](https://blog.rajpoot.dev/posts/postgresql/postgres-as-queue-2026)

---

## 6. Idempotency keys for external writes & ambiguous outcomes

### The ambiguity

Timeout on an external call is *indeterminate*, not failed: the server may have committed. Naive retry double-applies; naive give-up loses work. Two mitigations, in order of strength:

**1. Native idempotency keys (use whenever the API offers them).** Stripe: pass `Idempotency-Key`; retries with the same key within **24 hours** return the original response instead of re-executing — this is the documented, supported way to survive ambiguous timeouts. Shopify GraphQL likewise supports idempotency keys per mutation. Feed it a deterministic value you control: `hash(run_id, step_key, fence)` — stable across retries of the same attempt, different across re-leases.

```python
key = f"{run_id}:{step_key}:{fence}"          # same on retry, new after re-lease
resp = await client.post(url, json=body, headers={"Idempotency-Key": key})
```

Persist the mapping (`run_steps.output = {"provider_ref": ...}`) in the same transaction as the step result.

**2. Read-your-write reconciliation (for APIs without keys, or past the key TTL).** Allocate a client-generated identity *before* the call and make it part of the create request (e.g., your own `request_id`/UUID in the payload, or a provider field like `client_reference_id`). On timeout: backoff, then `GET`/search by that identity; found → adopt the returned id and resume; not found after the provider's consistency window → safe to re-create with the same identity. If the API offers neither keys nor a client-supplied searchable id, the write is genuinely non-idempotent: confine it to a single attempt (mark the step attempted before calling, never auto-retry it, require manual resolution) — brandur's reference implementation conservatively marks indeterminate foreign mutations failed and surfaces them.

Store a unique external reference to make reconciliation cheap and race-proof:

```sql
ALTER TABLE run_steps ADD COLUMN provider_ref text;
CREATE UNIQUE INDEX run_steps_provider_ref_idx
    ON run_steps (provider_ref) WHERE provider_ref IS NOT NULL;
```

### Server-side idempotency keys for your own mutating API (brandur's Postgres design)

If your FastAPI surface is the one receiving retried writes, the Stripe-shaped table and state machine are the reference:

```sql
CREATE TABLE idempotency_keys (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) <= 255),
    locked_at      timestamptz,
    request_method text NOT NULL,
    request_params jsonb NOT NULL,       -- to detect same key + different params
    request_path   text NOT NULL,
    response_code  int,
    response_body  jsonb,
    recovery_point text NOT NULL,        -- 'started' → phase names → 'finished'
    user_id        bigint NOT NULL,
    UNIQUE (user_id, idempotency_key)
);
```

Mechanics: lock the key on entry (only if unlocked or `locked_at` expired; concurrent in-flight request ⇒ 409; params mismatch ⇒ 409); drive the request as a sequence of *separate transactions* labeled by `recovery_point` (key upsert → domain write → foreign call → response stored); a replayed finished key returns the stored `response_code/response_body`; a `completer` process pushes abandoned in-flight requests to completion; a `reaper` deletes keys after ~24–72 h. Wrap the lock acquisition in SERIALIZABLE or accept the CAS-with-`locked_at` variant to avoid two lockers.

### What does NOT give you idempotency

GitHub GraphQL's `clientMutationId` is a Relay *echo field* — request/response correlation only; GitHub does not dedup on it. Relay-style correlation ≠ idempotency; don't build retry logic assuming the server deduped for you.

Sources: [Stripe — Idempotent requests](https://docs.stripe.com/api/idempotent_requests), [Stripe — Advanced error handling (retry within 24h, ambiguous outcomes)](https://docs.stripe.com/error-low-level), [Stripe blog — Designing robust APIs with idempotency](https://stripe.com/blog/idempotency), [Brandur — Implementing Stripe-like idempotency keys in Postgres](https://brandur.org/idempotency-keys), [Shopify — Implementing idempotency](https://shopify.dev/docs/api/usage/implementing-idempotency) / [Shopify Engineering — Resilient GraphQL APIs using idempotency](https://shopify.engineering/building-resilient-graphql-apis-using-idempotency), [Stack Overflow — clientMutationId is a Relay echo value](https://stackoverflow.com/questions/51303530/github-graphql-api-what-does-clientmutationid-mean)

---

## 7. Schema / workflow versioning of durable rows

- **Carry `schema_version int NOT NULL` on `runs`** (and a `code_version`/git-sha column for debugging). The version is stamped at creation and never mutated for the life of the run.
- **Dispatch by (workflow_type, schema_version)**: a registry maps the tuple to a handler. Startup compatibility check: every version present in `SELECT DISTINCT workflow_type, schema_version FROM runs WHERE status IN ('pending','running')` must be registered in the new deployment — refuse to start (fail fast) otherwise; a run whose version is unknown at claim time is left untouched (not failed) so the old worker can drain it.
- **Draining old versions (Temporal's model, copied):** new runs start on the current version; in-flight runs stay *pinned* to their original version; the old version enters a "draining" state and must remain deployed until `SELECT count(*) FROM runs WHERE schema_version = $old AND status IN ('pending','running') = 0`, then is retired. Practically: the claim query filters `schema_version = ANY($supported_versions)`, and you deploy old workers at reduced replica count until the pinned queue empties.
- **Migrations under live runs:** additive-first (expand/contract): add nullable columns/tables, dual-write, backfill, then drop. Never repurpose an existing column or change the shape of a stored `output` JSONB that old-version code still reads. New step keys may be added; renaming/removing step keys is a new `schema_version`.

Sources: [Temporal — Worker Versioning (pinned/draining lifecycle)](https://docs.temporal.io/worker-versioning) and [production-deployment docs](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning), [Temporal blog — worker versioning preview (keep old versions until in-flight workflows complete)](https://temporal.io/blog/announcing-worker-versioning-public-preview-pin-workflows-to-a-single-code), [Temporal community — versioning strategies](https://community.temporal.io/t/workflow-versioning-strategies/6911)

---

## 8. Async SQLAlchemy specifics (2026)

### Session-per-step; never hold a transaction across external I/O

The defining failure mode of FastAPI + SQLAlchemy workers: a session (and its pooled connection, in a transaction with row locks) held open across a slow HTTP call. At concurrency you exhaust the pool (`QueuePool limit ... overflow reached`) and, worse, hold row locks for the call's duration — which converts your own SKIP LOCKED queue into a convoy. Official docs: a single `AsyncSession` is **not safe for concurrent tasks** — one session per task.

```python
# tx 1: claim (short)
async with session_factory() as s, s.begin():
    job = (await s.execute(claim_sql, {...})).first()      # COMMIT at block end
# --- no session, no transaction here ---
result = await do_external_call(job)                        # seconds allowed
# tx 2: persist result under the fence
async with session_factory() as s, s.begin():
    n = (await s.execute(persist_sql, {..., "fence": job.fence})).rowcount
    if n == 0:
        raise LostLease(...)
```

Use `async_sessionmaker(engine, expire_on_commit=False)` so attributes stay readable after commit (documented requirement for async). For anything that must combine DB writes with external effects, split into claim/intent tx → call → result tx (the §3/§5 protocol).

### Engine lifecycle

- Create the engine once per process at startup (FastAPI `lifespan`), not per request/per worker loop.
- **`await engine.dispose()` on shutdown** — async engine pools must be closed with an awaitable; skipping it produces `RuntimeError: Event loop is closed` warnings at GC because SQLAlchemy cannot await from finalizers. `dispose(close=True)` closes checked-in connections.
- If an engine must ever be shared across event loops, it must use `NullPool` (documented) — better: don't share.

### Pool sizing for multi-worker deployments

Budget against Postgres `max_connections`:

```
per-process cap        = pool_size + max_overflow
total                  = processes × per-process cap (+ one-off admin/migrations) << max_connections
```

Example: 2 uvicorn workers × 1 event loop, `pool_size=10, max_overflow=10` → 40 connections + 10 headroom against a 100-connection DB. The effective concurrency limit of your step runtime *is* the pool cap: a claim task blocked waiting for a connection is a stuck worker, so size the pool to (max concurrent steps + margin), and gate step concurrency explicitly (`asyncio.Semaphore`) instead of letting it float above the pool. Add `pool_pre_ping=True` (survives DB failovers/idle kills) and `pool_recycle=300` (managed Postgres proxies kill idle connections). Behind PgBouncer transaction pooling, use `NullPool` (one pgbouncer client connection per app connection would defeat pooling). Server-side backstops: `statement_timeout`, `idle_in_transaction_session_timeout`.

asyncpg notes: use `timestamptz` columns and always pass timezone-aware datetimes (asyncpg rejects naive/aware mixing); `jsonb` maps to Python dicts without extra work.

Sources: [SQLAlchemy asyncio docs (create_async_engine, session-per-task, expire_on_commit, dispose, NullPool-across-loops)](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [SQLAlchemy connection pooling docs (pool_size/max_overflow, dispose)](https://docs.sqlalchemy.org/en/20/core/pooling.html), [FastAPI discussion — QueuePool limit exhausted (sessions held during slow work)](https://github.com/fastapi/fastapi/discussions/10450), [SO — pool_size/max_overflow for ASGI apps](https://stackoverflow.com/questions/72543167/how-to-properly-set-pool-size-and-max-overflow-in-sqlalchemy-for-asgi-app)

---

## 9. Reference architectures: Temporal vs roll-your-own on Postgres

Measured head-to-head on the same Postgres-backed hardware (backend.how, 2025): Temporal 1.28 installs **37 tables**; a 3-activity workflow touches 16 of them and executes **~145 SQL statements** (cost model ≈ 40 + 35×N; per activity: 4 fencing writes, 3 history events, 3 timer-task rows, dispatch + metadata + replay reads), storing **23 events** and ~93 MB per 5,000 workflows. Throughput saturated near **~80 workflows/s** on a 4-CPU VM with bimodal p90 (~1 s). A checkpoint-based single-file Postgres engine (Absurd, 5 tables, ~11 + 7×N statements) did the same work in ~32 statements, ~13 MB, **~1,450 tasks/s**, per-step slope ~0.2 ms vs ~8.5 ms.

What Temporal buys for that cost: event-sourced history with **deterministic replay**, durable timers/signals/child workflows/searchable visibility, worker versioning with pinned draining (§7), retries with sane defaults, multi-cluster/replication, mature Python SDK, UI, and Temporal Cloud if you don't want to run the cluster (~4 services + DB). What it costs: a cluster to operate (or Cloud spend), a determinism constraint on all workflow code, ~5× the per-step DB load, and a second mental model.

Roll-your-own on Postgres is justified when: workflows are bounded step chains (minutes-to-days, not months), state fits "steps + timers + outbox" tables, the team is small, everything already lives in one Postgres, and you are willing to own ~1,500 lines of runtime (Absurd's Python SDK is ~1,900 lines; their postmortem credits thin SDKs + SQL-resident complexity for surviving 5 months of production nearly unchanged). You must then build and monitor yourself: lease/heartbeats/reaper, dead letters, queue-depth metrics, version draining, and cleanup/partitioning (their one production regret). Community consensus matches: Temporal for genuinely long-running, signal-heavy, many-workflow-type estates; "over-engineering" for step pipelines that one good table schema handles. Middle ground if you want Temporal-style guarantees without the cluster: DBOS (Postgres-native durable workflows, >30k execs/s reported), Absurd, or procrastinate as the plain queue layer underneath your own step runner.

For this rewrite (Redis queue → durable steps on the existing Postgres): roll-your-own is the pragmatic default. Take §1–§8 as the design; adopt procrastinate only if you want a maintained queue layer under custom step logic, and reach for Temporal only if workflows grow signals, timers-heavy branching, or org-scale versioning needs.

Sources: [backend.how — Temporal under the hood](https://backend.how/posts/temporal-under-the-hood/), [Absurd in production — five months](https://news.lavx.hu/article/absurd-in-production-five-months-of-durable-execution-with-postgres), [DBOS — Making Postgres queues scale](https://www.dbos.dev/blog/making-postgres-queues-scale) and [DBOS vs Temporal](https://www.dbos.dev/compare/dbos-vs-temporal), [Temporal docs](https://docs.temporal.io/worker-versioning), [r/golang — Temporal vs job queue discussion](https://www.reddit.com/r/golang/comments/1as23yb/when_to_use_a_workflow_tool_temporal_vs_a_job/), [RudderStack — scaling Postgres queues](https://www.rudderstack.com/blog/scaling-postgres-queue/)

---

## Appendix: minimal end-to-end runtime loop (pseudocode)

```python
async def worker_tick(session_factory, worker_id):
    # 1. reclaim expired leases (reaper), 2. fire due timers, 3. claim a run
    # each in its own short transaction, all claims via FOR UPDATE SKIP LOCKED
    run = await claim_run(session_factory, worker_id, lease_secs=30)
    if run is None:
        return
    for step in workflow_steps[run.type, run.schema_version]:
        out = await get_step_output(session_factory, run.id, step.key)
        if out is not None:
            continue                                  # checkpoint replay (§5)
        heartbeat = asyncio.create_task(heartbeat_loop(session_factory, run, worker_id))
        try:
            result = await step(run)                  # external I/O here, NO session held (§8)
        except AmbiguousOutcome:
            result = await reconcile(step, run)       # §6: native key or read-your-write
        finally:
            heartbeat.cancel()
        ok = await persist_step(session_factory, run, step.key, result, fence=run.fence)  # §3 CAS
        if not ok:
            raise LostLease(run.id)
    await finish_run(session_factory, run, worker_id)     # fenced CAS; enqueue outbox rows in same tx (§1)
```

Recovery after crash: nothing in memory matters; the reaper requeues the run after lease expiry; completed steps replay from `run_steps`; the crashed step re-executes with the same idempotency key; `attempts >= max_attempts` ⇒ dead letter.
