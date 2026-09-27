# The production-entry mutation gates (R40-08, issue #344 — the R41-07 / #362 matrix appended)

External review `b521e1a`, item R40-08. CI greenness proved nothing
about four high-risk invariants: a service-level test can pass a
correct grant explicitly, invoke a reconciler method directly, or use a
reviewer double that ignores the budget — it stays green while the
INSTALLED command cannot reach the feature or its guard is unchanged.

This document names the REQUIRED composed-trace set (`tests/
production_entry/test_mutation_gates.py` + the landed `test_feedback_
ingress.py`), its mutation arms, the honesty rules both obey, and the
report plumbing that keeps their evidence separable. The **production
matrix** (R41-07 / issue #362, external review `68f22b8`) extends the
same discipline to the COMBINATIONS the review said the components
missed — see the last section.

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

## The production matrix (R41-07, issue #362)

External review `68f22b8`: the suite was green and the R41 defects
lived in COMBINATIONS — admission + finite budget, own commit + head
fence, Redis marker + failed persistence. `tests/production_entry/
test_production_matrix.py` is the small REQUIRED matrix that closes
that gap: every arm is a COMPLETE production path driven through native
webhook routing (a REAL ASGI gateway subprocess), durable worker entry
(the INSTALLED `run_step_worker` + `run_reconciler` + `run_step_reaper`
composition in its own OS process — `production_matrix_worker.py`), the
ACTUAL budget helpers, real SQL transactions and the SHIPPED collector
(`python -m forge.harness_entry --collect-candidate`). The stubs replace
the model only (the deterministic agents behind `build_default_agents`)
and the runner's file edits — never the policy under test.

### MX-1 — the matrix arms (24, parametrized)

`finite/unlimited × builtin/harness × initial/correction/retry ×
Redis-present/absent` on the GitLab lane, one arm per cell. Every arm
runs the SAME baseline sequence (issue → `/implement` → `/go` → the
backend's publication leg → the named leg's own sequence → the replay
probe) and asserts every axis it crosses:

- **budget** — `finite`: the root run's frozen budget row (read through
  `budget_for_run`) with a `budget_block_reason` of `None`; `unlimited`:
  NO budget row, by design;
