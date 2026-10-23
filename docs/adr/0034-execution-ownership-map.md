# ADR-0034: The execution-ownership map — one owner per mature authority decision, callers verified

> **Superseded by [ADR-0035](0035-simplification-program.md)** (2026-09-29).

Status: accepted (2026-09-26); historical decision, summarized 2026-10-23.
The complete original, with caller locations, continuations and measurements,
is preserved at this same path in local snapshot
`a80706e54674b5abb571f4b8481252a41273c0c7`. Its bytes are recorded in the
[archive manifest](../archive/documentation-0.42.json); the snapshot is
unpublished. See [archive recovery](../archive.md).

Correct helpers had accumulated while services, endpoints and reports still
independently decided what was approved, current, budgeted or publishable.
This ADR mapped actual callers, consolidated duplicate decisions and recorded
unimplemented provider capabilities honestly. ADR-0035 replaces those old
services and adapters with one engine; the safety lessons below remain.

## Decision

### 1. The ownership map (verified production callers)

| Decision | Contract retained for the new engine |
| --- | --- |
| Approved input | The executor receives exactly the consumed, digest-bound frozen input; display text cannot substitute for it. |
| Budget amendment | A durable native command identity applies once, atomically to enforcement capacity. Evidence projections cannot grant capacity. |
| Credential grant | The complete execution identity and expiry authorize redemption. A cached projection is not another authority. |
| Candidate publication | One validated write path, committed intent before effects, native expected-head protection and exact recovery. |
| Publication recovery | Recorded commit identities and native proof distinguish owned candidates, human changes, absent effects and unknown outcomes. |
| Command acceptance | Durable SQL receipt decides duplicates. A positive cache needs SQL confirmation; orphaned cache markers cannot lose commands. |
| Command completion | Journaled completed, refused, pending and exhausted outcomes outrank request status. A retryable observation cannot be recorded as completed. |
| Credential preflight | Diagnostics and dispatch use the same resolver. Preflight reads references and routes without redeeming secrets. |
| Artifact promotion | Build once; smoke and release identify the same artifact digest. A rebuilt or unbound artifact refuses promotion. |
| Verification applicability | Reuse requires the same candidate, obligations, freshness and authority. No provider locally invents a weaker check set. |
| Execution occupancy | Acquire atomically and release from native evidence. Ambiguous active jobs keep occupancy; verification jobs do not masquerade as lanes. |
| Feedback admission | Authorization, supported verbs and bounded rounds are explicit. Child admission and its own budget commit together or not at all. |

### 2. The extraction — one BudgetAmendment application service, three callers, one table

GitHub's evidence-only top-up ledger was replaced by the same durable
amendment application used by GitLab. Both consulted effective limits and
projected the same rows. Replayed native commands could not apply twice.
USD and count axes retained their distinct enforcement; no implicit unit
conversion or projection-only increase was allowed.

### 2b. The R41-17 continuation (#372) — the admission composition extracted, the dedup residual removed

Review child creation, its frozen input, independent budget, reservation,
round receipt and scheduling became one transaction. Partial failure left
no admitted child without budget. Ingress callers also adopted the durable
dedup owner instead of retaining an inline cache-based competing decision.

### 2c. The R42-16 continuation (#389) — the #379 display pin landed, the command-outcome parity rule added

A planner's proposed budget class could change selection but could not move
an already opened enforcement row. The displayed approval and frozen spec
were pinned to that row in both escalation and downgrade cases. Children
opened budgets from those same frozen limits.

The operator display's fallback classification was required to match the
journal authority exactly. Unknown words stayed unknown; a normal handler
return was not evidence of successful completion after a retryable native
observation. The final engine uses one outcome vocabulary rather than
retaining duplicate literal sets plus a compatibility rule.

### 3. Provider-native differences stay in adapters

Consolidation removes competing authority decisions, preserving native
capabilities and typed refusals. Historical API differences are recorded in
the archive. ADR-0035's native publication amendment now requires an exact
old-ref lease and complete-tree recovery on GitHub and GitLab; no weaker
read-before-write fallback is retained.

### 4. Single-owner entries — verified, deliberately NOT extracted

A second caller of one authority is not a duplicate decision. Original gaps
included Azure's budget display, GitHub's review-authority expiry and
provider-specific closing reserves. These were documented as gaps rather
than fabricated parity. The pre-migration credential bridge and old top-up
spelling were temporary compatibility mechanisms; the final cold cutover
removes them.

### 5. Synthetic executors and qualification fixtures never load in production startup

Scripted vendors, reference remotes and compatibility fixtures were barred
from production imports, with intentional-violation traps and clean-process
startup probes. Test doubles never constitute native acceptance evidence.
ADR-0035 retires their production machinery and preserves applicable tests.

### 6. Compatibility and the rollback/forward plan

The original change supported mixed workers and historical document
versions. This is historical deployment context, superseded by the owner's
ADR-0035 cold cutover: one engine, one baseline, explicit offline
backup/export migration and startup refusal of old schemas. No old grant,
top-up, dispatch or execution path remains available as a fallback.

## Consequences

Each mature authority has one owner, used by actual callers and enforced
by negative contracts. A display cannot overstate a journaled outcome,
a budget proposal cannot change enforcement, and fixtures cannot enter
production. The full chronology and original measured claims remain
available through the verified local archive and Git history.
