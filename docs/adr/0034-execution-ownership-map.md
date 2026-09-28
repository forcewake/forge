# ADR-0034: The execution-ownership map — one owner per mature authority decision, callers verified

Status: accepted (2026-09-24) · map refreshed R41-17 (2026-09-27, issue #372)
· refreshed R42-16 (2026-09-28, issue #389)

Context: review `b521e1a`, item R40-17 (issue #353). Previous:
ADR-0033 (execution-ownership consolidation), ADR-0030 (authority
boundary ownership). Dependencies: #337 (feedback ingress), #338
(review rounds), #339 (exposure fold), #340 (budget amendments),
#341/#342 (the grant authority row + identity validation). The
R41-17 refresh (#372) re-verified every caller list against the tree
AFTER this cycle's landings (#356 admission order, #357 durable
ingress, #358 recovery classification, #361 obligation digest) and
records the cycle's own consolidation in §2b. The R42-16 refresh
(#389) re-verified every caller list again after THREE landings'
worth of drift (#374-#376 the typed observation outcomes, conservative
occupancy and the credential-mode preflight; #378 the composed fault
traces; #377 the build-once qualification; #379 the budget
calibration; #380 the command-to-delivery progress view), adds the
new owners those landings created (§1: command completion outcomes,
credential delivery preflight resolution, the promotion composition
guard) and records this cycle's own consolidation in §2c.

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

| Decision | ONE owner | Real production callers (re-verified R42-16/#389) |
| --- | --- | --- |
| **approved input** (which plan text the executor runs) | `forge.adaptive.revisions.resolve_approved_input` → `ApprovedInput` (contract `forge.revision.approved-input/1`) | `forge.runs.service` — every dispatch entry resolves it: the note-command dispatch `src/forge/runs/service.py:6089` (inside `_advance_proposal:5510`) and the reviewer continuation `:8004` (inside `_review_and_ready:8035`; import `:131`); the brief is the record's text (`ApprovedInput.brief()`), never a re-render of `spec.plan_summary` (boundary rule R2, ADR-0033 §5). Single real caller — NOT extracted further; the second provider (GitHub) has not adopted the seam and no duplicate decision exists to remove (its dispatch carries the frozen spec brief envelope, a different, older contract). |
| **budget amendment** (whether an operator amendment applies) | `forge.durable.budgets.apply_budget_amendment` — the durable `budget_amendments` table, UNIQUE per `(run, command)`, applied atomically to the enforcement resource; `forge.adaptive.closing_budget.amendment_ledger_document` is the ONE projection of the rows | The operator route AND reviewer continuation on GitLab: `src/forge/runs/service.py:7830` (`continue_review_only:7667`); the GitHub leg's mirrored continuation: `src/forge/runs/github_service.py:6326` (`continue_review_only:6173`). Both record their decision blocks through the shared projection (`service.py:7658`, `:7913`; `github_service.py:6166`, `:6364`). Azure amends nothing (the honest gap below). The extraction is §2. |
| **grant persistence** (which credential authorizes the operation) | `forge.adaptive.credential_broker` — `operation_grant_for_plan` mints, `merge_operation_grant` persists idempotently; the judge is `forge.api_lane_control._authorize_operation_grant` over the keyed `operation_grants` row, with `validate_operation_grant_document` validating the COMPLETE identity | The dispatch mints through the broker (the dispatch-authorization zone); the redemption endpoint loads and judges (the one load site; the persisted-grant surfaces are the registry's `GRANT_PERSISTED_SURFACES`). The pre-029 in-flight bridge reads the run-evidence projection ONLY when no keyed row exists, with the SAME identity validation — the documented compat adapter (`compatibility.legacy_adapter_usage`), fail-closed. No second grant-deciding site reads the projection as authority. |
| **candidate publication** (whether/how a candidate's effects publish) | the trusted write path `forge.runs.publisher.publish_candidate` (ADR-0016/0026: intent persisted before the provider call, the one validated write) + the two-writer phase admission `forge.adaptive.publication_saga` / `saga_durable` | The provider legs publish through the one path: `src/forge/runs/backends.py:268` and `src/forge/runs/service.py:9571`; the saga's NATIVE effect adapters are the registered composition sites (`forge.adaptive.saga_native`, `forge.adaptive.two_writer_qualification`, and the labelled reference remote — contract composition, never a runtime default). No provider publishes around the path; the reconciler's probe-and-resolve (`service.evaluate_publication_intents:8907`, the reconciler pass list `src/forge/runs/reconciler.py`) adopts, never re-publishes blind. |
| **publication recovery** (#358: which outcome a moved head means for an open round) | `forge.runs.service._classify_round_head` + `_probe_round_open_intent` — the EXISTING publication identities decide (commit intents, journal results, `published_candidate`), the R11 marker+parents probe only when an open intent's outcome is unproven AND the head moved | ONE caller: the bounded review-round pass `service._reconcile_one_review_round` (`evaluate_review_rounds:3809`, the classifier at `:3929`) — no provider leg classifies a round head; `tests/test_round_head_classification.py` pins the four-outcome table. Deliberately NOT extracted in #372: the classifier reads service-held seams (the MR head reader, the collaboration branch) — extracting it would add plumbing, not remove decisions (the #372 measurement chose the admission composition instead, §2b). |
| **command acceptance** (#357: whether a delivery is a duplicate / a command is durably accepted) | `forge.gateway.durable_ingress` — `inbox_record_exists` (the authoritative SQL lookup), `cache_confirms_duplicate` (probe→confirm, the orphaned-marker fall-through), `mark_delivered_best_effort` (post-commit positive cache) | All THREE gateways answer through the owner: `src/forge/gateway/router.py` (GitLab — its inline probe→confirm spelling REMOVED by #372 in the same change), `github_webhook.py:797`, `azure_webhook.py:917`. The legacy orchestrator EVENT lane (`router`'s task-queue fall-through, the SET-NX `is_duplicate`) is a DIFFERENT decision whose answer certifies nothing durable — documented on `worker.queue.is_duplicate`, confined to `LEGACY_EVENT_DEDUP_CALLERS`, not removed. |
| **command completion outcomes** (#374, new since the R41 map: what "done" means for an accepted feedback command) | `forge.runs.service` — the typed observation-outcome stream: `_journal_feedback_outcome` (`src/forge/runs/service.py:4451`, one `feedback.outcome` row per CHANGED outcome), `_outcome_of_status` + `_FEEDBACK_REFUSED_STATUSES` (`:657`, `:641` — which request lifecycle statuses are the REFUSED class), the closed vocabulary `completed\|refused\|pending\|exhausted` and the `provider_observation.retry` deferral trail; a handler's normal return IS a terminal outcome ONLY (the plain-return defect #374 closed — a retryable observation RAISES `ReviewObservationRetryable` and rides the step runtime's bounded backoff) | The writer is the durable admission zone itself (the note handler `handle_review_feedback_note:2753`, its refusal rungs and the deferral path `_defer_review_observation`); the readers never re-derive: the #380 command axis (`forge.adaptive.operator_view.command_progress:5137`, the vocabulary at `:4620`) derives accepted/pending/applied/refused/exhausted from the JOURNAL rows with the request lifecycle as a journal-outranked FALLBACK whose refused-status set is parity-confined to the authority by the R42-16 rule (§2c); the worker records step success only on the handler's normal return (`worker/steps.py::execute_claimed_step`); the operator repair query `feedback_steps_without_outcome` (`service.py:11321`) surfaces the historical succeeded-without-outcome damage from the step side. No layer interprets a status more strongly than the journal word. |
| **credential delivery preflight resolution** (#376, new since the R41 map: which delivery plan a dispatch WOULD resolve) | `forge.adaptive.credential_broker.delivery_plan` (`credential_broker.py:1985`) — the ONE dispatch seam; `forge.adaptive.credential_preflight.resolve_delivery_plan` (`credential_preflight.py:271`) is the SAME function object invoked with the deployment defaults (the `FORGE_CREDENTIAL_BINDINGS` registry, the ambient `EnvBroker`), reading refs and declared routes only — no value is redeemed | The dispatch legs resolve their plans through the seam: `src/forge/runs/service.py:6155`, `github_service.py:4098`, `azure_service.py:2911`; the diagnostic surface composes the same resolver (`credential_delivery_preflight` → `forge.doctor`'s report, `doctor.py:457`) — the same-resolver property test (`tests/test_credential_preflight.py`) fails on a disconnected consumer mapping, so doctor's plan can never drift from dispatch's. |
| **promotion composition guard** (#377, new since the R41 map: whether a promotion's built bytes are the qualified bytes) | `scripts/generate_template_pins.py --exact-composition` (`exact_composition_findings:154`) — refuses a promotion whose built wheel differs from the strict qualification record's digest, an unbound pending candidate, or any artifact naming other bytes (the v0.41.0 shape: the live traces ran the pre-final build) | The CI gate `.github/workflows/ci.yml:55` and the release gate `.github/workflows/release.yml:107` — both refuse the pipeline, never warn-and-continue; the qualification records bind the digests BEFORE qualification (the process half, `docs/qualification/2026-09-28-r4204-build-once/`). Not a src/forge runtime decision: it owns the BUILD/RELEASE boundary, which is why it lives in the repo's script + workflow layer. |
| **verification applicability** (#361: which obligations a reviewer verdict still owes) | `forge.adaptive.revisions.review_obligation_digest` (+ the `verification_applicability` boundary's owners: `verification_binding`, `verification_sets`, `runs.usecases`) | ONE decision site: the reviewer continuation consults the digest at `src/forge/runs/service.py:8105` (the read-back validation `_review_reuse_refusal:11294` consumes the stored block, never re-derives); the GitHub leg's reuse gate consults the SAME `applicability` contract from the boundary's owner (`github_service.py:6017`). No provider re-derives an obligation list locally. |
| **execution authority** (leases/intents: which run may hold an execution slot) | `forge.adaptive.admission` — `try_acquire_lease` (the CAS insert, idempotent per run) + `release_lease_with_evidence` (the evidence-based release, never a local status guess); `uq_execution_lease_open_run` decides at the database | All three provider legs acquire through the ONE seam: `src/forge/runs/service.py:11137` (GitLab), `github_service.py:7546`, `azure_service.py:1244` — and release through `:11106` / `github_service.py:7510` / `azure_service.py:5111`; the ops drills' force-release is the registered exception (`adaptive.ops_drills`, the `native_occupancy` boundary's dependent list). No provider decides occupancy from its own run status; the registry's `native_occupancy` boundary (import-registration) is the mechanical guard. The #375 occupancy CORRELATION rewrite lives beside the seam: `service._classify_branch_pipeline:10632` + `_branch_search_verdict:10701` (safety-first — any current OR ambiguous active row → RUNNING before any all-terminal verdict; verification pipelines excluded by GitLab's canonical `merge_request_event`; row order never matters) — the ONE reducer of raw branch-listing observations, whose verdict the GitLab occupancy probe always flows through (`_native_occupancy_probe:10976`); the projection side consumes only the DERIVED lease word (`admission.lease_occupancy`), never raw pipelines. |
| **feedback admission** (whether a reviewer note is admitted at all) | `forge.gateway.feedback` — the capability flag, the closed verb set (`/fix`, `/ask`), the bounded round policy (`max_review_rounds`, clamped `[0,10]`) | The checked ingress unions the verb set per delivery: `src/forge/gateway/router.py` (zero routing when off — parse-then-refuse is not zero routing); the durable admission zone is `forge.runs.service` — the note handler (request record `:2851`), the round bound read `:3473`, the round admission `_admit_review_round:3351` composing the ONE child-admission service `forge.runs.round_admission` since #372 (§2b — the child opens its OWN budget through `open_budget_from_spec` + `closing-partition/1` inside that transaction, never an amendment of the parent), and the reconciler's bounded correction/round passes (`evaluate_review_corrections:3097`, `evaluate_review_rounds:3809`). |

The machine-readable copy of this map is the boundary registry
(`src/forge/adaptive/boundary_registry.py`): the #353 entries
(`budget_amendment_application`, `feedback_admission`) plus the
`BUDGET_AMENDMENT_APPLICANTS` allow-set, and — since the R41-17 refresh
(#372) — the tenth and eleventh entries (`command_acceptance_dedup`,
`review_round_admission`) with their allow-sets
(`DEDUP_CACHE_PROBE_MODULES`, `LEGACY_EVENT_DEDUP_CALLERS`,
`ROUND_ADMISSION_CALLERS`), all enforced by
`tests/test_architecture_boundaries.py` (each new rule carries
intentional-violation traps — §2b). Since the R42-16 refresh (#389) the
registry also carries the command-outcome PARITY rule-set
(`COMMAND_OUTCOME_JOURNALING_MODULE` / `COMMAND_OUTCOME_PROJECTION_MODULE`,
wired into the `operator_projection` boundary's enforcement — §2c): the
two spellings of the refused-status classification must agree exactly,
a mechanical failure on drift in either direction. The evidence-level
view (implemented / wired / native-executed / customer-accepted) is the
closure matrix — `src/forge/adaptive/closure_matrix.py`, published at
`docs/operations/closure-matrix.md` (the seventh capability,
`budget-amendment`, is unchanged by #389 — no new capability was
invented; the parity rule guards an existing decision).

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

### 2c. The R42-16 continuation (#389) — the #379 display pin landed, the command-outcome parity rule added

Two consolidations against this cycle's fresh seams, each removing or
confining a divergent second decision (no dual-authority period):

**The #379 named gap is closed: the GitLab budget display pin.** The
GitLab `RunService` opens the run's budget row at the DEFAULT class's
numbers BEFORE the first paid call (the planner itself reserves —
`runs/service.py::open_budget` in `_plan_and_publish`), then recompiles
the harness selection against the CURRENT plan (#379's planner
assessment: `budget_class` is a REQUEST). BEFORE this change the
recompiled selection's resolved ceilings rode into the frozen spec's
budgets block UNPINNED — the spec could display the assessment's
ceilings while the durable row kept the pre-plan ones (the #379 test
recorded the shape: the gate "approved" 40k while the row enforced the
pre-plan 600k). AFTER this change the recompile is followed by the
GitHub leg's B11 pin, verbatim in intent: the selection KEEPS the
planner's class, its ceilings are pinned to the opened row's numbers,
and the pin is loud in the selection reason
(`(budget pinned to the pre-plan class — idempotent budget row)`). The
divergent second decision — an independently resolved ceiling set
riding into the spec — is GONE in the same change; the spec's budgets
block, the run evidence's budget record and the durable `RunBudget`
row now carry ONE set of numbers (pinned both directions by
`tests/test_budget_calibration.py::TestPlannerAssessment` — the
escalation arm AND the downgrade arm). A second divergence the pin
also removes: review-round children open their OWN budget from the
frozen spec (`runs/round_admission.py:393`), so a child previously
opened at the assessment's numbers while the parent's row held the
pre-plan ones — the child now opens at exactly the parent's.

Measured: the pin is +24 lines in `runs/service.py` (11409 → 11433;
one conditional block, no new module — the decision the GitHub leg
already made, now spelled once per provider leg with the SAME reason
text); the calibration test file +81/−16 (714 → 779; the strengthened
arms). No durable record changes shape: an already-approved run's
frozen spec keeps its digest and its numbers (the pin affects only
FUTURE freezes — an assessment never moves an opened row, and now it
never moves a displayed one either); the Azure leg carries the SAME
pre-plan shape (`azure_service.py:869`/`:904`) and remains the honest
gap in §4.

**The command-outcome classification is parity-confined.** The #374
typed-outcome stream (§1's new "command completion outcomes" row) is
the journaling authority; the #380 projection's request-lifecycle
FALLBACK (`operator_view._REQUEST_REFUSED_STATUSES` +
`_JOURNAL_OUTCOME_TO_AXIS`) spells the same classification for the
no-journal window — pre-#374 requests and identity-only rows. The two
spellings agreed at write time (the same ten refused statuses, the
same four-word vocabulary) but NOTHING pinned them: a status the
authority journals `refused` while the fallback does not know it would
render APPLIED (complete) on the fallback path — exactly the
one-layer-interprets-more-strongly defect class. The projection module
landed this cycle (#380) and is change-frozen for this issue, so the
consolidation instrument is the PARITY RULE, not code motion: the
registry's `COMMAND_OUTCOME_JOURNALING_MODULE` /
`COMMAND_OUTCOME_PROJECTION_MODULE` pair, enforced by the new
`command outcome parity` check in `tests/test_architecture_boundaries.py`
— the two refused-status sets must be EQUAL, the journal-word mapping
must equal the authority's journaled vocabulary, renames on either
side are findings, and the real-tree contrast arm pins the extracted
VALUES (ten statuses, four words) so the rule can never pass vacuously
over two silently-empty extractions. Unknown outcome words stay
representable: the projection maps an unmapped word to the honest
`unknown` axis word — the rule pins that no KNOWN word is ever
silently degraded. The follow-up rung (one import onto the authority's
vocabulary, deleting the fallback's literal copy) is §4's first entry.

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

- **The command-outcome fallback's literal copy (R42-16's §2c).** The
  projection's `_REQUEST_REFUSED_STATUSES` literal set duplicates the
  authority's `_FEEDBACK_REFUSED_STATUSES` BY VALUE (ten statuses) and
  is parity-confined, not imported: `forge.adaptive.operator_view`
  landed in #380 this cycle and is change-frozen for #389's file
  ownership. Deleting the copy for one import onto
  `forge.runs.service`'s vocabulary (or moving both onto the revisions
  constants the authority already reads) is the NEXT rung — a
  one-line, trap-covered change once the freeze lifts; until then the
  parity rule makes drift a mechanical failure, not a review-time
  discovery.
- **The Azure leg's pre-plan display pin.** `azure_service.py` carries
  the SAME shape the #379 pin closed on GitLab (the pre-plan open at
  `:873`, the unpinned post-plan recompile at `:904`) — its spec's
  budgets block can display the assessment's ceilings while the row
  keeps the pre-plan ones. Verified, deliberately not fixed here: the
  file is outside #389's ownership and the guard enforces the row
  (safe in both directions, the same argument #379 recorded for
  GitLab). The pin, when it lands, is the same five lines the GitLab
  and GitHub legs already spell.
- **Budget opening + the closing partition.** ONE decision owner
  (`forge.durable.budgets.open_budget_from_spec` + the versioned
  `closing_partition` policy `closing-partition/1` in
  `forge.adaptive.closing_budget`). Real call sites: the spec-freeze
  open (`runs/service.py:1600`, opened from the frozen spec after the
  pre-plan row — idempotent, limits never moved) and the review-round
  admission open — inside the admission transaction itself
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
- **The R42-16 display pin** (§2c) is storage-neutral: no new table,
  no migration, no column. A run frozen BEFORE the pin keeps its
  existing `RunSpec` digest and its recorded ceilings — both the old
  and the new spec documents load through the unchanged
  `_load_executable_spec` digest-verified read (`compatibility.
  legacy_record_readability`: a pre-pin spec that displays the
  assessment's ceilings remains a truthful record of what its freeze
  wrote; the durable row was and remains the enforcement authority).
  Rollback: revert the pin block — future freezes re-diverge (the
  recorded, guarded shape #379 documented), nothing on disk changes
  meaning.

## Consequences

- The answer to "who decides X" is one table row (§1) with file:line
  callers, backed by registry data and closure-matrix evidence levels —
  the per-cycle docstring archaeology has a successor.
- A third amendment applicant, a fixture import, or a re-introduced
  inline ledger projection is now a mechanical test failure, not a
  review-time discovery. Since R42-16 so is a diverged command-outcome
  classification: a refused status (or a journal word) known to one
  spelling and not the other fails the architecture suite with the
  diverging element NAMED (`architecture.authoritative_call_sites`).
- The GitHub leg's operator amendment behavior is pinned by its own
  suite staying green (the legacy spelling) plus the new adoption
  tests (the table, the replay, the typed refusals) — the second
  caller adoption the review asked for, with the previous owner's
  duplicate removed in the same change.
- The #379 pin means an operator reading a GitLab plan comment, the
  frozen spec or the budget row sees the SAME numbers everywhere on
  every provider that pins (GitLab since R42-16, GitHub since B11) —
  the assessment names the class and the reason, the row's numbers are
  the ones enforced, and Azure is the recorded exception until its pin
  lands.
