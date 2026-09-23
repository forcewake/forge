# Synthetic test environments for multi-service systems — research (2026-09-23)

> E2E-qualification research for forge, topic 2 of 6. Sources:
> Testcontainers guides (multi-container, reuse, parallel isolation),
> systematic migration-testing write-ups (expand-contract, populated
> baselines, lock/lag budgets), broker-testing practice (Kafka/
> RabbitMQ/NATS, at-least-once failpoint testing), Docker-Compose
> ephemeral environment patterns; fetched 2026-09-23. Confidence
> marks: **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

forge's own VER-02/VER-03/VER-04 backlog line (baseline dependency
recipe, HTTP+message contract verification, real DB upgrades and
async failure semantics) is exactly this problem, and the E2E
qualification of recipes (R32-13) must run against *synthetic but
realistic* target systems: a producer/consumer pair, a populated
database baseline, a live broker. forge cannot ask design partners to
hand over production systems for qualification, so the question is
what a faithful synthetic stand-in looks like in 2026 practice — and
the answer is now highly standardized.

## Findings

### 1. Testcontainers is the standard middle layer — with known scaling rules

[documented] ([Testcontainers advanced guide](https://helpmetest.com/blog/testcontainers-advanced-guide),
[Spring Boot Testcontainers the right way](https://blog.devops-monk.com/2026/05/spring-boot-testcontainers),
[Testcontainers for integration tests](https://qajobfit.com/resources/testcontainers-for-integration-tests))

- The multi-service shape: one shared **Docker `Network`** +
  `withNetworkAliases` so containers reach each other by hostname
  (`postgres`, `redis`, `kafka`), while *host-side test code* must use
  `getHost()`/`getMappedPort()` — "confusing these two address spaces
  is one of the most common multi-container defects."
- Lifecycle economics: **static/class-scoped containers started once
  per JVM** (a shared base class), not per test class ("50 classes =
  50 Postgres containers = 20+ minutes"); isolation then comes from
  **schema-per-test / key-prefix-per-test / unique IDs per run**, not
  from container-per-test. `TRUNCATE ... CASCADE` per test or
  transactional rollback; schema-per-class avoids truncation cost.
- `.withReuse(true)` is a **development-time optimization, disabled
  in CI** — reused containers accumulate state and "can make an
  order-dependent test appear green"; "treat reuse as an optional
  developer optimization, not a CI isolation model."
- Ryuk (cleanup reaper) + dynamic port allocation make parallel suites
  safe by construction. The Docker-Compose module reuses the existing
  compose file for full topologies; microcks-style shared-collection
  fixtures report ~70 % faster execution vs per-class containers.
- Keep topologies small: "a universal base class that launches every
  service slows every test and makes failures vague."

### 2. Migration testing from populated baselines — the A0/A1/D1 matrix

[documented] ([testing DB migrations systematically](https://mironsoft.de/en/blog/sql-testing-database-migrations-systematically),
[migration testing in CI](https://technicalqa.com/qa-insights/database-migration-testing-ci-prevent-rollbacks),
[DB migration testing in CI](https://codenotes.tech/blog/database-migration-testing-in-ci),
[rollback safety matrix](https://qaskills.sh/blog/database-testing-migration-rollback-safety))

- The migration **test pyramid**: (1) syntax/idempotency on empty DB —
  every commit; (2) representative fixtures with edge rows (NULLs,
  long strings, unicode, duplicates, partially-migrated rows) — every
  migration; (3) **anonymized production-shaped snapshot** with lock
  timing, lag and rollback rehearsal — before critical migrations.
  "If your test only proves the migration runs on an empty database,
  you haven't tested the migration that will hurt you."
- The **version-compatibility matrix** is the qualification artifact:
  A0 (old app) × D0 (old schema) baseline; **A0 × D1 (expand) — the
  most valuable rollback test** ("if that works, an application
  rollback avoids another risky database operation"); A1 × D1 target;
  A1 × D0 usually unsupported (ordering prevents it); A0 × D2
  (contract) unsupported *after retirement is proven*. Expand-contract
  with **dual-write/backfill/read-switch phases tested as mixed
  states** — "that is where many production bugs hide."
- CI recipe: fresh container → apply all migrations to HEAD → load
  seed → **forward → rollback → forward** cycle → schema-diff against
  expected target state. Baselines as code: previous-schema fixture +
  old/uncomfortable rows committed as versioned assets so the same
  dataset reproduces in CI and debugging.
- Lock/lag budgets as gates: measure how long the migration held a
  blocking lock and replica lag on a production-scale snapshot;
  "if either breaks budget, the test fails and the migration does not
  ship." DDL-risk linter (DROP COLUMN, SET NOT NULL, CREATE INDEX
  without CONCURRENTLY) makes review-visible what CI cannot prove.
- Rollback honesty: "stop promising easy rollback for irreversible
  changes — promise tested compatibility, safe forward recovery, and
  observability"; destructive ops deferred to a later migration after
  stability; archive-before-drop.

### 3. Broker testing — five scenarios and deterministic failpoints

[documented] ([broker testing DX compared](https://helpmetest.com/blog/message-broker-testing-comparison),
[event-driven testing](https://yrkan.com/blog/event-driven-testing),
[at-least-once consumer testing](https://oneuptime.com/blog/post/2026-07-22-test-at-least-once-consumers/view))

- Setup cost ranking: NATS embedded (ms) < RabbitMQ container (~5 s)
  < Kafka containers (15–30 s; EmbeddedKafka diverges on rebalancing).
  **Test the guarantee the broker was chosen for** — the universal
  five: happy path; consumer crash before ack (message not lost);
  dead-letter path for poison messages; schema rejection at the right
  boundary; concurrent consumers don't double-process.
- The strongest 2026 pattern: **named deterministic failpoints**
  around the business commit and broker settlement —
  `AFTER_RECEIVE / BEFORE_BUSINESS_COMMIT / AFTER_BUSINESS_COMMIT /
  BEFORE_ACK / AFTER_ACK` — pause or kill the consumer exactly there,
  via a test barrier, not sleeps. "The most valuable case is
  AFTER_BUSINESS_COMMIT: terminate before the ack, restart, require
  redelivery — the inbox must turn the replay into a no-op."
- Assert **durable invariants, not invocation counts**:
  `delivery attempts >= 1; durable business effects == 1; eventually
  settled; unresolved checkpoint holes == 0`. "A test asserting
  `handlerInvocations == 1` rejects the very replay behavior the
  design must tolerate."
- Race duplicates directly: two workers, same stable event ID,
  barrier before the inbox insert — "the database unique constraint,
  not an in-memory mutex, must choose one winner." Negative case:
  same event ID with a different payload fingerprint must be
  quarantined as an identity collision, not swallowed as a duplicate.
- Isolation mechanics: unique topic/queue/group + UUID event IDs per
  test; per-run unique consumer groups; Awaitility-style bounded
  polling, never fixed sleeps; **Toxiproxy** between client and broker
  for latency/timeout/cut faults; verification consumer on a dedicated
  topic to assert outcomes without querying service DBs.

### 4. Compose-driven ephemeral environments — three persistence modes

[documented] ([compose test environments](https://qaskills.sh/blog/docker-compose-test-environments-health-ordering),
[reproducible integration tests](https://untied.dev/reproducible-integration-tests-with-kumo-and-docker-compose-),
[integration testing in CI](https://khimananda.com/blog/integration-testing-in-ci-pipelines))

- The CI-safe teardown default: `trap 'docker compose down --volumes
  --remove-orphans' EXIT` + `--abort-on-container-exit
  --exit-code-from tests`; "removing volumes prevents yesterday's
  migration residue from making today's test pass." Healthchecks with
  `depends_on: condition: service_healthy` gate app start on *real*
  readiness, not container start.
- **Three persistence modes**, choose deliberately: (1) fully
  ephemeral per run — CI default, clean by construction; (2)
  per-suite persistence — only when testing restart/recovery; (3)
  **fixture snapshot persistence** — seed once, snapshot the data
  directory, restore before every scenario (immutable image
  semantics; "do not clean up manually by deleting a few files").
- Seed data must **prove the reset worked**: assert emptiness before
  seeding (a guard that RAISEs if the DB is non-empty turns stale
  state into an immediate setup failure instead of a mystery
  assertion later); split **baseline seed** (environment startup) from
  **scenario data** (inside the test, so "the condition under test
  remains visible").
- External services are not reset by volume removal: unique
  namespace per run (prefix per run ID), cleanup APIs, count
  assertions per run ID. A **test-probe service** ("whose only job is
  to observe or summarize workflow state") is the recommended
  choreography layer for multi-hop async flows — tests query a clear
  deterministic end state instead of racing the system.
- Fixture snapshots as CI cache: `pg_dump --format=custom` snapshots
  restored in seconds vs minutes of SQL replay, "keyed by migration
  hash" — the populated-baseline economics that make matrix runs
  affordable.

## Concrete recommendations (ranked by effort/impact)

1. **A versioned "target-system kit" per recipe class (medium
   effort, high impact).** One compose/Testcontainers topology per
   dependency profile forge qualifies against: producer/consumer pair
   (HTTP contract lane), Postgres at a pinned baseline version with
   committed old-schema + uncomfortable-rows fixtures (migration
   lane), RabbitMQ-or-Kafka container with the five guarantee
   scenarios (broker lane). The kit is itself a versioned, tested
   artifact — the thing R32-13's "recipe qualification" runs against.
   [inference — implements VER-02]
2. **Populated-baseline migration gates with the A0/D1 rule (medium
   effort).** forge's schema-upgrade gates (alembic) get: baseline
   fixture at N-1 with edge rows → new migration → old-code smoke
   against new schema (the A0×D1 backward-compat check) →
   forward/rollback/forward cycle → schema-diff. Budget lock time and
   fail on breach. [documented pattern; implements VER-04 and the
   existing schema-upgrade-gates research]
3. **Failpoint harness for the broker lane (medium effort).**
   Implement the five named failpoints as a test-only hook in the
   sample consumer forge ships, and qualify every recipe that touches
   async semantics by killing at each failpoint and asserting the
   durable-effect invariant. This generalizes forge's existing
   SIGKILL failure-injection discipline from the controller to the
   *target system's* consumers. [documented pattern, inference
   application]
4. **Snapshot-restore seeding for matrix qualification runs (low
   effort, high leverage).** Build the populated baseline once per
   migration hash as a restorable snapshot; qualification matrix runs
   restore instead of re-seeding. Makes nightly matrix re-smokes
   (vendor-drift insurance) affordable. [documented]
5. **Schema/namespace-per-run isolation rules (low effort).** Every
   generated test resource — DB schema, topic/queue/group, run-ID
   prefix — derives from the qualification run ID; add the
   assert-empty-before-seed guard; unique consumer groups per run.
   Prevents cross-run contamination when the matrix parallelizes.
   [documented]
6. **Test-probe service as the acceptance oracle (low effort).** For
   multi-hop acceptance scenarios, ship a tiny probe that records
   observed end-state per run ID, so acceptance asserts on the
   probe's deterministic record rather than polling every service.
   Also the natural evidence source for R32-19 artifacts. [documented,
   inference application]

Relationship to existing plans: supplies the concrete environment
mechanics for VER-02/VER-03/VER-04 and the R32-13 recipe
qualification; the migration matrix and contract lane feed directly
into R32-22 (two-writer WorkPackage) compatibility evidence.
