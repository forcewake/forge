# ADR-0029: Composition boundaries — the five names, the owner map, and the matrix that records what may compose with what

> **Superseded by [ADR-0035](0035-simplification-program.md)** (2026-09-29).

Accepted 2026-09-23; historical summary updated 2026-10-23. The full original
at this path is in local unpublished snapshot
`a80706e54674b5abb571f4b8481252a41273c0c7`, verified by the
[archive manifest](../archive/documentation-0.42.json).
See [recovery instructions](../archive.md).

Provider services hand-assembled execution identities independently. This
allowed a source SHA to become an execution identity, a provider family to
substitute for a connection or a latest checkpoint to replace an approved
exact reference. This ADR named immutable boundaries before caller adoption.

## Decision

### 1. Five composition boundaries, each ONE immutable value

- Repository identity includes provider, connection and native repository.
  Cross-family mixing refuses; display paths do not grant authority.
- Attempt input includes run, attempt, repository, frozen execution digest,
  resume decision and lease. Missing authority cannot receive guessed defaults.
- Resume names fresh, required or explicit restart. Required restore needs
  an exact checkpoint reference, generation and durable deciding command.
- Every authority decision and external effect has one owner. Admission is
  atomic; publication validates and journals intent; required independent
  checks bind the candidate; controls and checkpoint selection are durable.
- Tool observations record actual returned content/errors and truncation.
  They are distinct from model findings and citation claims.

### 2. `CompositionMatrix` — composition permission is recorded evidence, not review memory

The historical matrix recorded directional verified, untested or unsupported
edges. Verified required an evidence reference; unregistered combinations
were untested. Queries reported every nonverified dependency from either
endpoint. Shipped inventory without proof was a finding, never acceptance.
ADR-0035 retires the matrix implementation while preserving honest evidence
boundaries in release and verification contracts.

### 3. Expand-contract for every boundary in this list

The old deployment preserved versioned readers until verified-zero usage.
Unknown shapes refused. The owner's ADR-0035 cold cutover now replaces that
mixed-version rollout: one schema, explicit offline backup/export and no
old execution fallback. Historical data is preserved without manufacturing
new authority from it.

### 4. The compat-fixture contract (`adaptive/compat_fixtures.py`)

Fixtures described each shipped document's actual read/recovery semantics.
A digest-only old spec required reapproval, and missing typed authority
never became a current executable input. Those retired fixtures and the
original version inventory remain in the archive, outside production.

### 5. Import boundary

Pure contracts do not import provider adapters, ingress or composition.
A type existing is not evidence that a production caller adopted it.

### 6. Extraction candidates (NOT executed here — the ADR-0027 ladder's next rungs)

The original ladder named start, dispatch, verification, revision and research
owners. Each extraction had to rewire real callers, remove duplicate
decisions and preserve end-to-end behavior. ADR-0035 delivers these through
one engine rather than provider-specific lifecycle services.

## Consequences

Identity errors refuse at construction; compatibility and acceptance require
explicit evidence. No additive contract alone establishes runtime adoption.

## Amendment — 2026-09-23, R36-20 (ADR-0030): the owner map is now a registry and a fence

Actual caller registration and negative traps replaced prose-only ownership;
stale registrations also failed. The final import contracts preserve that
mechanical ownership discipline.

## Amendment — 2026-09-24, R37-19 (ADR-0031): reference scenarios left the contract modules

Scenario executors were separated from product contracts, with production
imports forbidden. ADR-0035 removes their compatibility reexports and
preserves applicable negative tests on the real owners.
