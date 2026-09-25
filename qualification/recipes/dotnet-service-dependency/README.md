# Recipe: dotnet-service-dependency/1 (R38-08, issue #309)

The SDK-pinned recipe skeleton for verifying a representative .NET
producer/consumer service pair against the dependencies a customer
actually operates: a real PostgreSQL 16 migration upgrade and a real
RabbitMQ 3 redelivery scenario, with a complete TRX inventory for
every required test project.

The runner is `src/forge/adaptive/dotnet_recipe.py`
(`python -m forge.adaptive.dotnet_recipe`); the pins live in
`manifest.json` (this directory); the frozen fixture source is
`evaluation/tested_world/dotnet-orders/`. The recipe binds its report
to the manifest digest, the fixture source digest (the test bundle),
the observed SDK version and the resolved image digests — a changed
pin, a rebuilt fixture or a moved baseline image is a different
environment and the old report no longer applies.

## The pinned contract

| Pin | Value |
|---|---|
| dotnet SDK | 10.0.200 (target framework net10.0) |
| Microsoft.NET.Test.Sdk | 18.10.1 |
| xunit | 2.9.3 |
| xunit.runner.visualstudio | 4.0.0 |
| orders-db | docker.io/library/postgres:16 @ sha256:1a43e6bb8872ccce507f8467e549a6166e8ff132ad26b49879f5ec86ee612868 |
| orders-bus | docker.io/library/rabbitmq:3-management @ sha256:8689ddfceca1ff1ecfeaa6619cb9cfc73570cf5fa022d3d904cdb065da1f925d |

Both dependency containers are DISPOSABLE: started from the pinned
digest on random loopback ports, stopped AND removed on every path
(success, failure, crash). The synthetic container-local credentials
(`forge`/`forge-recipe-local`) are provisioned per run — they gate
nothing outside the disposable container.

## The arms

1. **SDK probe** — `dotnet --version`; a missing SDK records
   `dotnet-sdk-unavailable` and the build/test arm reports every
   required report as `missing_report`. Never a synthetic green.
2. **Build/test (TRX inventory)** — `dotnet test` per required
   project with `--logger trx;LogFileName=<pinned report name>`, an
   identity sidecar (`<report>.identity.json`, the #226 contract)
   beside every report, and reconciliation through
   `forge.adaptive.qualification.reconcile_reports`: missing =
   `missing_report`, a leftover from another run = `stale_report`,
   failures counted per project — a passing first project can never
   hide a failing second.
3. **Migration (N-1 → N)** — the fixture's baseline DDL + seeded
   canary rows inside the real postgres container, then the upgrade:
   preservation via row count + per-row sha256 fingerprints over the
   preserved columns, a CONSTRAINT probe (a negative-total insert
   must be ACCEPTED on the baseline and REJECTED after the upgrade —
   not just a version field), and the region backfill check. When no
   container can run, the same SQL pair runs on sqlite, labeled
   `reference-coverage`.
4. **Redelivery** — against the real RabbitMQ (a minimal stdlib
   AMQP 0-9-1 client): a per-run durable queue, every message
   published twice (duplicate publish), one delivery consumed WITHOUT
   ack, the channel crashed after the state commit, the broker's
   redelivery observed (`redelivered=true`), exactly-once side
   effects asserted from the database. When the image cannot run, the
   arm records `image-unavailable` and the labeled in-process
   reference harness stands in.
5. **Executor isolation binding** — the tool invocations run under
   the VerificationExecutor's own isolation machinery (composed from
   `verification_executor.py`: the env allowlist scrub, the narrowed
   PATH policy, the `EnforcementProfile` digest) and the dependency
   containers' network reachability is probed with the #308
   five-outcome taxonomy as the positive control.

## Honesty vocabulary

`verifies` — every executed arm's assertions held (real or labeled
reference) AND the required TRX inventory is green.
`real_coverage_complete` — the stronger claim: build/test AND both
dependency arms ran against the REAL pinned artifacts and images.
Reference arms, unavailable images and SDK mismatches are recorded in
the run document with typed statuses; the document grants neither
merge nor deployment authority.

## The partner-gated remainder (R38-14)

The fixture is a stand-in for the real customer scenario. What stays
gated on the design partner (R38-14): the actual service pair and its
API/event contract, the real baseline schema with production-shaped
data volumes, the customer's acceptance test bundle, and the
customer-runner reproduction of this recipe on their infrastructure.
The recipe skeleton, the TRX inventory machinery and the
dependency-image executor wiring here are the deliverable; swapping
the fixture for the partner's frozen source is a manifest + fixture
change, not a machinery change.
