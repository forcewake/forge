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

R38-13 / issue #314 (same day) qualified the same surface WITHOUT
overstating what each provider guarantees: payload preconditions
recorded and rechecked, the same-file read/apply window exercised live,
a single-writer policy where no native atomicity exists, content-level
adoption verification, and no blind redispatch of a lost mutation. See
"R38-13: the qualification" below.

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
oid — and, on GitLab since R38-13, the payload file's CONTENT at the
marker commit must equal the intended payload (a matching message alone
proves nothing). The marker is a commit-message trailer (the same shape
forge's real writer uses); NO provider dedupes on it and the adapters
never pretend otherwise.

## R38-13: the qualification (#314)

The #295 record was honest about two limits: GitLab's client-side head
check is not an atomic branch-wide CAS, and preserving a human COMMIT
in history is not preserving its CONTENT after a later full-file
replacement. R38-13 closes the content half and states the rest:

1. **Payload preconditions, recorded and rechecked.**
   `GitLabNativeEffects.commit` records, beside the pinned expected
   head, the PAYLOAD BASE (the content digest of the payload blob at
   preflight; `""` when the read proved absence) and the
   AFFECTED-FILE VERSIONS (path → digest). The read/apply window is
   rechecked against them TWICE:
   - BEFORE the apply: any drift shape (changed content, a file created
     underneath a proved absence, a file deleted underneath an update)
     refuses with the TYPED `ContentConflictError` while the concurrent
     content is still the branch head — nothing is overwritten;
   - AFTER the apply: the landed commit's parent is compared with the
     preflight head; on a mismatch (a concurrent commit landed inside
     the POST) the payload file is read back AT THE PARENT revision —
     still the preflighted content means the concurrent change did not
     touch our file (it proceeds, preserved underneath); drifted means
     the full-file-replacement hazard, surfaced as the same typed
     conflict on the landed effect (journaled `content_conflict`,
     booked `failed` with the parent sha where the concurrent content
     is recoverable) — never a silent overwrite, never claimed
     published.
2. **Single-writer exclusivity.** Where the provider offers no atomic
   branch-wide precondition, the effect surface enforces ONE in-flight
   writer per branch: a second writer entering the
   preflight→apply→recheck window is refused with the typed
   `WriterExclusivityError`, never interleaved.
3. **Content-level adoption.** A commit counts as carrying the saga's
   marker only when the payload file's CONTENT at that commit is the
   intended payload — a forged commit with the right message and
   wrong/absent content is not adopted (the repo parks for a human).
4. **No blind redispatch.** A mutation whose response was lost stays
   unresolved (in-process on the adapter, cross-process through the
   durable `outcome_unknown` booking): a negative probe does not prove
   a delayed apply absent, so the same mutation is never re-sent —
   resolution is a content-verified probe (adopt) or an explicit new
   attempt (a new marker).
5. **The typed conflicts are tracked.** `saga.content_conflict` and
   `saga.writer_exclusivity` outbox rows accompany the bookings, and
   the entry durably records each repository's preconditions under the
   parent run's `publication_preconditions` evidence key (beside the
   expected head) for audit and recovery.

### The CAS/exclusivity matrix (the profile statement)

Which provider has NATIVE atomicity and which relies on the stricter
writer policy — stated exactly (`PROVIDER_ATOMICITY` in
`saga_native.py` renders the same matrix):

| Guarantee | GitLab | GitHub |
|---|---|---|
| Branch-wide CAS on commit | NONE (client-side head check only) | NATIVE: `createCommitOnBranch` `expectedHeadOid` → `STALE_DATA` |
| Same-file read/apply window | client-side preconditions + pre/post-apply recheck — typed content conflict, never a silent overwrite | closed by the server CAS (the commit is refused outright) |
| Concurrent writers | SINGLE-WRITER POLICY on the effect surface (one in-flight writer per branch; the second parks typed) | the native CAS (the exclusivity policy is not the guarantee of record) |
| Adoption verification | BLOB-LEVEL: the payload content at the adopted commit must equal the intended payload | marker + parent over the listed history — the GitHub client exposes no repository blob read, so no content check is claimed |
| Payload preconditions journaled | payload base + affected-file versions at preflight | the INTENDED payload digest beside the CAS token (the transport offers no blob read to verify against after the fact) |

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
9 passed in 34.17s        # R37-14 (#295), the original record
13 passed in 57.86s       # R38-13 (#314) re-run: the #295 matrix + the four
                          # qualification arms, same adapters, fresh
                          # disposable projects
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

