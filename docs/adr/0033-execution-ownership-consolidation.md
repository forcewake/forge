# ADR-0033: Execution-ownership consolidation — six decision owners, one closure matrix

Status: accepted (2026-09-24)

Context: review `6df4020`, item Q39-17 (issue #336). Previous:
ADR-0030 (authority-boundary ownership), ADR-0032 (the versioned
execution spec). Dependencies: #320 (the operation grant), #321 (the
approved input), #323 (the native locator registry), #327 (the
consumer-contracts conformance check).

ADR-0032 named ONE execution contract, and the cycle landed its pieces:
#320 persisted `CredentialOperationGrant` at dispatch with the endpoint
judging every redemption against that document; #321 resolved
`ApprovedInput` at every GitLab dispatch entry with the brief generated
from the record; #323 made the native carrier locators collision-safe
behind `NativeLocatorRegistry`; #327 drove the new contracts through
their actual consumers in the conformance gate. What remains — the
defect this ADR closes — is that the ownership is stated in six
different module docstrings and four ADRs while the provider services
still re-resolve SOME inputs independently, and the evidence for "this
works" stays spread across model families (unit suites, the
production-entry package, the qualification store, live records). The
goal is FEWER independent decisions, not more wrappers: no new service
boundary, no source split (acceptance §7 — neither carries an
ownership or deployment benefit today).

## Decision

### 1. The six decision owners

Every authority-bearing decision an execution carries has exactly ONE
production owner and ONE consumer contract. The map, as landed:

