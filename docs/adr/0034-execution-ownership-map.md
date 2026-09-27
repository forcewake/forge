# ADR-0034: The execution-ownership map — one owner per mature authority decision, callers verified

Status: accepted (2026-09-24) · map refreshed R41-17 (2026-09-27, issue #372)

Context: review `b521e1a`, item R40-17 (issue #353). Previous:
ADR-0033 (execution-ownership consolidation), ADR-0030 (authority
boundary ownership). Dependencies: #337 (feedback ingress), #338
(review rounds), #339 (exposure fold), #340 (budget amendments),
#341/#342 (the grant authority row + identity validation). The
R41-17 refresh (#372) re-verified every caller list against the tree
AFTER this cycle's landings (#356 admission order, #357 durable
ingress, #358 recovery classification, #361 obligation digest) and
records the cycle's own consolidation in §2b.

The mechanisms exist — the R40 cycle landed them all. The residual
defect class is DIVERGENCE: a service, an endpoint, a template and a
report independently deciding what is approved, current, budgeted or
publishable. More classes without production adoption do not fix that.
This ADR is the map from ACTUAL imports and call sites (verified at
write time, file:line below — not Protocols, not test doubles), ONE
extraction that removes a duplicated decision in the same change, and
the honest single-owner entries that were deliberately NOT extracted.

## Decision

### 1. The ownership map (verified production callers)

| Decision | ONE owner | Real production callers (re-verified R41-17/#372) |
| --- | --- | --- |
| **approved input** (which plan text the executor runs) | `forge.adaptive.revisions.resolve_approved_input` → `ApprovedInput` (contract `forge.revision.approved-input/1`) | `forge.runs.service` — every dispatch entry resolves it: the note-command dispatch `src/forge/runs/service.py:5571` and the reviewer continuation `:7221` (import `:130`); the brief is the record's text (`ApprovedInput.brief()`), never a re-render of `spec.plan_summary` (boundary rule R2, ADR-0033 §5). Single real caller — NOT extracted further; the second provider (GitHub) has not adopted the seam and no duplicate decision exists to remove (its dispatch carries the frozen spec brief envelope, a different, older contract). |
| **budget amendment** (whether an operator amendment applies) | `forge.durable.budgets.apply_budget_amendment` — the durable `budget_amendments` table, UNIQUE per `(run, command)`, applied atomically to the enforcement resource; `forge.adaptive.closing_budget.amendment_ledger_document` is the ONE projection of the rows | The operator route AND reviewer continuation on GitLab: `src/forge/runs/service.py:7047` (`continue_review_only:6884`); the GitHub leg's mirrored continuation: `src/forge/runs/github_service.py:6326` (`continue_review_only:6173`). Both record their decision blocks through the shared projection (`service.py:7100`, `:7355`; `github_service.py:6166`, `:6364`). Azure amends nothing (the honest gap below). The extraction is §2. |
| **grant persistence** (which credential authorizes the operation) | `forge.adaptive.credential_broker` — `operation_grant_for_plan` mints, `merge_operation_grant` persists idempotently; the judge is `forge.api_lane_control._authorize_operation_grant` over the keyed `operation_grants` row, with `validate_operation_grant_document` validating the COMPLETE identity | The dispatch mints through the broker (the dispatch-authorization zone); the redemption endpoint loads and judges (the one load site; the persisted-grant surfaces are the registry's `GRANT_PERSISTED_SURFACES`). The pre-029 in-flight bridge reads the run-evidence projection ONLY when no keyed row exists, with the SAME identity validation — the documented compat adapter (`compatibility.legacy_adapter_usage`), fail-closed. No second grant-deciding site reads the projection as authority. |
| **candidate publication** (whether/how a candidate's effects publish) | the trusted write path `forge.runs.publisher.publish_candidate` (ADR-0016/0026: intent persisted before the provider call, the one validated write) + the two-writer phase admission `forge.adaptive.publication_saga` / `saga_durable` | The provider legs publish through the one path: `src/forge/runs/backends.py:268` and `src/forge/runs/service.py:8783`; the saga's NATIVE effect adapters are the registered composition sites (`forge.adaptive.saga_native`, `forge.adaptive.two_writer_qualification`, and the labelled reference remote — contract composition, never a runtime default). No provider publishes around the path; the reconciler's probe-and-resolve (`service.evaluate_publication_intents:8124`, the reconciler pass list `src/forge/runs/reconciler.py`) adopts, never re-publishes blind. |
| **publication recovery** (#358: which outcome a moved head means for an open round) | `forge.runs.service._classify_round_head` + `_probe_round_open_intent` — the EXISTING publication identities decide (commit intents, journal results, `published_candidate`), the R11 marker+parents probe only when an open intent's outcome is unproven AND the head moved | ONE caller: the bounded review-round pass `service._reconcile_one_review_round` (`evaluate_review_rounds:3548`, the classifier at `:3668`) — no provider leg classifies a round head; `tests/test_round_head_classification.py` pins the four-outcome table. Deliberately NOT extracted in #372: the classifier reads service-held seams (the MR head reader, the collaboration branch) — extracting it would add plumbing, not remove decisions (the #372 measurement chose the admission composition instead, §2b). |
| **command acceptance** (#357: whether a delivery is a duplicate / a command is durably accepted) | `forge.gateway.durable_ingress` — `inbox_record_exists` (the authoritative SQL lookup), `cache_confirms_duplicate` (probe→confirm, the orphaned-marker fall-through), `mark_delivered_best_effort` (post-commit positive cache) | All THREE gateways answer through the owner: `src/forge/gateway/router.py` (GitLab — its inline probe→confirm spelling REMOVED by #372 in the same change), `github_webhook.py:797`, `azure_webhook.py:917`. The legacy orchestrator EVENT lane (`router`'s task-queue fall-through, the SET-NX `is_duplicate`) is a DIFFERENT decision whose answer certifies nothing durable — documented on `worker.queue.is_duplicate`, confined to `LEGACY_EVENT_DEDUP_CALLERS`, not removed. |
| **verification applicability** (#361: which obligations a reviewer verdict still owes) | `forge.adaptive.revisions.review_obligation_digest` (+ the `verification_applicability` boundary's owners: `verification_binding`, `verification_sets`, `runs.usecases`) | ONE decision site: the reviewer continuation consults the digest at `src/forge/runs/service.py:7322` (the read-back validation at `:10621` consumes the stored block, never re-derives); the GitHub leg's reuse gate consults the SAME `applicability` contract from the boundary's owner (`github_service.py:6017`). No provider re-derives an obligation list locally. |
| **execution authority** (leases/intents: which run may hold an execution slot) | `forge.adaptive.admission` — `try_acquire_lease` (the CAS insert, idempotent per run) + `release_lease_with_evidence` (the evidence-based release, never a local status guess); `uq_execution_lease_open_run` decides at the database | All three provider legs acquire through the ONE seam: `src/forge/runs/service.py:10219` (GitLab), `github_service.py:7546`, `azure_service.py:1244` — and release through `:10188` / `github_service.py:7510` / `azure_service.py:5111`; the ops drills' force-release is the registered exception (`adaptive.ops_drills`, the `native_occupancy` boundary's dependent list). No provider decides occupancy from its own run status; the registry's `native_occupancy` boundary (import-registration) is the mechanical guard. |
| **feedback admission** (whether a reviewer note is admitted at all) | `forge.gateway.feedback` — the capability flag, the closed verb set (`/fix`, `/ask`), the bounded round policy (`max_review_rounds`, clamped `[0,10]`) | The checked ingress unions the verb set per delivery: `src/forge/gateway/router.py` (zero routing when off — parse-then-refuse is not zero routing); the durable admission zone is `forge.runs.service` — the note handler (request record `:2661`), the round bound read `:3201`, the round admission `_admit_review_round:3107` composing the ONE child-admission service `forge.runs.round_admission` since #372 (§2b — the child opens its OWN budget through `open_budget_from_spec` + `closing-partition/1` inside that transaction, never an amendment of the parent), and the reconciler's bounded correction/round passes (`evaluate_review_corrections:2849`, `evaluate_review_rounds:3548`). |

The machine-readable copy of this map is the boundary registry
(`src/forge/adaptive/boundary_registry.py`): the #353 entries
(`budget_amendment_application`, `feedback_admission`) plus the
`BUDGET_AMENDMENT_APPLICANTS` allow-set, and — since the R41-17 refresh
(#372) — the tenth and eleventh entries (`command_acceptance_dedup`,
`review_round_admission`) with their allow-sets
(`DEDUP_CACHE_PROBE_MODULES`, `LEGACY_EVENT_DEDUP_CALLERS`,
`ROUND_ADMISSION_CALLERS`), all enforced by
`tests/test_architecture_boundaries.py` (each new rule carries
intentional-violation traps — §2b). The evidence-level view
(implemented / wired / native-executed / customer-accepted) is the
closure matrix — `src/forge/adaptive/closure_matrix.py`, published at
`docs/operations/closure-matrix.md` (the seventh capability,
`budget-amendment`, is unchanged by #372 — no new capability was
invented; the two new registry entries guard existing decisions).

### 2. The extraction — one BudgetAmendment application service, three callers, one table

The review's named example, executed. BEFORE this change the decision
"an operator amendment to a review-blocked budget applies" lived in
TWO places:

- the GitLab leg (`runs/service.py::continue_review_only`) applied it
  through `apply_budget_amendment` (#340), but projected the ledger
  with its own inline spelling — twice (the refusal-time record and
  the release record);
- the GitHub leg (`runs/github_service.py::continue_review_only`) still
  applied its own `TopUpLedger`/`BudgetTopUp` into
  `run.evidence["review_budget_block"]["top_ups"]` — the legacy
  evidence ledger #340 left behind, a second authority for the same
  decision.

AFTER this change (one change, the duplicate removed from the previous
owner in the same commit):

- BOTH continuation routes apply through
  `forge.durable.budgets.apply_budget_amendment` — the durable table,
  the originating native command identity, the atomic
  enforcement-resource move;
- the GitHub leg's evidence top-up ledger is GONE (`TopUpLedger`
  construction removed from `github_service.py`; the class remains the
  compat spelling's key derivation — see §6);
- every recorded decision block projects the ledger through the ONE
  `forge.adaptive.closing_budget.amendment_ledger_document` (the full
  audit view + the pinned #325 `top_ups` view of the SAME rows) — the
  four inline spellings replaced;
- the GitHub refusal-time record now checks the EFFECTIVE closing cap
  (policy cap + applied usd amendments) — the same application decision
  the GitLab leg consults.

Caller list, before → after:

| Caller | before | after |
| --- | --- | --- |
| `runs/service.py::continue_review_only` (operator route + reviewer continuation, GitLab) | `apply_budget_amendment` + own inline ledger projection ×2 | `apply_budget_amendment` + the shared projection (`:6369`, `:6208`, `:6452`) |
| `runs/service.py::_record_review_budget_block` (refusal-time record, GitLab) | own inline ledger projection | the shared projection (`:6208`) |
| `runs/github_service.py::continue_review_only` (GitHub) | own `TopUpLedger` on run evidence | `apply_budget_amendment` (`:6326`) + the shared projection (`:6364`) |
| `runs/github_service.py::_record_review_budget_block` (GitHub) | legacy `top_ups` carried from standing evidence | the shared projection over the table (`:6166`) |

The mechanical guard: `BUDGET_AMENDMENT_APPLICANTS` (the owner + the
two provider routes, exhaustive) — a THIRD module constructing or
applying a `BudgetAmendmentCommand` fails the architecture suite, and
a drained applicant registration fails it too. The production-entry
mutation gate MG-2 already pins the deeper invariant (an evidence-only
amendment cannot buy a reviewer call); the GitHub adoption tests pin
the second caller (the table row, the replay, the typed
command-identity refusal, the calls-axis re-open of the guard's limits
row through the same atomic apply MG-2 pins end-to-end —
`tests/test_github_runs.py`,
`TestReviewerBudgetDecisionConsultsTheClosingReserve`).

### 2b. The R41-17 continuation (#372) — the admission composition extracted, the dedup residual removed

Two consolidations, each removing a duplicate decision in the same
change (no dual-authority period):

**The child-admission transaction is a named application service.**
The sequence ONLY `_admit_review_round` knew — FlowRun flush → the
child's OWN budget → the copied frozen spec → the confirmed
reservation → the round row → the admission outbox, ONE commit or
nothing (#356's ordering) — moved to
`src/forge/runs/round_admission.py` (`admit_round_child` + the pure
`plan_round_child` derivation + the admission's own integrity arbiters
`round_slot_integrity_conflict`). The service keeps the eligibility
ladder and every reply/refusal rung (the operator surface) and calls
the owner. The collaborators travel as EXPLICIT immutable context
values (`RoundAdmissionSeams` — the service resolves its own module
globals at call time), so the #356 mutation seams
(`forge.durable.open_budget_from_spec`, `runs.service.Controller`,
`runs.service.MRReservation` in `tests/test_review_rounds.py`) still
drive the REAL admission path, byte-for-byte the same tests. The
kill property was re-proven live after the extraction: reverting the
transaction to the pre-#356 order (the budget opening BEFORE the
child's flush) inside the new module fails all four finite-profile
admission tests exactly as before — the unbudgeted compatibility
profile alone passes, the #356 signature.

Measured: `service.py` 10649 → 10425 lines (**−224** on the old path,
after ruff format); the new module is 464 lines (the moved transaction +
derivation + arbiters, plus the contract surface: the frozen
`RoundChildPlan` / `RoundAdmissionSeams` dataclasses and the module
contract docstring).
The alternative candidate — extracting #358's recovery classification
(`_classify_round_head` + probe + settle helpers, ~268 lines) — was
measured against the same bar and rejected: the classifier reads
service-held seams (the MR head reader, the collaboration-branch
resolver, the provider client), so the extraction would have added
callback plumbing to service.py instead of removing decisions, and it
would have churned #358's freshly landed recovery code.

**The #357 residual is gone.** The GitLab gateway (`router.py`)
spelled the probe→confirm duplicate sequence INLINE beside the owner
helpers — the exact residual the #362 parity agent noted. It now
answers through `cache_confirms_duplicate` /
`mark_delivered_best_effort` like the GitHub and Azure gateways
(router.py 1084 → 1072 lines; the GitLab durable-ingress kill matrix
re-run green, zero semantic change — the same 503s, the same
orphaned-marker fall-through, the same `feedback.duplicate_delivery`
logs).

Two boundary rules now catch a bypass mechanically
(`tests/test_architecture_boundaries.py`, each with traps): the
gateway dedup confinement (`was_delivered` only inside the owner; the
SET-NX `is_duplicate` only in the legacy EVENT lane; the inverse
guards — a gateway that stops calling the helper, or an owner that
loses a seam, is a finding) and the round-admission confinement (the
owner's surface referenced only by the owner and the one admitting
service; inverse guards for a drained registration and a hollowed
transaction).

### 3. Provider-native differences stay in adapters

The consolidation erases DUPLICATED DECISIONS, never platform
capabilities. The named example (R40-16's basis): GitLab publishes
read-before-write (the client re-reads the branch and refuses a moved
head), GitHub publishes through native CAS (compare-and-swap updates).
Both compose the SAME owner — the publication saga's typed errors and
the one validated write path — through `forge.adaptive.saga_native`,
where each provider's REAL preconditions live. No generic
"best-effort publish" default was created by this change or may be:
the conformance table (`tests/test_architecture_boundaries.py`,
`contract.provider_conformance`) shows the shared invariant (typed
refusals equivalent everywhere) AND separately tests the native
differences (the resume-capability matrix records the scripted GitLab
batch lanes resume-incapable rather than fabricating parity). The same
rule holds for the amendment seam: the usd axis is enforced at the
closing gate over usage receipts, the count axes at the guard's row —
the axes are DISTINCT and never converted implicitly.

### 4. Single-owner entries — verified, deliberately NOT extracted

- **Budget opening + the closing partition.** ONE decision owner
  (`forge.durable.budgets.open_budget_from_spec` + the versioned
  `closing_partition` policy `closing-partition/1` in
  `forge.adaptive.closing_budget`). Real call sites: the plan-acceptance
  open (`runs/service.py:1412`) and the review-round admission open —
  since #372 inside the admission transaction itself
  (`runs/round_admission.py:393`) — both route through the SAME seam
  with the SAME policy; that is two callers of one owner, not a
  duplicated decision, so no further extraction (the admission's
  composition around it is §2b's). The GitHub/Azure legs open budgets
  through the same `open_budget_from_spec` seam WITHOUT the partition
  (`github_service.py:968`, `azure_service.py:1027`) — the honest gap:
  the protected closing share is GitLab-only today (recorded in the
  registry entry's `honest_gaps`); inventing partition parity for
  lanes whose review leg is not guard-admitted would promise more than
  those platforms' flows give.
- **The pre-029 grant bridge** (§1, grant persistence) — kept, typed
  and named, while in-flight attempts from before migration 029 can
  still exist. Deletion is the follow-up rung when the compat window
  drains.
- **The GitHub leg's #340 continuation guards.** The GitLab leg stamps
  and checks `authority_expires_at` and the verification binding before
  a paid review (#340); the GitHub leg's continuation does not stamp
  the window yet. Not fabricated here: the guards are addition rungs,
  not duplicated decisions (the amendment decision itself — §2 — is
  now shared).

### 5. Synthetic executors and qualification fixtures never load in production startup

- The labelled evaluation package (`forge.adaptive.reference` — the
  scripted-causal vendor, the reference remotes, the system twin) is
  barred from every runtime entry point by the reference-separation
  rule (ADR-0031/#300) — unchanged.
- NEW (this change): the qualification-fixtures axis.
  `forge.adaptive.compat_fixtures` (the versioned compat documents)
  has ZERO production importers — verified and now PINNED by the
  fixture-isolation rule in `tests/test_architecture_boundaries.py`
  (any src/forge module importing it, module-level or lazy, is an
  architecture failure, with an intentional-violation trap), plus a
  runtime probe that imports the default production startup surface
  (`forge.main`, `forge.adaptive.wiring`, `forge.worker.queue`) in a
  clean interpreter and asserts no fixture or reference module loads.
  This is the `architecture.shadow_authority_count` axis: fixture code
  can never become a deployed authority by import.

### 6. Compatibility and the rollback/forward plan

- **Immutable RunSpecs and active attempts** keep loading through the
  documented adapters (ADR-0018/0031): the spec legacy readings
  (`SpecInvalid`/`SpecLegacy` typed refusals) and the pre-029 grant
  bridge (§1) are unchanged by this ADR.
- **The legacy top-up spelling** (`top_up_usd`/`top_up_reason` on the
  GitHub continuation) is a documented adapter onto the axis model: a
  usd-axis amendment whose command identity is the legacy
  content-derived key (`BudgetTopUp.idempotency_key`) — the legacy
  replay semantics (same amount+reason+operator applies once)
  preserved exactly, now enforced by the table's `(run, command)`
  unique index instead of an evidence map. The NATIVE spelling
  (`command_id` + `axis` + `amount` + `reason`) is the forward
  contract and REQUIRES the originating command identity — both legs
  refuse typed without it (`amendment_requires_command_identity`).
- **Mixed-worker deployment** (the storage-affecting question): the
  extraction adds no new table — it MIGRATES a decision onto the
  `budget_amendments` table #340 already created (migration 030). An
  old worker (pre-#353) records a GitHub top-up into run evidence; a
  new worker reads the amendment table. Neither doubles-applies (the
  release semantics are one-shot on both sides), and the operator
  surface (`budget_blocked_review`, `operator_view.py`) folds whatever
  the recorded decision block carries — a projection, never a second
  ledger. Forward: no backfill is needed (a pre-migration evidence
  top-up moved no enforcement capacity, so the table correctly holds
  no row for it); the evidence history remains readable as the
  immutable trail it is. Rollback: revert this change — the GitHub leg
  returns to its evidence ledger, the table's rows become inert for
  that leg (they still answer the audit read), and no data is lost.

## Consequences

- The answer to "who decides X" is one table row (§1) with file:line
  callers, backed by registry data and closure-matrix evidence levels —
  the per-cycle docstring archaeology has a successor.
- A third amendment applicant, a fixture import, or a re-introduced
  inline ledger projection is now a mechanical test failure, not a
  review-time discovery.
- The GitHub leg's operator amendment behavior is pinned by its own
  suite staying green (the legacy spelling) plus the new adoption
  tests (the table, the replay, the typed refusals) — the second
  caller adoption the review asked for, with the previous owner's
  duplicate removed in the same change.
