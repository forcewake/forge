# ADR-0030: Authority-boundary ownership — the six owners, the registry, and the mechanical fence

> **Superseded by [ADR-0035](0035-simplification-program.md)** (2026-09-29).

Status: accepted (2026-09-23); historical decision, summarized 2026-10-23.
The complete original, including caller inventories and measurements, is in
local snapshot `a80706e54674b5abb571f4b8481252a41273c0c7` at this same path.
The [archive manifest](../archive/documentation-0.42.json) verifies its bytes.
The snapshot is unpublished; see [archive recovery](../archive.md).

The original problem was divergent decisions: a newly correct owner existed,
but an old caller independently decided what was current or authorized.
A registry named each authority, its actual callers and negative contracts.
Import and call-graph checks rejected bypasses and stale registrations.
The final implementation uses ADR-0035's domain, ports, engine and Effects;
the historical registry and compatibility adapters are removed.

## Decision

### 1. The six owning boundaries

| Boundary | Required behavior retained in the new architecture |
| --- | --- |
| Repository identity and checkpoint lifecycle | Resolve the configured authority once. Unavailable, corrupt and unauthorized are never absence. Fence writes during cutover; journal deletion and enforce retention locks. |
| Continuation authorization | Derive continuation from durable lineage and the pinned checkpoint digest. Prose cannot authorize a restart or discard. Uncertain evidence refuses execution. |
| Native occupancy | Acquire by database uniqueness. Release requires native terminal evidence; local status alone cannot free a slot. |
| Candidate publication | Persist intent before provider I/O, validate one immutable candidate and recover by native correlation. A human-moved head refuses publication. |
| Verification applicability | Bind evidence to the exact candidate and applicable required checks. Lane success and WAIT never authorize readiness. |
| Operator projection | Read authorized durable state. A display cannot claim a stronger outcome or offer actions outside the actor/state policy. |

### 2. The registry and the mechanical fence

Checks confined authority selection, envelope construction, checkpoint lookup,
continuation modes and blob unlinks to registered owners. Registrations were
checked in both directions. Negative traps deliberately introduced bypasses;
positive controls ensured the checkers did not reject permitted delegation.
These lessons remain in import contracts and invariant/edge-case tests.

### 3. Provider conformance (`contract.provider_conformance`)

Equivalent negative inputs must produce equivalent typed refusals while
retaining native identities and concurrency semantics. Unavailable remains
unknown, distinct from proven absence. Superseded generations and mismatched
epochs refuse. Historical resume gaps were recorded, not treated as parity.

### 4. Persisted-schema inventory (expand-contract)

The former deployment retained explicit versioned fixtures for RunSpec,
checkpoint, control, attempt and continuation documents. A historical
source OID never became an invented execution identity. ADR-0035's owner
cutover now replaces mixed-version execution: stop the old service, preserve
backup/export, initialize the final schema and refuse old schemas at startup.

### 5. The measured outcome (decision points, not module count)

The useful measure was one authority per decision and actual adoption by
callers. Adding a helper or registry entry without removing a competing
decision did not count as consolidation. Original counts and native
acceptance limitations remain in the archived record.

## Consequences

Ownership is enforced mechanically. Native differences stay explicit.
Fixtures and projections never become deployed authorities. Retiring an
implementation preserves its applicable negative contracts and evidence.

## Amendment — 2026-09-24, R37-19 (ADR-0031): the owners stay; the scenarios moved out

The six boundaries stayed. Scripted twins and qualification executors moved
outside production imports. ADR-0035 subsequently removed that machinery;
retained product contracts continue to exercise the real owning seams.
