# Two-writer durable provider publication and recovery (2026-09-24)

R36-18 / issue #277: the two-writer slice of R32-22 run through DURABLE
storage and a provider whose duplicate behavior is REALISTIC. The
previous qualification
(`docs/evaluation/2026-09-23-two-writer/README.md`) proved the models
against a scripted remote whose `commit` was idempotent per
`(branch, marker)` and an in-memory saga store. That established the
vocabulary, not native exactly-once publication — a provider that
deduplicates a marker in a fake proves nothing about a provider that
does not. This run closes that gap.

- Module: `src/forge/adaptive/saga_durable.py`
- Pins: `tests/test_saga_durable.py` (35 tests; the real-PostgreSQL
  class skips without `FORGE_PG_TEST_URL`) and
  `tests/production_entry/test_two_writer_durable.py` (21 tests; the
  matrices are PostgreSQL-gated, one dual-backend trace runs everywhere)

## What is new, and what is deliberately not

Nothing coordination-shaped was reimplemented. The run drives the
SHIPPED `SagaCoordinator` (publication ladder: prepare → fence-check →
commit-intent → provider commit → verify → record), the SHIPPED
`WorkPackageCoordinator` (durable child-start intents, proven phase
advances, `PhaseAdvanceRefused`), and the SHIPPED
`start_child_run_row` dispatch (children are real `FlowRun` rows with
deterministic, replay-adoptive ids). The new code is exactly the three
pieces the issue names:

1. **`PostgresSagaStore`** — the `SagaStore` seam made durable. The
   saga's complete persisted state (per-repo step/compensation records
   plus the step journal) is a versioned JSON document
   (`forge.saga.durable/1`) on the parent run's `FlowRun.evidence`
   column — the same pattern the durable `WorkPackageCoordinator`
   established — and every observability transition lands one
   `Outbox` row in the SAME transaction as the state it announces.
   **No new table, no migration**: `publication_intents` has a closed
   status vocabulary (`requested…unknown/failed`) with no honest slot
   for `preparing`/`parked_human`, and forcing one would be dishonest;
   the JSON column on the existing row is the established shape, and
   the store works identically on PostgreSQL (asyncpg) and aiosqlite.
2. **`NativeShapedRemote`** — a GitLab/GitHub-shaped remote as a strict
   in-process object. Commits APPEND on the current head with NO
   marker dedup (a repeated identical commit creates a SECOND commit —
   pinned by test); the marker is a commit-message trailer, which is
   how forge's real writer correlates (`(forge-saga:<id>:<digest>)`,
   the same shape as the live writer's `(forge-op:<key>)`). The refs
   surface carries expected-head preconditions: a moved head under the
   publication's CAS pin is a 422-shaped refusal. Merge requests are
   idempotent ONLY by the provider-native key `(repository, source
   branch)`. Protected branches refuse direct commits. Every effect is
   journaled with its native identity (commit sha + parent + author,
   MR iid + url), and there is no merge/force-push/delete anywhere the
   publication protocol can reach — the only human merge is a world
   knob tests fire, never a publication call.
3. **`DurablePublicationEntry`** — the composition the customer entry
   would be: start the package, drive each writer's publication
   through the real coordinator over the durable store and the native
   remote, record child outcomes FROM THE PERSISTED SAGA ONLY, advance
   phases.

## The child gate lives at the storage boundary

The R36-18 acceptance says the consumer cannot start while the
producer outcome is missing, failed, or bound to a different tested
world. In this composition the gate is stronger than a dispatch
check: the store itself refuses (`PhaseAdmissionRefused`, pre-commit)
any save that would move a repository whose phase has not been
admitted — an unadmitted writer cannot even persist its write-ahead
intent. Admission derives from the durable workpackage record (writers
of phases ≤ current), and outcomes are recorded only from the
persisted saga: `ready_for_review`/`human_merged` prove success,
`failed` records failure, and `outcome_unknown`/`parked_human` record
NOTHING (an unproven surface and an owed human decision are not
outcomes — invocation completion is never consulted). Pinned both
ways: a silent producer leaves `PhaseAdvanceRefused(awaiting=("producer",))`
with zero consumer launches and zero consumer effects; a producer
whose only "proof" is an outcome from a foreign tested world is
`rejected`; a failed producer blocks with `failed=("producer",)` while
the partial-publication report names both the refusal and the
never-started writer.

## The crash matrix (real PostgreSQL, real coordinator)

