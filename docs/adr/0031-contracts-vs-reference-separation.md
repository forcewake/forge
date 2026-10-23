# ADR-0031: Contracts vs reference — separating reusable product contracts from reference scenarios

> **Superseded by [ADR-0035](0035-simplification-program.md)** (2026-09-29).

Accepted 2026-09-24; historical summary updated 2026-10-23. The complete
original at this path is preserved in local unpublished snapshot
`a80706e54674b5abb571f4b8481252a41273c0c7`. The
[manifest](../archive/documentation-0.42.json) verifies its bytes;
[archive instructions](../archive.md) explain recovery.

Runtime contract modules had also contained synthetic twins, provider-shaped
remotes and scripted vendors. Importing a product contract could therefore
load scenario assumptions into deployed code. This ADR separated those
categories; ADR-0035 subsequently retired the qualification machinery.

## Decision

### 1. The labelled reference package

Product contracts, fake-native integration and customer-executed evidence
were distinct. Scenario builders and synthetic executors moved to a labelled
evaluation package. Real decision owners and durable identities stayed
product code. A fixture's behavior never became a native platform guarantee.

### 2. The dependency-direction rules (mechanical)

Production entry points could not import reference code. Only explicit,
registered temporary extraction homes could reexport it. Reference scenarios
composed product contracts without owning orchestration or importing service
composition. Clean-process startup probes and intentional-violation tests
checked that no synthetic implementation was selected implicitly.

### 3. The compatibility surface and the migration policy

The historical rollout preserved exact-symbol imports, sometimes lazily to
avoid cycles and keep a spawned executable's top level standard-library-only.
Reexports were removed when callers drained, with stale registrations failing.
These were transitional deployment rules, superseded by the owner's final
cold cutover. No compatibility import or synthetic executable is retained
in the final engine or lane.

### 4. The owner map is unchanged

Moving test machinery did not create new authority, orchestration or DTOs.
The same owners decided approval, continuation, publication and verification.

## Consequences

Product code and its test doubles have explicit dependency direction.
Unit, process, native and customer evidence stay distinguishable. The full
original records the moved symbols and historical acceptance limits.

## Appendix — residual duplicated decision points (listed, not fixed)

The original audit identified repeated Boolean parsing, timezone handling,
marker scans, lazy import adapters and preservation fingerprints. Consolidation
had to remove competing decisions without coupling unrelated removal schedules.
Timezone comparisons must stay aware; marker recovery must verify native
identity; preservation proof compares actual stored data. Retiring reference
implementations preserves these lessons on the final product seams.