R38-13 arms (the #314 qualification, same live matrix):

| Arm | Outcome (all CONTENT-verified by blob reads at the revision, never history scans alone) | Native effect counts (live) |
|---|---|---|
| SAME-FILE read/apply window (a REAL human commit to `forge/publication.json` injected between the publication's preflight read and the server apply) | the pre-apply content recheck refused with the TYPED `ContentConflictError` BEFORE any effect; the human's content still readable at the branch HEAD (blob read at the head commit); the saga's marker never in the real history; no review opened; `saga.content_conflict` outboxed; the consumer stayed gated at `preparing` | producer 0 publication commits (only the injected human commit), 0 MRs; consumer untouched |
| UNRELATED-file window (same window, `HUMAN_WINDOW_EDIT.md`) | the publication PROCEEDED on top of the human commit (the landed commit's parent IS the human sha); the unrelated content AND the intended payload both read back blob-level at the head; the preconditions durably recorded beside the PREFLIGHT expected head (`payload_base: ""` — proved absent at preflight on the fresh branch) | producer exactly 1 commit + 1 MR; consumer 1 commit + 1 MR |
| DELAYED APPLY (the commit response dies, then the first probe is injected negative) | the same mutation was NOT blindly redispatched (durable `outcome_unknown`, `saga.unknown_effects`; exactly ONE marker commit in the real history — the apply was real); the recovering process then ADOPTED by content-verified correlation (`recovery.native_adoption`, `adopted=True`) and completed | producer `commit_calls: 1` across BOTH processes; exactly 1 commit + 1 MR |
| ACCESS REVOKED DURING RECOVERY (the consumer's reads ride a REAL project access token, `read_api` only, REVOKED while the coordinator is dead) | every consumer read failed CLOSED with that credential's own 401 (`ProviderUnavailableError`); the consumer stayed `intent_recorded` (uncertain, not "failed"); the producer's reviewable effect stands as the honest partial; NO fallback to the wider admin credential the process also holds — structurally impossible (one client per repository, no fallback path exists) and behaviorally proven (the scoped read still 401s with the admin token available) | consumer 0 commits, 0 MRs, no journal effects; producer 1 commit + 1 MR standing |

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
2. **the race this cannot close** — R38-13's qualification: a push
   landing between that read and the server's apply ends up UNDERNEATH
   our commit. The window is still not closed (no client-side dance can
   close it), but it is now DETECTED rather than merely narrowed: the
   payload preconditions recorded at preflight are rechecked BEFORE the
   apply (a drifted payload file refuses typed while the concurrent
   content is still the head) and AFTER it (the payload is read back AT
   THE PARENT revision of the landed commit — a drift there is the
   full-file-replacement hazard and surfaces as the typed
   `ContentConflictError` on the landed effect, journaled conflicted,
   never claimed clean). History preservation alone is no longer the
   claim; content is.

The payload action (create vs update) is decided by an AUTHORITATIVE
`read_blob` (R14): a failed read never forges "file does not exist".
`CommitOutcomeUnknown` (the client's never-retried timeout) maps to the
saga's `TimeoutError` → `outcome_unknown` → adopt-or-park by
CORRELATION — and since R38-13 the correlation is content-verified and
the same mutation is never blindly redispatched while its outcome is
unproven (a negative probe does not prove a delayed apply absent).

## How to re-run

```bash
# disposable Postgres (the pe_db fixture resets the database's public
# schema per test — never point FORGE_PG_TEST_URL at a live database)
podman exec forge-postgres psql -U forge -d postgres -c \
  'CREATE DATABASE forge_tw3_test'
set -a; source .env; set +a
export FORGE_PG_TEST_URL="postgresql+asyncpg://forge:forge@127.0.0.1:5433/forge_tw3_test"
export FORGE_GITLAB_LIVE_URL="$GITLAB_URL" FORGE_GITLAB_LIVE_TOKEN="$GITLAB_TOKEN"
uv run pytest tests/production_entry/test_two_writer_native.py -q
podman exec forge-postgres psql -U forge -d postgres -c \
  'DROP DATABASE forge_tw3_test WITH (FORCE)'
```

Without either env gate the file skips visibly (13 skips); the unit
suite (`tests/test_saga_native.py`, now including the R38-13
precondition / exclusivity / content-adoption / no-redispatch pins)
runs env-clean everywhere.
