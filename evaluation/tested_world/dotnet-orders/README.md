# dotnet-orders — the .NET fixture service pair (R38-08)

The customer-shaped fixture for the `.NET service-and-dependency`
verification recipe (issue #309 / R38-08). Two minimal SDK-style
services sharing one contract project, mirroring the shape of the
Python reference pair (`../orders-api`, `../orders-projection`):

| Project | Role |
|---|---|
| `src/DotnetOrders.Common` | the shared contract: `OrderCreated` (dialect v2 widens v1 with `Region`), the referee `Contract`, the broker `Delivery` shape, and the `V1OnlyGate` that pins the negative old/new combination |
| `src/DotnetOrders.Api` | the producer — deterministic dialect-v2 `OrderCreated` stream |
| `src/DotnetOrders.Projection` | the consumer — `ProjectionHandler` applies deliveries with exactly-once side effects (the durable effect commits BEFORE the ack) |
| `tests/DotnetOrders.Api.Tests` | the producer's acceptance tests (TRX project 1) |
| `tests/DotnetOrders.Projection.Tests` | the consumer's acceptance tests (TRX project 2) — includes idempotency under duplicate delivery, the crash-after-commit-before-ack replay, and the negative v1-consumer/v2-producer combination |
| `migrations/` | the N-1 baseline (portable) and the N-1 -> N upgrade in two dialects (PostgreSQL 16, SQLite) |

Pins (frozen in `qualification/recipes/dotnet-service-dependency/manifest.json`):
dotnet SDK 10.0.200, `net10.0`, `Microsoft.NET.Test.Sdk` 18.10.1,
`xunit` 2.9.3, `xunit.runner.visualstudio` 4.0.0.

The fixture is FROZEN SOURCE: the recipe builds these exact files and
binds its report to the test-bundle digest over them. Editing these
files is a new test bundle — the old report no longer applies.

This fixture is a stand-in for the partner-gated real customer
scenario (R38-14): same shape (producer/consumer, event contract,
baseline schema, frozen acceptance tests), synthetic content.
