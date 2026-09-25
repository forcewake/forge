# The .NET service-and-dependency recipe, qualified on the lab (2026-09-25)

R38-08 (#309): the SDK-pinned .NET recipe skeleton + the TRX inventory
machinery + the dependency-image executor wiring, qualified to the
maximum honest extent this lab allows. The runner is
`src/forge/adaptive/dotnet_recipe.py`; the pins live at
`qualification/recipes/dotnet-service-dependency/` (README +
`manifest.json`); the frozen fixture source is
`evaluation/tested_world/dotnet-orders/`; the Python pins are
`tests/test_dotnet_recipe.py` (34 tests, including the container-gated
real arms). The recorded full-run document of the lab execution below
is `recorded-run.json` beside this README.

## What ran on the lab (2026-09-25, all arms REAL)

| Arm | Outcome |
|---|---|
| SDK probe | dotnet 10.0.200 on PATH — matches the pin exactly |
| orders-db container | `postgres:16` pulled AND run at the pinned digest `sha256:1a43e6bb8872…` (resolved == pinned); disposable, stopped + removed |
| orders-bus container | `rabbitmq:3-management` pulled AND run at the pinned digest `sha256:8689ddfceca1…` (resolved == pinned); disposable, stopped + removed |
| Reachability controls | both dependencies answered on their service ports — `authorized_control_succeeded` (the #308 five-outcome taxonomy) |
| Build/test (TRX) | `dotnet test` per required project under the pinned SDK, both exit 0, both TRX produced with identity sidecars; reconciliation `all_passed` (Api 3/3, Projection 6/6) |
| Migration (N-1 → N) | REAL PostgreSQL 16 inside the container: 25 seeded canary rows preserved (count + per-row sha256 fingerprints equal), the negative-total insert ACCEPTED on the baseline and REJECTED after the upgrade, region backfill complete |
| Redelivery | REAL RabbitMQ 3 via a minimal stdlib AMQP 0-9-1 client: 4 messages each published twice on a per-run durable queue, one delivery consumed WITHOUT ack, the channel crashed after the state commit, the broker REDelivered it (`redelivered=true` observed), 9 deliveries total, exactly ONE durable side effect per message asserted from the database |
| Run verdict | `verifies: true`, `real_coverage_complete: true`, zero problems, zero degradations; 26 receipted commands; exit 0 |

The .NET fixture's own acceptance tests (inside the reconciled TRX)
additionally pin: exactly-once effects under duplicate delivery, the
crash-after-commit-before-ack replay, v1/v2 widening acceptance, and
the NEGATIVE old/new combination — a v1-only consumer rejects the v2
producer for the expected compatibility reason (the widening field).

## What is stubbed / reference-labeled, and why

Nothing was faked green. The design includes two labeled REFERENCE
arms that run ONLY when the real dependency cannot (no runtime, image
unpullable, digest mismatch): the sqlite migration dialect and the
in-process redelivery harness, both tagged `reference-coverage` with a
typed status (`image-unavailable`, `container-runtime-unavailable`,
`image-digest-mismatch`, `dotnet-sdk-unavailable`). On this lab both
real arms ran, so the reference stand-ins stayed unused — they are
pinned by the offline tests in `tests/test_dotnet_recipe.py`.

The Python fixture profile (`evaluation/tested_world/orders-api` +
`orders-projection`, the #296 executor world) is RETAINED untouched as
the fast deterministic reference — this recipe does not replace it.

## The partner-gated remainder (R38-14)

The real customer scenario selection is design-partner-gated. What
stays gated: the customer's actual service pair and API/event
contract, their baseline schema and data volumes, their acceptance
test bundle, and reproducing this recipe on a customer runner. The
fixture here is a stand-in with the same SHAPE (producer/consumer,
shared contract, frozen acceptance tests, migration pair); swapping in
the partner's frozen source is a manifest + fixture change, not a
machinery change. No paid model tasks were used; the verifier carries
no coding-agent credentials (the tool invocations run under the
executor's env allowlist scrub + narrowed PATH, and the run document
binds the enforcement-profile digest).

## Honesty vocabulary

`verifies` — every executed arm's assertions held (real or labeled
reference) AND the required TRX inventory is green.
`real_coverage_complete` — the stronger claim: build/test AND both
dependency arms ran against the REAL pinned artifacts/images. The
document grants neither merge nor deployment authority.
