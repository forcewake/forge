# Two-writer coordinated change — qualification (2026-09-23)

R32-22's first two-writer slice: ONE WorkPackage whose producer and
consumer lanes both write, qualified against a COMPLETE frozen
CandidateSet. The harness is
`src/forge/adaptive/two_writer_qualification.py`; the pins are
`tests/test_adaptive_two_writer.py` (45 tests). It is a PURE/DB-level
qualification: the durable coordination legs run on aiosqlite, the
publication legs run against a deterministic scripted remote and the
in-memory saga store, and the whole thing DRIVES the existing models —
`WorkPackage`/`WorkPackageCoordinator`, `PublicationSaga`/
`SagaCoordinator`, `freeze_candidate_set`/`freeze_verified_world`,
`EvidenceLedger` — rather than reimplementing any of them.

## The scenario

One concrete producer/consumer change across four repositories:

| Repo | Role | In the package | In the frozen set |
|---|---|---|---|
| `repo-producer` (A) | writer | `producer` item, phase 1 | `changed` (new candidate + image) |
| `repo-consumer` (B) | writer, depends on A | `consumer` item, phase 2 | `changed` (new candidate + image) |
| `repo-pinned` (C) | pinned baseline | read-only REFERENCE item (a reference snapshot, no lane) | `baseline` at the PINNED digest — the scenario's branch head has moved PAST the pin, and the freeze must ride the pin, never the head |
| `repo-neighbor` (D) | read-only neighbor | context (`read_only_repositories`, no item at all) | `baseline` (observed identity) |

`freeze()` snapshots the COMPLETE tested world through the existing
entries: changed identities, baseline identities (C at its pin), the
contract bundle (producer's changed contract + consumer's expectation),
the per-repo test bundle, the environment profile, the external pins
and the policy refs — with the tested-world and applicability digests
PERSISTED on the set at freeze time (NXT-22), so every later binding
reads the recorded digest, never a recomputation.

## Dependency-gated child start

The coordinator is driven through both phases with the frozen world as
the package's ACTIVE world, and the gate is pinned in both directions:

- the consumer lane is NEVER dispatched while the producer is silent
  (`PhaseAdvanceRefused` with `awaiting=("producer",)` —
  launched-but-silent is not proof) or failed (`failed=("producer",)`,
  package state `failed`, zero consumer launches);
- only a PROVEN outcome — recorded against the package's active
  tested-world digest — admits phase 2; an outcome recorded from a
  DIFFERENT world is `rejected` and unblocks nothing;
- phase 2 then launches exactly once, and the package completes on the
  consumer's own proven outcome.

## The kill-at-step-k matrix