Death is injected at the store's COMMIT BOUNDARY — an observer fires
after every durable save (`kill_at_boundary`) — so the pre-crash
process is the real `SagaCoordinator` interrupted between its own
saves, never a mirrored walker. Two boundary mappings are the real
coordinator's own batching, stated rather than approximated:
fence-check and commit-intent share ONE save (the intent
write-ahead), and `prepare` is the pass's first save (the begun
intents `begin_saga` journals).

Every cell of the matrix (every saga step × both writers, 12 cells
over real PostgreSQL, plus the representative sqlite cells in the unit
file) restarts from a genuinely fresh engine over the same rows and
converges to `complete` with:

- exactly ONE distinct commit carrying the saga marker per writer and
  exactly one created merge request (duplicate-effect counting over
  the remote's journal, not status strings);
- `commit_calls == 1` per writer — recovery's probe-first discipline
  against a remote that would happily create duplicates is what
  prevents the second effect;
- prefix-preserved branch histories and an empty
  `destructive_operations()` — no rollback, no rewrite;
- no duplicate model work: across the dead process and the recovering
  one, each child launched exactly once, and the outbox trail shows
  the consumer's child intent AFTER the producer's recorded outcome;
- an inert third pass: no remote effects, no child launches, no
  outbox rows, unchanged steps digest.

## Lost responses: adoption by evidence, unknown stays unknown

A lost commit response (the effect lands, the answer dies) books
`outcome_unknown` — honestly, with the `saga.unknown_effects` outbox
event. Recovery reconciles through NATIVE correlation only: it lists
the branch's commits and adopts when the marker-carrying commit is
ESTABLISHED (message carries the marker; parent is the expected
base). Pinned: adoption never re-calls the provider
(`commit_calls == 1`), and the `recovery.native_adoption` event names
the correlation. When the surface cannot prove anything (provider
unavailable during reconciliation), the effect STAYS
`outcome_unknown` across two recovery passes — fail closed, no blind
retry, the consumer still gated — and resolves by adoption once the
provider heals, still without a duplicate.

## Human pivots and policy outcomes

A human MERGE is observed, never performed (`observe_merge` → the
shipped `observe_human_merge`, the only route to `human_merged`); the
merged publication stands. The post-pivot matrix (human merged the
producer's PR, then edited the consumer's branch while the coordinator
was dead, deaths across the consumer's ladder, real PostgreSQL):

- deaths inside the open effect window (`fence_check`/`commit_intent`/
  `provider_commit`) → the consumer PARKS: `parked_human` with the
  conflict note, the `human_edit.conflicts` outbox event, the
  partial-publication report listing the merged producer as standing
  and the parked consumer as outstanding, and the human's commit
  prefix-preserved in the branch — never force-overwritten;
- deaths on already-verified rungs (`verify`/`record`) → the
  publication completes FORWARD (the effect was verified before the
  edit; the review carries it);
- a head that moves BEFORE the second publication starts is caught by
  the publication's CAS pin: a 422-shaped provider refusal books
  `failed` with the expected/live heads in the note — an explicit,
  reported policy outcome (retryable by a new attempt; the producer's
  PR stands; the human edit is preserved);
- provider unavailability after the first PR fails CLOSED (intent
  persisted, nothing created blind, consumer effects zero) and
  succeeds late once the surface heals.

## Read-only dependencies

Structural plus behavioral: the saga plans only the two writer
repositories, so no publication call can even name `repo-pinned` or
`repo-neighbor` (asserted over the remote's journal — zero effects,
zero calls), and the entry's credential staging (one writer credential
per writer repository, read credentials elsewhere) is verified clean
by `verify_credential_scope`.

## Observability

`workpackage.partial_publication`, `saga.unknown_effects`,
`recovery.native_adoption`, `human_edit.conflicts` — each an `Outbox`
row committed in the same transaction as the durable state it
announces, with each outstanding effect identified (repository, status,
note, review URL). The `PartialPublication` view over the persisted
saga answers "what stands, what is owed" at any moment.

## What remains for a live provider run

The remote is provider-SHAPED, not a live provider: the next step past
this qualification is pointing the same entry at the real
GitLab/GitHub adapters (the files API / refs CAS / MR-by-source-branch
semantics `NativeShapedRemote` models), where correlation is the
provider's commit listing over real shas. Out of scope here, as the
issue states: distributed provider transactions, automatic merge,
destructive compensation.
