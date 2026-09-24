# Two-writer durable saga over NATIVE provider effects (2026-09-24)

R37-14 / issue #295: the durable two-writer publication saga (#277,
R36-18) connected to ACTUAL native provider effects — the EXISTING
`GitLabClient`/`GitHubClient` injected behind the saga's effect
interface and exercised against the live lab GitLab CE with REAL
PostgreSQL durable state. The adapters are
`src/forge/adaptive/saga_native.py`; the pins are
`tests/test_saga_native.py` (unit, env-clean) and
`tests/production_entry/test_two_writer_native.py` (the live failpoint
matrix, PG + live gated).

## The effect interface

`SagaEffectSurface` (in `saga_native.py`) formalizes what
`DurablePublicationEntry` consumes. Each method maps to one
native-effect concept:

| Interface method | Native-effect concept | GitLab spelling | GitHub spelling |
|---|---|---|---|
| `pin_expected_head` + `commit` | create-commit under an expected-head precondition | CLIENT-SIDE check (GitLab's Commits API has no CAS) — see the race note | NATIVE: `createCommitOnBranch` `expectedHeadOid` → `STALE_DATA` |
| `open_review` | create-merge-request, idempotent by the native `(repository, source branch)` key | probe the opened MRs first (GitLab 400s a duplicate-source MR); create with a `Draft:` title | find-by-`(head, base)`, create as a draft PR |
| `remote_head` / `head_carries_marker` | read correlation | one `get_branch_head` GET + the LISTED commit history scanned for the marker | `git/refs/heads` + the branch-scoped commit listing |

`NativeShapedRemote` (the R36-18 reference) satisfies the same Protocol
— verified by the adapter-compat tests in both suites — so the durable
entry drives reference and native spellings through ONE seam. The
`expected_head` keyword on `NativeShapedRemote.create_commit` is the
one-shot spelling of its pin (additive; the native append itself stays
precondition-free, like the providers' own commits APIs).

Correlation is by NATIVE IDENTITY everywhere: recovery lists the
branch's commits and matches the marker IN THE MESSAGE plus the parent
oid. The marker is a commit-message trailer (the same shape forge's
real writer uses); NO provider dedupes on it and the adapters never
pretend otherwise.

## What ran LIVE (the point of this evaluation)

Provider: the lab **GitLab CE 19.3.2**
(`gitlab.forcewake.duckdns.org`), authenticated with the developer
`.env` token against **disposable projects only** — `forge-tw-<uuid>-producer`
and `forge-tw-<uuid>-consumer`, created under the token's own namespace
by the module fixture and DELETEd at teardown (GitLab accepted the
deletes; instances keep `deletion_scheduled` tombstones per their own
retention). Project 68 and every other existing lab project were never
touched. Durable state: real PostgreSQL in a disposable
`forge_tw2_test` database (created and dropped for the run).

`uv run pytest tests/production_entry/test_two_writer_native.py -q`
with `FORGE_PG_TEST_URL`, `FORGE_GITLAB_LIVE_URL` and
`FORGE_GITLAB_LIVE_TOKEN` exported:

```
9 passed in 34.17s
```

Arms and outcomes (the matrix's own assertions, all live):

| Arm | Outcome | Native effect counts (live) |
|---|---|---|
| positive trace | package `complete`, saga `complete`; consumer child launched only after the producer's persisted outcome (outbox ordering) | 1 commit + 1 draft MR per repository per arm; every MR `opened`, `merged_at` empty |
| kill matrix ×5 (`prepare`, `commit_intent`, `provider_commit`, `verify`, `record` on the producer; `fence_check` shares `commit_intent`'s save) | every cell restart-and-converged: exactly ONE marker commit in the ACTUAL listed history (parent == expected base), ONE MR per repository, prefix-preserved histories, one child launch per item across the dead + recovering processes, inert third pass | 1 commit + 1 MR per repository per cell; `destructive_operations()` empty everywhere |
| lost first-repo commit response | `saga.unknown_effects` then `recovery.native_adoption`; producer `ready_for_review` with `adopted=True` | `commit_calls: 1` per repository — no duplicate commit in the actual history (asserted by listing) |
| human edit between partial publication and recovery (death inside the consumer's effect window, then a REAL commit pushed to the publication branch via the API) | consumer `parked_human`, `human_edit.conflicts` surfaced, partial report standing=[producer] / outstanding=[consumer]; the human commit stayed the branch head; prefix intact | producer 1 commit + 1 MR; consumer 0 MRs (never got there); nothing forced |
| two recovering processes + definitive second-repo failure (the consumer's review targets its own source branch — GitLab's REAL 400 "You can't use same project/branch for source and target") | consumer `failed` with the provider's own refusal text in the note; producer's reviewable effect stands as ONE durable partial state; the second recovering process spent nothing (same steps digest, same partial, no new commits/MRs/children) | producer 1 commit + 1 MR; consumer 1 commit + 0 MRs |
| the bot never merges | asserted from the provider's own listing after every arm: every MR in the disposable projects `opened`, draft-shaped, `merged_at` empty | 0 merges across the whole run |

A second instrumented live pass (fresh disposable projects, same
adapters) reproduced the counts: both arms `complete`, lost-response
producer `adopted=true` with `commit_calls=1` per repository, 1 created
MR per repository per arm, `any_merged=false` on both projects.

## What stayed REFERENCE (and why that is honest)

- The R36-18 arms (`tests/test_two_writer_durable.py` and the
  `NativeShapedRemote` reference) remain the full 12-cell consumer-side
  kill matrix and the post-pivot (human-merge) matrix. The live matrix
  here covers the producer-side death steps, the lost response, the
  human edit and the two-recoverer/failure arms against the REAL
  transport; it does not re-run every reference cell over HTTP.
- `GitHubNativeEffects` is exercised over the existing in-memory
  `FakeGitHub` (parsed CAS semantics, `STALE_DATA` refusal, delayed
  apply) — no live GitHub repository exists in the lab, and claiming a
  live GitHub run would be dishonest. The native-CAS path (real
  `expectedHeadOid`) is what the fake mirrors.
- The consumer's "review" is a draft MR whose merge is a HUMAN decision
  (`observe_human_merge` is the only route to `human_merged`); no arm
  of this evaluation merged anything.

## The GitLab client-side-CAS race note

GitLab's Commits API (`POST /repository/commits`) has NO branch-wide
CAS and no idempotency — a repeated POST is a SECOND, distinct-SHA
commit (`CAS_PROTECTED_PROVIDERS` in `forge/durable/intents.py` says
the same). `GitLabNativeEffects.commit` therefore:

1. reads the branch head (`get_branch_head`) and REFUSES 422-shaped on
   a moved head vs the pin — the writer's own `BranchDriftError`
   discipline, client-side;
2. **the race this cannot close**: a push landing between that read and
   the server's apply ends up UNDERNEATH our commit. Nothing is
   overwritten and no human commit can be lost — the saga's verify step
   reads the listed history back, and a head the saga cannot account
   for parks the repository for a human decision. The window is
   NARROWED, not closed; that is stated rather than hidden.

The payload action (create vs update) is decided by an AUTHORITATIVE
`read_blob` (R14): a failed read never forges "file does not exist".
`CommitOutcomeUnknown` (the client's never-retried timeout) maps to the
saga's `TimeoutError` → `outcome_unknown` → adopt-or-park by
correlation, never a blind retry.

## How to re-run

```bash
# disposable Postgres (the pe_db fixture resets the database's public
# schema per test — never point FORGE_PG_TEST_URL at a live database)
podman exec forge-postgres psql -U forge -d postgres -c \
  'CREATE DATABASE forge_tw2_test'
set -a; source .env; set +a
export FORGE_PG_TEST_URL="postgresql+asyncpg://forge:forge@127.0.0.1:5433/forge_tw2_test"
export FORGE_GITLAB_LIVE_URL="$GITLAB_URL" FORGE_GITLAB_LIVE_TOKEN="$GITLAB_TOKEN"
uv run pytest tests/production_entry/test_two_writer_native.py -q
podman exec forge-postgres psql -U forge -d postgres -c \
  'DROP DATABASE forge_tw2_test WITH (FORCE)'
```

Without either env gate the file skips visibly (9 skips); the unit
suite (`tests/test_saga_native.py`) runs env-clean everywhere.
