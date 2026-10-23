# ADR-0033: Execution-ownership consolidation — six decision owners, one closure matrix

> **Superseded by [ADR-0035](0035-simplification-program.md)** (2026-09-29).

Accepted 2026-09-24; historical summary updated 2026-10-23. The complete
original, caller map and measured claims are in local unpublished snapshot
`a80706e54674b5abb571f4b8481252a41273c0c7` at this path. See the
[verified manifest](../archive/documentation-0.42.json) and
[archive recovery](../archive.md).

The execution contract had named owners, but providers still independently
resolved some authority and evidence levels were scattered. This ADR required
one decision owner and adoption by actual consumers. ADR-0035 replaces the
old services, registry and closure matrix with the engine and enforced ports.

## Decision

### 1. The six decision owners

| Authority | Contract retained |
| --- | --- |
| Approved plan | Execute the exact durable approved input and text digest. |
| Credential operation | Persist and judge the full scoped, expiring grant; a registry reference alone authorizes nothing. |
| Active attempt | Derive continuation from recorded lineage and use one typed refusal vocabulary. |
| Checkpoint restore | Resolve one configured authority and restore the exact approved checkpoint. |
| Verified candidate | Bind independent required checks to SHA, freshness and applicable obligations; WAIT never authorizes readiness. |
| Usage receipt | Deduplicate by complete run/attempt/source identity; final replaces partial and unknown stays unknown. |

### 2. The runtime-recipe × harness-profile axis separation, restated

Execution mechanism and driver capability are different facts. A profile's
claim does not establish a recipe's restore behavior, and a selected resume
mode does not prove vendor support. Native acceptance must exercise the
actual composed mechanism.

### 3. The extraction ladder

Each rung rewired real callers to the owner and preserved the same end-to-end
trace. Adding types or wrappers without removing an independently decided
input was insufficient. Historical mixed-version readers were explicit and
unknown versions refused. The final cold cutover removes those adapters.

### 4. The closure matrix

The former matrix distinguished domain contracts, wired callers, executed
processes, native execution, recovery and customer acceptance. Closed issues
or passing unit tests could not promote missing levels to proven. Evidence
paths had to exist. ADR-0035 removes matrix machinery; reports still retain
these distinctions and never equate synthetic execution with live acceptance.

### 5. The three boundary rules

- Redemption must load the persisted operation grant in addition to resolving
  credential references and revocation.
- Dispatch must resolve the exact approved input instead of rendering another
  brief from a summary.
- Native credential locators must use a collision-safe owner instead of lossy
  independently derived names.

Each rule had an intentional-violation trap. Applicable lessons remain in
new domain, OIDC, Effects and lane contracts.

## Consequences

One authority has one owner and a verified consumer. Evidence gaps remain
visible. A new wrapper, schema field or successful synthetic scenario alone
cannot establish production adoption or acceptance. The final implementation
has no historical credential bridge, profile qualification or matrix fallback.
