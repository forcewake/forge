# The production-entry mutation gates (R40-08, issue #344)

External review `b521e1a`, item R40-08. CI greenness proved nothing
about four high-risk invariants: a service-level test can pass a
correct grant explicitly, invoke a reconciler method directly, or use
a reviewer double that ignores the budget — it stays green while the
INSTALLED command cannot reach the feature or its guard is unchanged.

This document names the REQUIRED composed-trace set (`tests/
production_entry/test_mutation_gates.py` + the landed `test_feedback_
ingress.py`), its mutation arms, the honesty rules both obey, and the
report plumbing that keeps their evidence separable.

## The trace set — one per high-risk invariant, each WITH its mutant

Every trace drives the same entry points a customer invokes (real ASGI
ingress, the installed worker/reconciler loops, the real dispatch leg,
the real `BudgetGuard` over a real recording HTTP endpoint, real lane
and collector subprocesses, real durable state). Every mutation arm
runs the SAME event sequence a second time with exactly ONE defect
seeded — a patch applied to the SHIPPED symbol at test time
(`monkeypatch` on the module attribute, resolved via `getattr` so a
moved seam fails loudly), the way a regression would reintroduce it.
The arm asserts the DEFECT's observable; the baseline's assertions fail
under the patch, which is precisely what makes the set a detector.
Nothing whose production creation is under test is ever seeded.