The saga-testing discipline from the research (topic 6 §3), applied to
forge's publication ladder `prepare → fence-check → commit-intent →
provider commit → verify → record`:

- **12 crash cells** — death after EVERY step of EACH writer's
  publication (producer deaths land between the two publications;
  consumer deaths land mid-second-publication).
- **6 pivot cells** — the producer's PR is observed human-MERGED (the
  pivot; the harness records the observation, the bot never merges),
  then the coordinator dies during the consumer's publication and a
  HUMAN EDIT lands on the consumer's branch while it is dead.
- **1 lost-response cell** — the provider commit lands but the response
  dies with the process.

Death is injected by a walker that replays the coordinator's own
write-ahead ordering step by step (journal the fence, persist the
intent BEFORE the provider call, book the effect, verify, record) and
abandons the process at the chosen boundary; the RESTART is the real
`SagaCoordinator.recover` over the persisted saga — recovery is never
faked. One caveat is honest in both directions: the coordinator batches
fence-check and commit-intent into one save on its own, so the matrix's
`fence_check` cell persists a half-laddered journal state the normal
run never produces — recovering from it is precisely the defensive
coverage the matrix exists to give.

Every cell must satisfy the whole invariant set, computed (never
assumed) per cell:

1. **No duplicate publication** — exactly one DISTINCT marker-keyed
   commit and one review per repo (a re-issued intent that re-calls the
   provider but lands the same idempotent commit is still one
   publication).
2. **No destructive rollback** — no force-push, no branch delete (the
   provider protocol cannot even express them, and the scripted remote
   counts direct attempts), and every pre-restart branch history
   survives as a PREFIX of the post-restart history.
3. **Idempotent re-execution** — a THIRD process over the reconciled
   state spends nothing and changes nothing.
4. **Convergence** — the saga ends `complete` or `parked` with a
   recorded reason; nothing else.

Results: all 12 crash cells reconcile to `complete` (one publication
per repo). The pivot cells are FORWARD-ONLY in every case — the merged
producer stands as `human_merged`, never re-published, never rewritten
— and split honestly by where the death landed:

| Death step (consumer) | Outcome | Why |
|---|---|---|
| `commit_intent`, `provider_commit` | `parked_human` | the effect window is open and the human edit stands in it — reconcile/verify parks, never overwrites |
| `verify`, `record` | completes forward | the repo is already decided; the PR shows the human edit riding on top |
| `prepare`, `fence_check` | completes forward, stacked | FINDING: from `preparing` the current coordinator commits ON TOP of the moved head (the head comparison lives in `_reconcile`, not `_publish`); append-only and non-destructive, but a real adapter's push leg would hit the moved head — the live run must decide whether publish gets the same pre-check reconcile has |

The lost-response cell books the honest PARTIAL publication on the
first pass (`outcome_unknown` producer, consumer's PR standing and
reported) and closes the window by ADOPTING the marker-carrying remote
effect on restart — no second publication attempt.

## The readiness contract (can-i-deploy)

Promotion is a QUERY over the recorded `EvidenceLedger`, never a
re-run. Three edges, each its own verification record bound to the
frozen world by `record_evidence`:

- `contract:producer->consumer` — the consumer's expectation replayed
  against the producer's change (covers both writers);
- `baseline:consumer->pinned` — the consumer verified against C at the
  PINNED digest;
- `observation:neighbor` — the read-only neighbor observed at its
  frozen identity (covers D ALONE).

`readiness(set, ledger, edges)` is a pure lookup: every edge needs a
non-superseded record whose claimed applicability matches the set's
FROZEN digests. Blocked edges are named with a reason kind —
`missing`, `stale` (superseded by an invalidation event) or `invalid`
(recorded against different frozen identities) — and an unfrozen set
refuses the query outright. The per-edge precision is the point:

- mutate the consumer contract mid-package (new consumer candidate +
  changed bundle) → both identity digests flip → the contract and
  baseline edges block BY NAME, while the **neighbor observation edge
  stays ready** — a consumer-side change does not invalidate the one
  edge that never touched it;
- drift the pin (re-pinned C) → the baseline edge blocks.

Stale evidence is never silently reused: the old records stay
inspectable in the ledger, but neither their fingerprints nor the
event-based supersession path can put them back behind a promotion
without an explicit reactivation through the full applicability check.

## Credential scope rules

`verify_credential_scope(lane_assignment(package), staged_credentials)`
restates MRP-03 for staging: a WRITER credential may appear only under
its owning writer's ONE repository; read-only repositories (D included
— the read universe is derived from the lanes' `siblings_read`, so
context repos are in scope even without an item) and repositories
outside the package entirely may hold READ credentials only. Ownership
comes from an explicit dispatch table when given, else is derived from
the staging — and a name staged under two writer repos is a leak under
any ownership. Violations are typed records
(`CredentialScopeViolation`) naming the credential, the leaking
repository and the owner; the qualification report checks the clean
staging AND three adversarial probes (neighbor receives a writer
credential, writer credential crosses lanes, pinned baseline receives
a writer credential) — all caught.

## The report

`forge.two-writer.qualification/1` (`TwoWriterReport.to_document()`):
scenario digests, both phase-gating arms, all 12 + 6 + 1 matrix cells
with their invariant verdicts, the readiness queries (happy, stale,
empty), the credential verdicts, the partial-publication outcome, and
the HUMAN DECISION POINTS — the standing PRs (merge stays a human
approval; the bot never merges), the deploy promotion (gated on the
readiness query, a SEPARATE approval from the merges), and the parked
consumer branches from the pivot cells, each with the cross-repo
evidence bundle (world digest, evidence ids) attached. The report is
deterministic end to end — every id, digest and URL derives from the
scenario — so `report_digest` reproduces bit-for-bit across fresh runs.

## Honest limitations

- **This is not a provider-wired run.** The publication legs run
  against `ScriptedRemote` (a marker-keyed fake with append-only
  histories) and `InMemorySagaStore`; the durable legs run on
  aiosqlite, not Postgres. It qualifies the MODELS and their
  reconciliation contracts, not any real GitHub/GitLab surface.
- The matrix's pre-crash walk replays the coordinator's save ordering
  with the module's pure transitions; it is a faithful mirror, not the
  coordinator's own `_publish` code path (the RECOVERY side is the
  real coordinator throughout).
- The evidence records behind the happy-path readiness query are
  RECORDED by the harness against the frozen world; no verification
  lane executed anything. The gate mechanics (binding, invalidation,
  per-edge precision) are what is qualified here, not passed tests.
- The `prepare`/`fence_check` pivot finding above is recorded, not
  fixed — changing `_publish` to pre-check the head is a
  `publication_saga.py` decision outside this qualification's scope.

## What remains for a LIVE two-writer run

1. A provider-backed `PublicationProvider` (GitHub/GitLab adapters) and
   a durable `SagaStore` (the runs tables), then the same matrix
   re-run against them — the harness's cell driver takes any provider.
2. Child lanes dispatched through
   `WorkPackageService`/`run_service_starter` against real provider
   subjects, with terminal FlowRun rows feeding
   `advance_from_children`.
3. Real verification-lane execution producing the `EvidenceRecord`s —
   then `readiness` answers from executed evidence instead of
   harness-recorded evidence.
4. Mixed-version (old-writer × new-writer) compatibility cells against
   the topic-2 synthetic environments — the A0/A1 × D0/D1 states per-repo
   CI cannot see.
5. The reconciliation sweep (a `forge doctor` check scanning terminal
   packages for un-reconciled remote effects) as the fallback for
   missed events — the research's low-effort closer.
