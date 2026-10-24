# ADR-0023: Gate-approved driver selection

Status: accepted on 2026-09-15; amended by
[ADR-0035](0035-simplification-program.md). The complete original is retained
at this path in the verified [documentation archive](../archive.md).

Driver and model selection are execution authority. Resolve them before
human approval, freeze them with scope, verification and budget limits, and
show the complete execution policy in the plan comment. Dispatch consumes
the approved value. A planner suggestion cannot expand configured authority,
read credentials to select an unconfigured driver or introduce an ambient
model route. Material policy changes require another approval.

The original implementation supported a project-approved ordered driver
chain. Optional fallback applied only to explicitly classified infrastructure
failure before a candidate existed; advances were durable and visible.
Code failure did not justify changing the producer. Telemetry ranking was a
human suggestion, never automatic cross-project routing. Learned per-request
routing, dispatch-time model selection, mid-candidate switching and opaque
provider auto tiers were rejected as defaults.

ADR-0035 removes that chain and its fallback machinery from the 1.0 runtime.
A run has one explicit driver, model and metering policy in its immutable
RunSpec. First-class drivers are Claude Code and Copilot CLI; the latter
requires explicit scoped unmetered authorization. Unsupported capabilities
refuse visibly. Repairs retain the approved driver and scope. Resuming WIP
advances the generation of the same attempt and preserves the exact approved
input and named checkpoint; it does not select another driver.

The historical routing/compiler APIs and environment-variable preference
ladder are not compatibility interfaces. Experimental drivers remain isolated
lane extensions, with explicit opt-in and their own capability proof. They do
not silently extend server configuration or the approved execution policy.