| Invariant | Baseline trace | Mutation arm (the seeded defect) | Pins |
| --- | --- | --- | --- |
| **MG-A — feedback ingress** (#337) | `TestFI1TheWiredIngressTrace` (test_feedback_ingress.py): token-authenticated note → REAL ASGI ingress → the ADR-0017 §1 inbox transaction → a RESTARTED worker (the installed `run_step_worker` loop) → the human `/approve-revision` → the INSTALLED reconciler re-drives the correction | `TestFI5TheRegistrationMutations`: the parser registration removed (capability flag off) — nothing is ever ingested; the reconciler's correction pass neutralized — the approved correction never re-drives | The installed path reaches the handler; both registrations are load-bearing |
| **MG-1 — partial-liability admission** (#339) | `TestMG1PartialLiabilityAdmission::test_a_partial_subtotal_never_settles_the_closing_admission`: settled 8.00 FINAL + a PARTIAL at 0.50 inside a 3.00 envelope, cap 10.00 — bounded exposure 11.00, the closing review does NOT fit, the run is BLOCKED having never contacted the provider | `..._fits_the_unfittable`: `exposure_fold` reverted to VALUE PRESENCE (any cost settles, only costless rows reserve) — exposure reads 8.50, the run wrongly stays REVIEWING | Finality, never value presence, settles liability; a partial's subtotal rides INSIDE the retained envelope |
| **MG-2 — guarded review amendment** (#340 / AT-04) | `TestMG2GuardedReviewAmendment::test_a_calls_amendment_reopens_the_real_guard_for_exactly_one_review`: the lane's own artifact receipt exhausts the one-call budget; the REAL `LLMReviewer` over the REAL `LLMClient` is refused by the ACTUAL `BudgetGuard`; a calls-axis amendment re-opens the enforcement row and EXACTLY ONE reviewer call reaches the provider; the redelivery applies once and re-reviews nothing | `..._cannot_buy_a_reviewer_call`: `apply_budget_amendment` reverted to EVIDENCE-ONLY (amount/reason recorded, the limits row untouched) — the shipped pre-review capacity check still refuses, NOTHING reaches the provider | A reviewer double cannot bypass `BudgetGuard`; the amendment moves the enforcing resource, not an annotation |
| **MG-3 — grant persistence under concurrent evidence** (#341) | `TestMG3GrantPersistenceUnderConcurrentEvidence::test_the_projection_preserves_a_concurrent_evidence_write`: the redemption-mode dispatch persists its operation grant while a checkpoint-shaped evidence write lands in the window — BOTH the keyed authority row and the concurrent key survive | `..._drops_the_concurrent_evidence`: the projection writer reverted to the WHOLE-DOCUMENT overwrite — the concurrent key is silently dropped | The projection is a targeted CAS; nobody's evidence is lost to the grant write |

### Mutation evidence (the proof protocol)

Each baseline was run with its defect seeded through a throwaway `-p`
plugin (the same patch the arm applies) and FAILED; green without. The
seeded-defect failures, quoted:

- **MG-1** (value-presence fold seeded):
  `AssertionError: assert 'reviewing' == 'blocked'` — the unfittable
  closing review was admitted.
- **MG-2** (evidence-only amendment seeded):
  `AssertionError: {'allowed': False, … 'budget.refused_axis': 'calls' …} — assert False is True`
  — the amendment claimed applied, the real guard refused.
- **MG-3** (whole-document writer seeded):
  `AssertionError: assert None == {'bytes': 4096, 'slot': 'wip'}` —
  `evidence.get('checkpoint_probe')` after the stale stomp: the
  concurrent write was gone.

The service-level siblings remain: `tests/test_operation_grant.py`
pins the same #341 interleave at the unit seam; `tests/
test_budget_amendment.py::TestAmendmentOpensTheRealGuard` is AT-04's
guard-only bar. The MG traces are the INSTALLED-path layer above them.

## Honesty rules

- The mutation arms patch the SHIPPED module the way a regression
  would reintroduce the defect — never a test double standing in for
  the shipped code, never a source-file edit.
- Baselines never seed the state whose production creation is under
  test (the receipts ride the durable `ingest_usage_receipt` front
  door; the lane's own artifact exhausts MG-2's budget; the grant
  rides the real dispatch leg).
- Every arm runs the SAME event sequence as its baseline — only the
  one mutation differs.

## The gate manifest (pg_gate)

`scripts/pg_gate.py` treats all five trace classes as REQUIRED traces
(class-prefix patterns, so a removed test OR a removed arm is the
marker-removal mutation the manifest check detects):

- a REQUIRED trace whose skip names `FORGE_PG_TEST_URL` classifies
  `prerequisite_missing` — the DISTINCT, named outcome (issue
  acceptance 6): the profile is reported **UNQUALIFIED** under
  `PrerequisiteError` (exit 2), never an invisible skip and never
  folded into a generic failure bucket;
- the report artifact (`pg-gate-qualification-report`) enumerates the
  executed critical test ids with their **exact source sha256**
  (`qualification.critical_test_sources`) — an executed record is
  bound to the bytes that ran, not to a matched pattern alone;
- `gate.source_identity` distinguishes the executed-on basis:
  `source-main` (git commit + dirty flag) vs `release-artifact`
  (`FORGE_RELEASE_ARTIFACT_SHA256`, the canary's spelling) — an
  artifact run can never present itself as source-main evidence;
- the traces write one machine-readable record per executed
  baseline/arm when the gate exports `FORGE_TRACE_RECORD_DIR`
  (schema `forge.trace-record/1`: label, mutation, outcome, duration,
  `evidence_class: production-entry (offline composed trace)`, and
  the same source identity). The gate embeds them into the report
  (`qualification.mutation_gate_trace_records`) so the CI artifact is
  self-contained. Unset, the traces write nothing — fully hermetic.

## Evidence classes stay separate

Unit / integration / native-live / customer evidence never mixes:

- the MG records carry `evidence_class: production-entry (offline
  composed trace)` — offline fakes, real subprocesses and databases;
- `pg_gate`'s `execution_profile` stays `postgres-integration
  (service-container PostgreSQL)`;
- the release canary records its own class against the artifact
  digest; the source identity's `basis` field keeps the two from
  being conflated in any downstream report.

## Teardown

See `tests/conftest.py`'s `_close_abandoned_authority_reader_sessions`
for the aiosqlite thread/closed-loop fix this issue landed at its
allocation origin (the lane-control authority reader's unclosed
session), including why it lives in the test conftest while the source
module is sibling issue #338's active zone.
