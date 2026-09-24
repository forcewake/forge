# ADR-0030: Authority-boundary ownership — the six owners, the registry, and the mechanical fence

Status: accepted (2026-09-23)

Context: review `16339c2`, item R36-20 (issue #279) — the review's own
diagnosis of its repeated findings: *"repeated findings arise when a new
correct helper and an old caller decide the same thing differently"*,
with the review's own examples (the checkpoint repository vs the retry
helper's JSON→HTTP path; the continuation decision vs the old
unconditional rejection; an envelope `attempt_id` fed a source OID).
Every R36 sibling landed this session already extracted ONE owning
module per decision — collector identity (#260), typed continuation
(#261), the configured lookup authority (#262), GC locks (#263), the
cutover fence (#264), envelope v2 (#265), the installer ladder (#266),
the PG gate (#267), the CE entry (#268), lane closure (#269), discovery
authority (#270), the live cohort (#271), the revision proof (#272),
verification binding (#273), the operator API (#274), the lab pilot
(#275), delivery measurement (#276), the durable saga (#277), system
verification (#278), ops drills (#280), profile records (#281). What
remained was the review's actual ask: make the ownership VISIBLE and
MECHANICALLY ENFORCED so the next helper cannot quietly re-decide a
decided thing — explicitly NOT another extraction wave, not a parallel
controller, not a package per noun.

This ADR names the six owning boundaries ADR-0029's owner map pointed
at, records their measured end state, and binds them to one declarative
registry (`src/forge/adaptive/boundary_registry.py`) that
`tests/test_architecture_boundaries.py` enforces over the production
tree with static import/call-graph checks and negative trap tests.

## Decision

### 1. The six owning boundaries

Each entry: the decision it owns, the owner module(s), the registered
production callers (verified against actual imports — the registry
fails on both an unregistered import AND a stale registration), and the
negative contract. The registry is the machine-readable form of this
table; this ADR is the reviewed interpretation.

**1. Repository identity + checkpoint lifecycle** — owner
`forge.adaptive.checkpoint_repository` (with `forge.api_checkpoint_
channel` as the store it wraps). Owns: WHICH authority answers
checkpoint presence, upload and retention (filesystem index / postgres
`checkpoint_metadata` / the authenticated channel proxy), the cutover
fence (`MutationsFencedError`, configured-vs-active), the pins overlay,
the pending-GC journal and the volume-wide sweep locks. Registered
callers: the HTTP channel, the control-service composition
(`adaptive/wiring`), the revision proof (`adaptive/revisions`), the
versioned migration adapter (`adaptive/checkpoint_migration`), the
operator snapshot reader, the ops drills, the retry/revival chain
(`runs/revival`), the continuation pin path (`runs/github_service`),
the compat fixture reader, and `main`'s startup marker line.
Negative contract: **no dispatch, control or retry path may read a
checkpoint index directly, construct a store, consult a different
authority on failure, or resolve durability from the environment on its
own.** Enforced: `resolve_repository` calls confined to 5 composition
sites; the legacy chain (`revival._legacy_http_lookup`,
`CheckpointStore._load_index`, direct `CheckpointStore` construction)
confined to 4 chain modules — and inside `runs/revival` to the one
opt-in adapter function itself; CAS blob unlinks confined to the two
sweep entry points (`_sweep_locked`/`_asweep_locked`), with the
pins/journal overlays and atomic-write temp files the enumerated
exemptions.

**2. Continuation authorization** — owner `forge.adaptive.continuation`.
Owns: WHICH recoverable state a retry continues from
(`fresh`/`required`/`restart`/`uncertain`), from recorded evidence,
with lineage and the pinned `checkpoint_digest`; plus the typed `/retry`
command grammar that authorizes a discard. Registered callers: the
GitHub dispatch leg (`runs/github_service` — the one adopted lane-resume
path) and the compat fixture reader (persisted-document reads).
Negative contract: **no service may select a resume mode from its own
reading of the evidence, honor the restart verb from prose, or collapse
an unprovable checkpoint state into a dispatch decision.** Enforced:
`ContinuationMode` construction confined to the owner and the compat
reader; inside the vocabulary home (`runs/github_service`) the mode
literals exist ONLY in the `LANE_RESUME_MODES` definition, the
constants are referenced only as the documented initial-dispatch default
(`LANE_RESUME_MODE_FRESH` as the parameter default) and the vocabulary
validation, and every `resume_mode=` a call passes is the decision's
`resume_mode()` or the inherited parameter — never a fresh expression.

**3. Native occupancy** — owner `forge.adaptive.admission` (with
`forge.runs.admission` as its identity half). Owns: WHEN an execution
slot is occupied and when it frees — the CAS lease, the derived
occupancy vocabulary (`never_dispatched`/`dispatched_unknown`/
`native_running`/`draining`/`observed_terminal`) and the evidence-based
release. Registered callers: the three provider services, the command
router, the operator snapshot reader, the ops drills, the `runs`
package surface. Negative contract: **no provider may release a slot
without evidence, derive occupancy from local status, or hold two
leases for one run** — `uq_execution_lease_open_run` decides, not a
read-then-write. Enforced: import registration (the CAS itself is a
database invariant, not an AST shape).

**4. Candidate publication** — owners `forge.adaptive.publication_saga`
+ `forge.adaptive.saga_durable`. Owns: WHETHER and HOW candidate
effects are published — intent persisted before the provider call,
adoption by native correlation on recovery, human-edit parks, the
durable two-writer phase admission. Registered callers: the two-writer
qualification machinery. Negative contract: **no orchestration path may
create a provider effect without a persisted intent, adopt an effect it
cannot correlate natively, or force-overwrite a human-moved branch.**
The provider write boundary itself stays ADR-0016/0026's (the
`runs/publisher` policy over `mr_reservations`); the saga owns the
coordination decision. Enforced: import registration.

**5. Verification applicability** — owners
`forge.adaptive.verification_binding` +
`forge.adaptive.verification_sets` + `forge.runs.usecases`. Owns:
WHETHER a verdict applies to the exact candidate and tested world it is
cited against — subject binding, freshness, the required-report
inventory, applicability invalidation, and the one verdict vocabulary.
Registered callers: the GitHub and Azure services (through
`observe_verification` and the binding surfaces), and the
independent-check, system-verification, workpackage and two-writer
qualification modules. The GitLab lane deliberately joins at the shared
verdict VOCABULARY (`runs/verification`'s result shape) rather than the
use case itself — ADR-0027 slice 2's documented divergence, kept
visible here rather than smoothed over. Negative contract: **no
provider may count a green harness job as independent verification,
apply a verdict to a candidate it does not bind, or re-derive the
required-checks proof locally — WAIT is never a verdict.** Enforced:
import registration plus ADR-0027 §3's core-import rule on
`runs.usecases`.

**6. Operator projection** — owners `forge.adaptive.operator_view` +
`forge.adaptive.operator_snapshot`. Owns: WHAT an operator sees and may
safely do — the derived state vocabulary, the CAS-guarded projection,
and the one subject-scoped authorized reader over the durable rows.
Registered callers: the operator HTTP API and the support bundle.
Negative contract: **no surface may assert a state workers did not
derive, offer an action outside the state × actor matrix, or read
durable rows around the authorized reader.** Enforced: import
registration.

### 2. The registry and the mechanical fence

`boundary_registry.py` is deliberately import-light (pure stdlib data):
each boundary's owners, registered dependents, the confinement
allow-sets (legacy-chain modules, `resolve_repository` callers,
`AttemptStartSpec` constructors, sweep entry points, mode-construction
modules) and the negative contracts as data. The architectural test
enforces, over every `src/forge` module (AST, following the repo's
existing idiom from `test_usecases.py`'s import-boundary check):

1. **Legacy-lookup confinement** — references to the retired chain are
   legal only in the enumerated modules; inside `runs/revival` only in
   `_legacy_http_lookup` itself. Anything else fails with the
   `architecture.legacy_call_sites` finding (a new dispatch path
   dialing the legacy adapter — the #262 escape hatch confined).
2. **The composition monopoly** — `resolve_repository(...)` calls only
   from the five registered composition sites.
3. **Envelope construction** — `AttemptStartSpec(...)` only in the
   type's home and the one adopted adapter; every registered composed
   dispatch entry actually calls `compose_attempt_start` (the registry
   is verified in both directions).
4. **GC unlink confinement** — CAS blob unlinks only inside the sweep
   entry points, with the enumerated non-blob exemptions; the check
   ALSO fails if a registered sweep entry point no longer unlinks blobs
   (a renamed seam may not hollow the rule).
5. **Mode selection** — no mode literals, mode-constant re-derivations
   or fresh `resume_mode=` expressions in the vocabulary home.
6. **Registration** — a module importing an owner without registration
   fails with an instruction to register or route through the owner;
   a registration whose module no longer imports the owner fails as
   stale.

The negative trap tests (the issue's acceptance) prove the checkers
detect rather than pass vacuously: an intentional legacy call in a
synthetic module, a raw index read, the chain inside `revival` but
outside the adapter, an unregistered `resolve_repository` call, a
hand-built `AttemptStartSpec`, a blob unlink outside the sweeps, a
grafted mode re-derivation, and an unregistered boundary dependent —
each is caught, and each check's positive form (the pass-through, the
in-sweep unlink) is asserted unflagged.

### 3. Provider conformance (`contract.provider_conformance`)

The same negative contract inputs produce equivalent typed refusals
across the GitHub path and the SHARED core, with native details
retained:

- **fenced authority** — a repository raising `MutationsFencedError`
  (the cutover-mid-flight case) surfaces through the GitHub-consumed
  adapter as the typed `unavailable`, and the shared
  `revival.retry_rejection` words it `checkpoint_authority_unavailable`
  — never "no checkpoint";
- **typed checkpoint-unavailable / corrupt / unauthorized** — the same
  refusal code from the one table; the GitHub continuation path over
  the same outcome normalizes to unknown → UNCERTAIN → no dispatch; a
  proven `absent` stays distinct (`nothing_to_retry`) — the two
  refusals may never converge (that convergence was the R36-03 defect);
- **stale epoch** — the superseded-generation oracle is
  `api_lane_control`'s alone (AST-asserted: no provider re-derives
  credential staleness), and the envelope's authority axis refuses a
  bad epoch while a good one is INSIDE the envelope digest (a bumped
  epoch is a different authorization; identical durable inputs
  reconstruct the identical envelope).

AST parity pins the "one table" claim: all three provider services call
the same `retry_rejection` and the same typed lookup adapter, and only
`runs/revival` constructs refusals. **The honest gap, asserted as
fact:** the lane-resume dispatch contract is GitHub-only (R32-04; the
GitLab CE qualification #268 and the lab pilot #275 recorded the seam's
absence) — GitLab and Azure carry NO resume-mode selection, the tests
assert that absence rather than fabricate parity, and their retry
refusal semantics are the shared core's.

### 4. Persisted-schema inventory (expand-contract)

The compat-fixture inventory (`adaptive/compat_fixtures.py`) now covers
every schema the boundaries persist — `compatibility.active_version_
population`:

- `run_spec` v1/v2/v3 (unchanged);
- `checkpoint_metadata` v1 (unchanged);
- `control_command` v1/v2 (unchanged);
- **`attempt_start` v1** (Q35-07 — audit-only: identity was the source
  OID; no execution identity is manufactured) and **v2** (R36-06 — the
  derived execution id, separated source base, epoch and pinned
  continuation ref inside the digest);
- **`continuation_decision` v1** (Q35-02 — the decision core;
  digest-governed reuse, lineage read as absent, never guessed) and
  **v2** (R36-02/03 — lineage, the pinned `checkpoint_digest`, the
  typed `refusal_code` observability);
- **`checkpoint_lookup` v1** (the retired opt-in chain's lossy
  exact/absent answer, authority `legacy-http-opt-in` — a v1 document
  claiming typed vocabulary is REFUSED, the chain could not produce it)
  and **v2** (the typed five-state outcome that never collapses).

Removal follows ADR-0029 §3: a version's fixture is deleted only at the
verified-zero-usage checkpoint (the legacy lookup fixtures go with the
R38 removal of `_legacy_http_lookup` itself).

### 5. The measured outcome (decision points, not module count)

The review's yardstick: *"changed lines and duplicated DECISIONS
measured — not module count."* The session's end state, counted from
the landed tree (the numbers the architectural test re-derives on every
run):

| Duplicated decision (before the session) | Decision sites now |
|---|---|
| checkpoint authority selection: the upload route, the resume producer and each retry helper resolved their own authority (Q35-03, R36-03) | **1** composition point (`resolve_repository`), 5 registered composition sites, one selection matrix (`resolve_checkpoint_lookup_authority`) |
| retry checkpoint presence: three provider handlers each ran the raw-index/legacy-token chain | **1** typed adapter (`durable_checkpoint_outcome`) consumed by all three; the legacy chain reduced to **1** opt-in adapter behind `FORGE_RETRY_LEGACY_LOOKUP=1`, deleted at R38 |
| resume-mode selection: every retry dispatch passed an unconditional mode | **1** decision table (`decide_continuation`) with persisted reuse; **0** mode literals in the dispatch service outside the vocabulary definition |
| slot release: per-provider release wrappers defaulting `native_completed=True` | **1** evidence-based release (`release_lease_with_evidence`) + derived occupancy |
| dispatch identity: an envelope `attempt_id` fed a source OID at each entry | **1** composer (`compose_attempt_start`); `AttemptStartSpec` constructed in exactly **2** modules (the type's home and the adapter) |
| GC deletion: unlinks scattered across retention/on-upload/pending-GC paths | **2** sweep entry points; every CAS blob unlink inside them |
| retry refusal wording: per-provider rejection logic | **1** refusal table (`retry_rejection`), consumed verbatim by all three providers |

This slice itself changes no production decision: it adds the registry,
the enforcement tests, the compat fixtures and this documentation. The
production code edits it made are zero — the audit found no live
violation of the six boundaries in the tree (the siblings' landings
already routed every caller through the owners; the only findings were
this slice's own registry being corrected against the true import
graph, which is the registry working as designed).

## Consequences

- A fix to checkpoint authority, continuation or publication now has a
  named owner, and a new caller that bypasses one FAILS CI with an
  instruction — the review's "applies consistently and verifies without
  reading three orchestration implementations".
- Adding a caller to an owned decision is a reviewed registry edit (the
  allow-set change is visible in the diff), not a silent drift; stale
  registrations fail too, so the registry cannot rot.
- Provider differences stay explicit: native APIs, CAS behavior and CI
  identities are NOT erased behind permissive defaults — conformance is
  asserted on the negative contract (typed refusals), and the GitLab/
  Azure lane-resume gap stays the documented honest state until the
  seam is actually wired (#268's finding).
- The monorepo, the modular monolith and the coordinated release stay:
  no packaging change ships here; the boundaries are ownership fences,
  not seams for a split.

## Amendment — 2026-09-24, R37-19 (ADR-0031): the owners stay; the scenarios moved out

The six boundaries, owners and negative contracts above are unchanged
by the R37-19 separation. What changed is where the scenario machinery
that PROVES the owners lives: the deterministic twin, the native-shaped
remote and the reactive scripted vendor moved from inside
`system_verification`, `saga_durable` and `steering_causality` into the
labelled evaluation package `forge.adaptive.reference`, registered in
this registry's `allowed_dependents` like any other caller (so the §2
import-registration fence covers them), with runtime entry points
barred from importing the package entirely and the compatibility
re-exports pinned by tests until their callers drain. See ADR-0031 for
the dependency-direction rules and the residual-duplication audit.