- **leg** — `initial`: the candidate committed natively, the Draft MR on
  the collaboration branch, the closing review carrying its
  `obligation_digest` (#361); `correction`: the round child with an
  INDEPENDENT run id on the SAME persisted collaboration target, its OWN
  budget under the finite axis, the MR reservation and the dispatch on
  the target branch (#356 + #359 composed); `retry`: the re-dispatch
  rides the target branch (the harness lane uses the documented
  `restart` verb — its vendor state is unprovable by design);
- **backend** — `harness`: the dispatch ledger carries the frozen
  RunSpec variables; `builtin`: the REAL ChangesetWriter committed the
  candidate;
- **redis** — the same-delivery replay answers `deduplicated` with
  exactly ONE durable `/go` step, marker or not.

### MX-2 — the fault windows (real process kills)

| Window | The kill | The recovery it proves |
| --- | --- | --- |
| acceptance commit | the GATEWAY process SIGKILLed after the `/fix` inbox commit, before the response leaves | the committed `review_feedback` step IS the command — a fresh worker admits the round with NO resend (#357 on the correction path) |
| child admission | the WORKER process SIGKILLed inside the round admission transaction (after the child flush, at the child-budget open) | NOTHING partial survives — no round, child, budget, reservation or outbox; the redelivered `/fix` admits exactly one round (#356's atomicity at a process boundary) |
| native commit | the WORKER process SIGKILLed after the round child's `create_commit` returned, before the journal | the OPEN publication intent recovers through the installed reconciler's R11 probe — ONE commit total, the round survives (#358's precondition) |
| journal completion | the WORKER process SIGKILLed after the writer's whole `apply()` returned (intent + commit + journal durable, the child mid-`committing`) | the round pass CLASSIFIES the head as the round's OWN effect (`review_round.effect_resolution{own}`) and adopts it — one commit, no stale (#358) |
| occupancy mixed history | no kill — the draining lease over a branch listing with a PRIOR attempt's terminal pipeline beside the CURRENT attempt's running one | mixed history KEEPS the lease; only the CORRELATED terminal releases it, exactly once (#360) |

The gateway and the worker are killed INDEPENDENTLY (MX-2a kills only
the API; MX-2b/2c/2d kill only the worker while the API stays up) —
never a cancelled asyncio task. A worker killed mid-step has its lease
expired by the trace's clock advance and the REAL step reaper
(`run_step_reaper`, part of the harness's installed composition)
reschedules it.

### MX-3 — the mutation pairings

Each arm seeds ONE regression into the process that executes the policy
and runs the SAME baseline sequence as its paired matrix arm, asserting
the DEFECT's observable:

| Arm | The seeded defect (the pre-fix behavior) | The defect's observable |
| --- | --- | --- |
| (a) `budget-before-child` | the pre-#356 admission ordering — the child budget opened before the child run existed, so `open_budget`'s `RunNotFound` guard fired for every finite spec (spelled at the seam the reorder moved: the guard raises for exactly the uncommitted-child call) | the finite round admission dies: no round, no child, no child budget — the finite correction arm's assertions all fail |
| (b) `pre-commit-cache` | the pre-#357 ingress (SET-NX before the transaction) — the #357 harness mutant, extended to the matrix's CORRECTION path | the `/fix` retry answers a SUCCESSFUL EMPTY DUPLICATE; nothing durable, no round, no recovery possible |
| (c) `base-guard-first` | the pre-#358 recovery order — the base-head fence before any own-effect classification (the classifier answers `not_dispatched` at the base, `foreign` on ANY moved head) | the MX-2d recovery fails: the round stales on its OWN commit, the child parks `blocked(review_round_foreign_head)` before any re-dispatch |
| (d) `any-terminal-occupancy` | the pre-#360 predicate — any terminal pipeline in the listing releases the slot | the MX-2e sequence oversubscribes: the historical terminal frees the slot with the current job running, a second lease is granted at capacity one |

### Mutation evidence (MX, the proof protocol)

Each arm's defect observable was produced by the seeded child and
quoted; the paired baseline assertions fail under the same patch:

- **(a)** (the finite-correction baseline under the worker child's
  `--mutation budget-before-child`): the baseline's round-admission
  assertion failed with `AssertionError: []` (`assert len(rounds) == 1`
  — no round, no child run but the parent's; the child-budget and
  child-run assertions fail identically). Green without the patch.
- **(b)** (the correction ingress baseline under the gateway child's
  `--mutation pre-commit-cache` + `--db-failures 1`): the baseline's
  ingest assertion failed with `500` (`assert first.status_code ==
  202`), and the in-window retry answered
  `{'status': 'accepted', 'deduplicated': True}` with NOTHING durable
  behind it — the baseline's durable-command/round/dispatch assertions
  all fail. Green without the patch.
- **(c)** (the MX-2d recovery baseline under the reconciler child's
  `--mutation base-guard-first`): the baseline's no-stale assertion
  failed with `AssertionError: stale`
  (`assert rounds[0].status not in ("stale", "ended")`; the child parks
  `blocked` under `review_round_foreign_head`). Green without the patch.
- **(d)** (the MX-2e mixed-history baseline under the in-process
  reverted `RunService._branch_search_occupancy`): the baseline's
  verdict assertion failed with
  `assert verdict.name == "RUNNING" -> TERMINAL`, and the drain pass
  released `1` lease with the CURRENT attempt's job still running —
  the held/refused-second-lease assertions fail identically. Green
  without the patch.

### Gate plumbing

The three MX classes are REQUIRED traces in `scripts/pg_gate.py`
(`R41-07 (#362) MX-1/MX-2/MX-3`, class-prefix patterns on the
production-entry profile), and `tests/test_pg_gate.py` pins that the
LIVE collection carries the full 24-cell cross — a dropped parametrize
entry refuses the gate, never quietly shrinks the matrix. The MX traces
write `forge.trace-record/1` records beside the MG set when
`FORGE_TRACE_RECORD_DIR` is exported.
