# ADR-0028: Live-leg-check invariants — DB-arbitrated Draft-MR uniqueness, identity-first AzDO dispatch, authored-marker self-trigger guard

Status: accepted (2026-09-20)
Context: the live leg-check campaign (GitLab CE, GitHub and Azure DevOps
driven end-to-end against real instances on 2026-09-20) surfaced three
failure classes that the contract suites could not see, because each depends
on either concurrent actors or provider identity — exactly the gap the
[ADR-0027](0027-lifecycle-consolidation.md) ladder predicts ("the next
feature fix changes three places again"). They are recorded here as
decisions because each one fixes a *guarantee*, not a bug site:

## Decision

1. **ONE `create_merge_request` per run+branch is a DB invariant, not an
   application habit.** The failure-injection suite (S2b/S4 windows, real
   Postgres) caught a check-then-create race: a lease-expiry re-drive and
   the publication-intent scanner both decided "no Draft MR exists yet"
   before either created — two provider calls and two audit rows for one
   intent. Journal checks and provider searches narrow the window but
   cannot close it for two actors. The arbiter moved into the database
   (the same ON CONFLICT arbiter pattern the A10 budget fix uses):
   `uq_create_mr_per_branch` (migration 018) makes one
   `create_merge_request` row per `(flow_run_id, correlation_id=branch)` a
   DB invariant; the leg INSERTs with `ON CONFLICT DO NOTHING`, then
   `SELECT … FOR UPDATE` serializes the loser behind the winner's create,
   and the loser **adopts** — journal check first, provider search second
   (a crashed attempt can die between the provider call and the journal).
   Legacy duplicate rows are collapsed to the lowest id: they were
   artifacts of this defect, not distinct external writes.

2. **The AzDO dispatch journal is intent-first down to the identity.**
   `_advance_harness` journaled the execution handle only on success, so a
   Runs-API start failure left the run parked with NO project/repo identity
   — the revival scanner then skipped it, recorded the attempt `succeeded`,
   and stranded the run mid-transition (unreachable by `/retry` or a fresh
   `/implement`). The dispatch identity (project/repo/branch/attempt base,
   handle at `run_id=0`) is journaled BEFORE the HTTP call — the same
   intent-before-effect ordering [ADR-0005] mandates for every other
   external write — and a revival that still finds no identity re-parks the
   run `blocked` and records the attempt `failed`, keeping `/retry`
   reachable.

3. **Self-trigger protection is content-marker-based where provider
   identity cannot disambiguate.** On single-PAT AzDO deployments forge
   posts under the OPERATOR's identity: an author-based bot-loop guard
   either misses forge's own notes (a rejection note suggesting
   "/implement" self-triggered a run — issue #67's AzDO twin) or, set to
   the operator, drops the operator's REAL commands. Every forge-authored
   comment therefore carries a hidden `<!-- forge:authored -->` marker
   (one choke point: `_post_journaled_comment`), and the gateway skips
   marker-bearing comments BEFORE the author check. The author check stays
   for dedicated-bot deployments; rejection notes no longer contain literal
   slash-commands (defense in depth, #67 item 3). The GitHub leg's guard
   compares both `FORGE_GITHUB_BOT_LOGIN` and `FORGE_BOT_USERNAME` (53249b2).

4. **Provider read contracts are verified against the live API, not the
   docs** — the AzDO builds list silently ignores `repositoryId` without
   `repositoryType=TfsGit` and then demands a GUID (not a name), and the
   GitHub runs list drops the undocumented `head_branch=` param name. Both
   fixed client-side with the live-probed param shapes pinned by tests
   (06b5554, b36d31f, e7b308a); `contract_tested` levels in the release
   manifest reflect them.

## Consequences

- Every external write on the publication path now has a DB-level
  "exactly once" story: the commit (CAS + intent), the MR (unique index +
  row lock), the note (journaled single-action recovery). Adding a provider
  means plugging a transport into the same arbiters, not re-deriving them.
- `FORGE_AZDO_BOT_NAME` semantics change: it names forge's identity only
  when a dedicated bot exists; shared-PAT deployments leave it non-matching
  and rely on the marker (documented in
  [azure-setup](../getting-started/azure-devops.md)).
- The FI suite runs against a disposable real Postgres locally
  (`FORGE_PG_TEST_URL`) — the race was invisible to the SQLite-parallel
  unit suite and to a pure-format CI commit, which is why the CI integration
  job flaked before the local harness caught it.

Evidence: 25 consecutive green FI-suite runs after the fix (was ~1-in-2);
commits e7b308a, 927021b, 22e855a, 82901e9, 5b2add4, 69e9799, 0acda79,
2cbdf4e, 06b5554, b36d31f, d4c0ad1; issue #67.

## Addendum (2026-09-21, review e53ffd2 — v0.13.0)

The third review's findings land on the same invariant families; recorded
here so the next reader finds ONE decision record:

5. **Verification deadlines are per-candidate epochs** (B01/B07): persisted
   `{candidate_sha, started_at}`; observations never move them; a frozen
   non-empty required list makes absent checks a missing mandatory gate.
6. **Authoritative evidence is the newest native occurrence** (B02): run
   identity orders runs, attempt identity orders attempts within a run —
   never the reverse; workflow identity (not display name) merges checks.
7. **ReserveEffect before effect** (B03): `mr_reservations` (migration 019)
   is the general shape — a committed, row-locked logical intent with an
   immutable attempt journal beside it; the reviewer's LoadApprovedInput /
   ReserveEffect / ReconcileEffect vocabulary names the same boundaries.
8. **Bound adapters touch only their subject** (B08): every recovery scan is
   repo/project-scoped; the dispatcher enumerations are the only
   provider-wide reads (pinned by the B15 boundary test).