| Decision | Production owner | Consumer contract |
| --- | --- | --- |
| which plan is approved | `forge.adaptive.revisions.resolve_approved_input` → `ApprovedInput` (#321) | `forge.revision.approved-input/1` — the source label + `plan_text_digest`; the brief is `ApprovedInput.brief()`, never a re-render |
| which credential authorizes the operation | `forge.adaptive.credential_broker` — `CredentialOperationGrant` minted by `operation_grant_for_plan`, persisted idempotently by `merge_operation_grant`, judged at `forge.api_lane_control._authorize_operation_grant` via `operation_grants_for_attempt` (#320) | `forge.credential.operation-grant/1` — refs + the absolute deadline; a registry entry alone grants nothing (`grant_absent_native_only`) |
| which attempt is active | `forge.adaptive.continuation` — `decide_continuation` over recorded evidence (#261/#262) | the resume words (`fresh`/`required`/`restart`) + the one typed retry-refusal table (`runs.revival.retry_rejection`) every provider consumes verbatim |
| which checkpoint restores | `forge.adaptive.checkpoint_repository` + `forge.api_checkpoint_channel` — `resolve_repository` the one composition point (#259/#263) | the checkpoint lookup-outcome vocabulary + the resume spec (`/lane/controls/resume-spec`) and activation receipts |
| which candidate is verified | `forge.adaptive.verification_binding` (+ `verification_sets`, `runs.usecases`) — subject binding, freshness, applicability (#273) | the one verdict vocabulary; WAIT is never a verdict; no provider counts a green harness job as independent verification |
| which receipt counts | `forge.durable.budgets.ingest_usage_receipt` — the R23 natural key; `forge.adaptive.usage_ingestion` the artifact front door (R38-09) | the identity `(run, attempt, receipt_id[, source])` — idempotent by construction, final replaces partial, unknown stays unknown |

Nothing else may decide these. The boundary registry
(`forge.adaptive.boundary_registry`) already guards five of the six
neighborhoods mechanically; this cycle adds the three rules below for
the shapes the cycle itself landed (the grant load, the approved-input
brief, the locator allocation). A second harness or runtime recipe
composes the SAME owners — acceptance §1's "no duplicated
approval/continuation implementation" is a consequence of the map, not
a new framework.

### 2. The runtime-recipe × harness-profile axis separation, restated

ADR-0032 §3's composition matrix is (provider × runtime recipe ×
harness × credential route). The two axes must not be conflated:

- the **runtime recipe** is HOW a lane executes: the SDK/dependency
  pinning, the resume capability (`RESUME_CAPABLE_RECIPES` — the
  scripted GitLab batch lanes restore nothing; the GitHub harness and
  the GitLab SDK lane restore exactly), the test-report shape the
  collector emits;
- the **harness profile** is WHO executes: the driver identity, the
  invocation surface, the sessions model, the control capabilities
  (`interrupt`, `steer`) the record claims.

A profile record's `control_capabilities` never enter the execution
spec; a spec's resume mode never decides a profile's capability claim.
The qualification store judges profiles; the composition matrix judges
recipes. Neither borrows the other's axis.

### 3. The extraction ladder

The remaining duplication (the provider services re-resolving some
inputs) is removed ONE provider boundary at a time — ADR-0029's
composition ladder, not a rewrite wave:

1. **the GitLab input** (landed, #321): every dispatch entry resolves
   `ApprovedInput` from durable state; the brief is the record's text;
   the WIP-reuse fence rides the record. Rule R2 below guards it.
2. **the credential grants** (landed, #320 + #323): the dispatch mints
   and persists THE grant; the endpoint loads it; the locator
   allocation goes through the registry's collision check. Rules R1
   and R3 below guard them.
3. **the execution-spec adoption** (this cycle, additive): the spec
   gains the two authority members — the approved-input digest and the
   grant id — so a persisted spec names the approval and the
   credential authorization it was composed under (ADR-0032's
   amendment note). The provider services adopt
   `compose_execution_spec` composition-by-composition;
   `COMPOSED_DISPATCH_ENTRIES` records who has.
4. **the later rungs** (pending, named by the closure matrix): the
   usage-identity wiring rule, the live rebind trace, the live grant
   redemption. One rung lands only with the same end-to-end trace
   green BEFORE and AFTER the extraction (acceptance §4).

Each rung ships with compat imports (ADR-0031's policy: a compat import
is removed only when its callers are drained), and versioned read
adapters for persisted prior contracts — an unknown newer schema is a
typed refusal, never a silent drop.

### 4. The closure matrix

A GitHub issue closed is not every level proven. Each of the six
capabilities carries a small closure matrix over six levels — domain
contract, wired caller, executed process, native execution,
cross-process recovery, customer acceptance — with the CURRENT
evidence class recorded as data (`forge.adaptive.closure_matrix`):

- **unit-proven** — the owner's contract suite pins the decision;
- **pe-proven** — a production-entry trace drives the same entry a
  customer invokes (real processes, real restarts, the modeled native
  surface);
- **live** — a committed live-provider record proves it against the
  real provider (with its failures recorded as failures);
- **pending-human** — the evidence chain is complete up to the human
  record; the approval is the only missing member;
- **pending** — the honest gap; the entry names where the gap is
  documented.

The matrix validates against the repository (an evidence path that
does not exist is a validation failure, so the matrix can never cite
phantom traces) and renders the honest table per capability — pending
levels render pending, never proven. It is published at
`docs/operations/closure-matrix.md` and cross-linked from the
profile-qualification records (the qualification store judges
PROFILES; the closure matrix judges CAPABILITIES — the two views
join on the live records but answer different questions).

### 5. The three boundary rules

Three mechanical rules (in `boundary_registry`, enforced by
`tests/test_architecture_boundaries.py`, each with an
intentional-violation trap):

- **R1 — the redemption endpoint loads THE grant.** Inside
  `forge.api_lane_control`, a credential-registry lookup
  (`resolve_dispatch_credential`) may run only in a function that also
  loads the persisted operation grant. The registry validates
  revocation, rotation and staged slots; it never authorizes an
  operation. Trap: a registry-lookup authorization path with no grant
  load.
- **R2 — the GitLab dispatch resolves `ApprovedInput`.** Inside
  `forge.runs.service`, a function that builds the dispatched executor
  brief (the `FORGE_PLAN` digest variables) must obtain its text from
  `resolve_approved_input` / `ApprovedInput.brief()`; a direct
  `spec.plan_summary` brief is the pre-#321 shape. Trap: a brief
  variable constructed from the spec summary.
- **R3 — the locator allocation goes through the registry.** The
  legacy lossy carrier derivation (`credential_secret_name` /
  `credential_secret_segment`) is the broker's migration inventory's
  own; any other module deriving a carrier name directly bypasses
  `NativeLocatorRegistry.allocate`'s collision check. Trap: a dispatch
  leg calling `credential_secret_name`.

## Consequences

- The consolidated ownership map is one table (this ADR) + one
  registry module + one matrix module — the per-cycle docstring
  archaeology stops being the only place the answer lives.
- The closure matrix makes the gaps VISIBLE: today the live
  interrupt-resume arm is recorded as failing delivery, the live
  rebind and grant-redemption traces do not exist, and the
  usage-identity wiring has no rule yet — a release note may quote the
  matrix instead of re-deriving honesty from memory.
- The execution spec's additive members keep `forge.execution.spec/1`
  (additive-with-version-note, ADR-0032's amendment): a spec/2 bump is
  reserved for a change that alters the digest semantics of EXISTING
  members.
- No new service boundary, no source split, no new wrapper layer: the
  ladder runs on the main development line until a concrete ownership
  or deployment benefit says otherwise.
