# Remote effect reconciliation: provider idempotency facts and durable patterns

Research for review findings **R11** (publication intent persisted after the HTTP effect on some
paths; a crash between the remote commit and journal completion leaves an ambiguous outcome) and
**R17** (deadlines must be evaluated before provider I/O). Scope: forge's three providers
(GitHub REST/GraphQL, GitLab REST, Azure DevOps REST) for the effects we perform — branch create,
commit, PR create, comment/note, CI dispatch.

Key code references in this repo:

- `src/forge/repository/writer.py` — GitLab write path; already journals intent *before* dispatch
  (`record_action` at line ~154) and reconciles a lost create-commit response by
  `(forge-op:<key>)` marker + expected parent OID (`_resolve_unknown`).
- `src/forge/runs/github_service.py`, `src/forge/runs/azure_service.py` — `_post_journaled_note`
  and `_publish_changeset` paths (where the R11 gap lives).
- `src/forge/durable/models.py` — `ActionLog` (intent→outcome journal), `StepRun`
  (`deadline_at`, `lease_owner`, `lease_expires_at`, fence token), `Outbox`.

---

## Ground truths per provider (URLs)

### GitHub

**No idempotency-key mechanism on REST POSTs.** GitHub's REST best-practices page documents
conditional requests (ETag / `if-none-match`) as the only concurrency tooling, and states
explicitly: *"Conditional requests for unsafe methods, such as POST, PUT, PATCH, and DELETE are
not supported unless otherwise noted in the documentation for a specific endpoint."* No
`Idempotency-Key` header is documented anywhere in the REST reference.
<https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api>

**GraphQL `clientMutationId` is echo-only.** Every mutation input and payload documents it as
"A unique identifier for the client performing the mutation" — it is copied from input to output
so a client can correlate a response with a request. GitHub documents no server-side
deduplication on it (unlike Linear's clientMutationId, which does dedupe). Treat GitHub
mutations as **not** idempotent across retries.
<https://docs.github.com/en/graphql/reference/input-objects>

**`createCommitOnBranch` CAS semantics.** The mutation "creates a commit whose parent is the HEAD
of the provided branch and also updates that branch to point to the new commit. It can be thought
of as similar to `git commit`." `expectedHeadOid` is "The git commit oid expected at the head of
the branch prior to the commit" — an optimistic lock. On mismatch the API fails with
`Expected branch to point to "<oid>" but it did not. Pull and try again.`.
<https://docs.github.com/en/graphql/reference/commits> ·
<https://stackoverflow.com/questions/72836597/how-to-create-new-commit-with-the-github-graphql-api>

**What the failed CAS does NOT tell you:** it only says the branch tip moved away from
`expectedHeadOid` before your write applied. It says nothing about whether a *previous,
identical* attempt landed — the tip may have moved because your earlier request succeeded, because
a human pushed, or because another forge run committed. A CAS failure is therefore **never**
evidence of "nothing happened"; it forces a probe of the new tip. Real-world integrations (e.g.
`github/gh-aw` signed-commit push) confirm the operational shape: fetch the tip via
`git ls-remote`/API first, then replay commits one at a time with `expectedHeadOid`, and handle
the "branch does not exist yet" case separately (ls-remote returns empty for a new branch).
<https://github.com/github/gh-aw/pull/22008>

**`createRef` duplicate semantics: 422 "Reference already exists".** `POST /repos/{owner}/{repo}/git/refs`
on an existing ref fails with HTTP 422 `Reference already exists`
(<https://github.com/octokit/rest.js/issues/339> shows the exact response). This error is
*cheaply resolvable*: the ref now exists; a follow-up `GET /repos/{owner}/{repo}/git/ref/{ref}`
(<https://docs.github.com/en/rest/git/refs>) returns its OID. If the OID equals the SHA you
passed, your earlier attempt (or an identical one) landed — adopt it. If it differs, someone else
owns the ref. Caveat: GitHub's 422 on this endpoint is documented only generically ("Validation
failed, or the endpoint has been spammed"), and 422 is also used for unrelated ref-update
failures, so always distinguish by the `message` field.
<https://github.com/github/rest-api-description/issues/4887>

**Probe limitation: commit search indexes only the default branch.** `GET /search/commits`
supports `hash:`, `parent:`, `tree:`, and message matching, but "When you search for commits, only
the default branch of a repository is searched." Useless for per-run factory branches. The
reliable probe is `GET /repos/{owner}/{repo}/commits?sha=<branch>` (List commits, branch-scoped)
with client-side message filtering.
<https://docs.github.com/en/search-github/searching-on-github/searching-commits> ·
<https://docs.github.com/en/rest/commits/commits>

**PRs and comments:** no server-side dedup on creation. A PR find-by-identity probe exists as
`GET /repos/{owner}/{repo}/pulls?head=<owner>:<branch>&state=open`; issue comments must be found
by scanning `GET /repos/{owner}/{repo}/issues/{n}/comments` for a marker embedded in the body.

### GitLab

**Commits API has no CAS and no documented idempotency.** `POST /projects/:id/repository/commits`
takes `branch`, optional `start_branch`/`start_sha`, and a `force` flag ("When true overwrites the
target branch with a new commit based on the start_branch or start_sha" — a force-push footgun,
never use it for retry). Nothing in the reference describes deduplication or optimistic
concurrency; because GitLab builds the commit server-side with a fresh committer timestamp, a
blind retry produces a *second, distinct-SHA commit* with the same tree. The `last_commit_id`
field on actions is informational only ("Last known file commit ID. Only considered in update,
move, and delete actions") — no stale-value rejection is documented. The endpoint is additionally
rate-limited (>20 MB payloads: 3 requests / 30 s).
<https://docs.gitlab.com/api/commits>

**Branch creation duplicate semantics: 400 "already exists".** Creating an existing branch fails
with HTTP 400 and an "already exists" message; forge's `ChangesetWriter.ensure_branch` already
treats this as idempotent re-entry and verifies the branch with `get_branch`
(`src/forge/repository/writer.py:190-207`).

**MR uniqueness is provider-enforced.** "Each branch can be associated with only one open merge
request" — creating a second open MR for the same source/target fails with
`Validate branches Cannot Create: This merge request already exists`. This makes MR creation
*effectively idempotent*: on collision, find the existing MR via
`GET /projects/:id/merge_requests?source_branch=…&target_branch=…&state=opened`.
<https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/> ·
<https://gitlab.com/gitlab-org/gitlab/-/issues/22015>

**Probe endpoints are good.** `GET /projects/:id/repository/commits?ref_name=<branch>` returns
`id`, `message`, `parent_ids`, and can parse `trailers=true` server-side;
`GET /projects/:id/repository/commits/:sha/refs` ("Get references a commit is pushed to") answers
"which branches contain this commit".
<https://docs.gitlab.com/api/commits>

### Azure DevOps

**Pushes API is a true CAS.** `POST .../pushes` carries `refUpdates[].oldObjectId`; the docs for
the underlying refs update state: *"You must specify both the old and new commit to avoid race
conditions."* The per-ref `GitRefUpdateStatus` enum includes `staleOldObjectId`: *"the ref update
request could not be completed because the old object ID presented in the request was not the
object ID of the ref when the database attempted the update. The most likely scenario is that the
caller lost a race to update the ref."* Like GitHub's CAS failure, this proves only that the ref
moved — not who moved it, and not whether a previous attempt landed.
<https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pushes/create?view=azure-devops-rest-6.1> ·
<https://learn.microsoft.com/en-us/rest/api/azure/devops/git/refs/update-refs?view=azure-devops-server-rest-5.0>

**PR creation is provider-deduplicated.** Creating a PR whose active source/target pair already
exists fails with HTTP 409 / `TF401179: An active pull request for the source and target branch
already exists.` The generated OpenAPI spec for `pullRequests_create` documents exactly this 409.
Probe via `GET .../pullrequests?searchCriteria.sourceRefName=…&searchCriteria.targetRefName=…&searchCriteria.status=active`.
<https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/create?view=azure-devops-rest-7.2> ·
<https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/get-pull-requests?view=azure-devops-rest-7.1> ·
<https://apis.io/apis/microsoft-azure-repo/microsoft-azure-repo-pull-requests-api> ·
<https://stackoverflow.com/questions/56184170>

**Build queue has no dedup.** `POST .../build/builds` ("Queues a build") documents no
idempotency key, dedup window, or conflict status; every accepted POST queues a build. Dedup must
be client-side: list builds filtered by definition/branch/queue-time and match on a marker carried
in build parameters (what forge's Azure handle already does).
<https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/queue?view=azure-devops-server-rest-7.1>

### Summary matrix

| Effect | Provider-native dedup? | CAS? | On-duplicate signal | Safe blind retry? |
|---|---|---|---|---|
| GH `createCommitOnBranch` | No (`clientMutationId` echo-only) | Yes (`expectedHeadOid`) | CAS error; tip moved, provenance unknown | No |
| GH `createRef` | Yes (ref names unique) | n/a | 422 `Reference already exists` | Borderline — safe *only* with follow-up OID compare |
| GH PR / issue comment | No | No | — | No (duplicates) |
| GL `POST /repository/commits` | No | No | — | No (second commit, new SHA) |
| GL `createBranch` | Yes | n/a | 400 `already exists` | Yes (verify with get_branch) |
| GL MR create | Yes (1 open MR per src/tgt) | n/a | `already exists` validation error | Effectively yes (then find by identity) |
| ADO push | No | Yes (`oldObjectId` → `staleOldObjectId`) | `staleOldObjectId`; provenance unknown | No |
| ADO PR create | Yes (active src/tgt unique) | n/a | 409 `TF401179` | Effectively yes (then find by identity) |
| ADO queue build | No | No | — | No (second build) |

---

## Prior art patterns

**Temporal: at-least-once activities + caller-side idempotency.** Temporal's activity contract is
explicitly at-least-once: an activity whose result was not recorded (crash, timeout, lost
response) is re-executed even if its side effect already happened — "Activities won't record to
the Event History until they return or produce an error. If an Activity fails to report to the
server at all, it will be retried." The docs recommend designing activities to be idempotent, and
suggest deriving a *stable* idempotency key from Workflow Run ID + Activity ID so every retry of
the same logical step carries the same key while different logical steps differ. This is exactly
forge's window: the journal completion (`complete_action`) is the "record the result" step; a
crash before it means the effect's outcome must be *looked up*, not re-attempted.
<https://docs.temporal.io/activity-definition> ·
<https://temporal.io/blog/idempotency-and-durable-execution>

**AWS Step Functions: let the engine own completion, but the effect still needs identity.** The
three service-integration patterns (Request Response, Run a Job `.sync`, Wait for Callback
`.waitForTaskToken`) exist so the durable engine never marks a step done before the *job* is done
— `.sync` polls until the external job completes. They do not make the external effect idempotent;
AWS guidance elsewhere (EC2 `RunInstances` client tokens) shows provider-side dedup keys as the
complement. Lesson for forge: the remote commit must complete-and-verify (probe) before the
journal marks success, and when the provider offers no key (all three git providers, for commits),
identity lives in the effect itself — the commit message marker.
<https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html>

**Stripe idempotency keys: the gold standard forge cannot get from git providers.** Client sends
`Idempotency-Key` (≤255 chars); Stripe replays the first response for retries within 24 h, errors
on the same key with different parameters, and returns 409 for a concurrent in-flight duplicate.
Keys expire after 24 h, so post-expiry retries need application-level safeguards. This is the
shape the IETF `Idempotency-Key` header draft standardized. None of GitHub/GitLab/ADO offer this
for commit creation (GitHub explicitly: conditional requests unsupported on unsafe methods), so
forge must emulate the pattern locally: persist the key *before* the request, and reconcile by
key *after* an ambiguous outcome.
<https://stripe.com/docs/idempotency> ·
<https://ietf-wg-httpapi.github.io/idempotency/draft-ietf-httpapi-idempotency-key-header.html>

**Transactional outbox + polling relay = "effectively once".** Write the intent in the same local
transaction as the business state, then a relay performs the external write and marks completion.
Delivery is at-least-once (relay can crash after publishing, before marking), and exactly-once
semantics are recovered by dedup on a stable event id. Forge already has the pieces: the `Outbox`
table, `ActionLog` intent rows, and the GitLab writer's intent-first ordering. What is missing is
the **recovery leg**: a scanner that claims *pending* intents after a crash and probes instead of
re-posting. The analogous consumer-side dedup here is the provider-visible marker.
<https://microservices.io/patterns/data/transactional-outbox.html> ·
<https://codelit.io/blog/outbox-pattern-reliable-messaging>

**Marker-in-effect dedup when the provider has no keys.** The Bernstein tracker contract mandates:
"Where the tracker does not [support idempotency], the adapter must dedupe locally via a comment
marker (the agent posts a `<!-- bernstein-key: ... -->` marker) or a ledger keyed on
(ticket_id, op, key)." Forge's `(forge-op:<key>)` commit-message suffix is the same pattern for
commits; it should extend to comments/PR bodies.
<https://bernstein.readthedocs.io/en/latest/trackers/contract>

**CI dispatch dedup: identity by content, not by request.** GitHub Actions does not dedupe
`workflow_dispatch` calls; the ecosystem dedupes by *content identity*: `concurrency` groups with
`cancel-in-progress` collapse concurrent runs for the same logical unit, and tree-hash comparison
actions skip a run when a recent run of the same workflow already processed the identical git tree
("Only considers runs with a smaller run ID ... to avoid two runs skipping each other"). Applied
to forge: dispatch identity = (branch, content digest, operation key) embedded in dispatch
parameters, then find-before-create on recovery.
<https://github.com/leavesster/duplicate-run-action> ·
<https://github.com/step-security/skip-duplicate-actions>

**Retry budgets with jitter (feeds R17/R11 retry design).** AWS Builders' Library: always set
timeouts sized at a conservative percentile; back off exponentially with a cap; use *full jitter*
(`random(0, min(cap, base·2^attempt))`) to desynchronize retry waves; retry only when the
dependency looks healthy and budget retries client-side (token bucket); side-effecting calls are
unsafe to retry unless idempotent. GitHub's own guidance adds: honor `retry-after`, stop at
`x-ratelimit-remaining: 0`, back off exponentially and throw after a bounded number of retries.
<https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/> ·
<https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/> ·
<https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api>

---

## Reconciliation probe design

### The identity tuple

Every publication intent must carry a provider-independent identity so a probe can answer "did
**this** attempt land?":

```
identity = (provider, repo, target_ref, expected_parent_oid, operation_key, content_digest)
```

- `operation_key` — client-minted, **stable across retries of the same intent** (minted when the
  intent row is created, never per attempt; see schema section). Appears in the effect itself:
  commit message suffix/trailer, PR body, comment body, dispatch parameter.
- `expected_parent_oid` — branch head captured at intent time; a probe match requires the found
  commit's parent list to equal it (root commit ⇒ empty parent list). This prevents attributing a
  later repair commit to an older intent, and detects the case where someone else already moved
  the branch.
- `content_digest` — tree/digest of the ChangeSet, for belt-and-braces verification that the
  adopted commit has the intended content.

### Decision table: what each outcome proves

| Observation | Proof | Action |
|---|---|---|
| Commit found with exact `operation_key` AND parents == expected | This attempt landed | Adopt: journal `succeeded`, record SHA |
| Commit found with exact key, wrong parents | Landed but branch moved since (or raced) | Adopt the SHA for evidence; re-check ref; if ref no longer contains it → `duplicated`, new intent on new base |
| ≥2 commits with the same key | Bug or retried-with-new-key | `unknown` — block run, never guess |
| 0 matches, branch head == expected parent | Nothing landed | Safe to re-dispatch *with the same key* (bounded attempts) |
| 0 matches, branch head != expected parent | Someone else moved the branch | `duplicated/superseded` — new intent against new head (drift path), never force |
| CAS failure (`expectedHeadOid` / `staleOldObjectId`) on retry | Branch moved; your write did not apply *this* time | Re-probe (previous attempt may have landed!) before any new dispatch |
| `Reference already exists` (GH createRef) / GL 400 `already exists` | Ref exists; may be yours or not | Compare ref OID to intended SHA: equal → adopt; differ → someone else's → supersede |

### Per-provider probes (read-only)

**GitLab** (forge already implements the core in `writer.py:_resolve_unknown`):
1. `GET /projects/:id/repository/commits?ref_name=<branch>` → filter `message` contains
   `(forge-op:<key>)` AND `parent_ids == [expected_parent]`; exactly one match ⇒ adopted.
   Pass `trailers=true` if the marker moves to a `Forge-Op:` trailer — parsed server-side
   (<https://docs.gitlab.com/api/commits>).
2. Branch-create ambiguity: `GET /projects/:id/repository/branches/<name>` verifies the
   pre-existing branch is real.
3. MR: `GET /projects/:id/merge_requests?source_branch=&target_branch=&state=opened` —
   find-by-identity before POST; the provider's own uniqueness (1 open MR per pair) makes this
   reliable.

**GitHub:**
1. Commits: `GET /repos/{o}/{r}/commits?sha=<branch>&per_page=30` → client-side filter on
   `commit.message` for the marker AND single parent == expected. **Do not use
   `/search/commits`** — default-branch-only indexing
   (<https://docs.github.com/en/search-github/searching-on-github/searching-commits>).
   GraphQL alternative: `repository.ref.target.{ ... on Commit { history(first: 20) } }` with the
   `oid` check in the same round-trip.
2. Ref adoption: after a 422 on `createRef`, `GET /repos/{o}/{r}/git/ref/{ref}` and compare OID.
3. `createCommitOnBranch` CAS failure ⇒ first `GET ref` and inspect the new tip for *this* intent's
   marker before deciding the attempt failed.
4. PR: `GET /repos/{o}/{r}/pulls?head=<owner>:<branch>&state=open` (forge already has
   `get_pr_by_head`). Comment: scan `GET /repos/{o}/{r}/issues/{n}/comments?since=<journaled
   dispatch time>` for the marker in the body (lower bound via journaled time keeps the scan
   bounded — same trick forge already uses for Actions run discovery in
   `src/forge/execution/github_actions.py`).
5. Workflow dispatch: carry the operation key in `client_payload`; recovery lists
   `GET /repos/{o}/{r}/actions/runs?event=workflow_dispatch&created=>=<dispatch_time>` and matches
   the journaled run id (existing `ActionsHandle` discovery logic).

**Azure DevOps:**
1. Push/commit: `GET .../repositories/{id}/commits?searchCriteria.itemVersion.version=<branch>`
   (branch-scoped list, newest first) → filter `comment` for the marker and verify the returned
   commit's `parents`. Alternatively `GET .../pushes?searchCriteria.…&includeRefUpdates=true` and
   inspect the ref update result (`newObjectId`) of the journaled push id.
2. Ref state: `GET .../repositories/{id}/refs?filter=heads/<branch>` → compare `objectId` to
   `expected_parent` (move happened / did not).
3. PR: on 409 `TF401179`, `GET .../pullrequests?searchCriteria.sourceRefName=refs/heads/<b>&
   searchCriteria.targetRefName=refs/heads/<t>&searchCriteria.status=active` → adopt the returned
   `pullRequestId`.
4. Build dispatch: list `GET .../build/builds?definitions=<id>&branchName=…&queryTimeRange=…` and
   match the marker in build parameters (current client-side matching, made durable by journaling
   the queued build id in `remote_result` immediately after the POST returns).

### Distinguishing OUR previous repair commit from a NEW attempt

- The marker must be **unique per intent, not per process invocation**: if every retry mints a
  fresh key, a crashed attempt's commit is unfindable and a blind retry creates the duplicate R11
  warns about. The key lives in the intent row; retries reuse it. (forge's `writer.py` currently
  mints `uuid4().hex[:12]` per `apply()` call — moving minting into the persisted intent is the
  single most important fix.)
- The human-readable commit message may repeat across repair cycles (same issue, same file);
  only `(forge-op:<key>)` + parent OID constitute identity. This is already enforced in
  `_resolve_unknown` and must be preserved in the GitHub/ADO probes.
- If a repair cycle intentionally supersedes an old intent (new content), it gets a *new* intent
  row and *new* key; the probe for the old intent must then classify the new head as
  `duplicated/superseded` rather than adopting content it did not request.

### PR / comment / dispatch effects: find-or-adopt, never blind POST

| Effect | Find-by-identity query | Provider dedup backstop |
|---|---|---|
| GH Draft PR | `pulls?head=owner:branch&state=open` | none |
| GL MR | `merge_requests?source_branch&target_branch&state=opened` | 1 open MR per pair |
| ADO PR | `pullrequests?searchCriteria.sourceRefName&targetRefName&status=active` | 409 TF401179 |
| GH/GL issue comment | `comments?since=<dispatch time>` + marker in body | none (marker only) |
| GH/GL/ADO dispatch | runs/builds list + journaled time/params + marker | none |

Rule: every POST to these endpoints is preceded by the find query on recovery paths (and, where
cheap, on the hot path). Where the provider enforces uniqueness (GL MR, ADO PR), the duplicate
error is *also* an adoption signal — transition to find-by-identity instead of failing.

---

## PublicationIntent schema recommendation

### Table: `publication_intents`

```sql
CREATE TABLE publication_intents (
    id                  UUID PRIMARY KEY,              -- minted once at creation
    run_id              VARCHAR(64)  NOT NULL,         -- FK -> flow_runs
    step_run_id         UUID NULL,                     -- owning step, for lease/fence reuse
    provider            VARCHAR(16)  NOT NULL,         -- 'github' | 'gitlab' | 'azure'
    repo                VARCHAR(200) NOT NULL,         -- owner/name, project id, etc.
    operation           VARCHAR(16)  NOT NULL,         -- 'commit'|'branch'|'pr'|'comment'|'dispatch'
    target_ref          VARCHAR(255) NOT NULL,         -- branch (or issue# for comments)
    operation_key       VARCHAR(64)  NOT NULL,         -- (forge-op:<key>) — STABLE across retries
    expected_parent_oid VARCHAR(40)  NULL,             -- head captured pre-dispatch (NULL = new repo)
    expected_head       VARCHAR(40)  NULL,             -- caller-pinned drift guard
    content_digest      VARCHAR(64)  NOT NULL,         -- changeset tree/digest
    idempotency_scope   VARCHAR(64)  NOT NULL,         -- e.g. 'issue-42:publish' (probe correlation)
    state               VARCHAR(16)  NOT NULL DEFAULT 'requested',
        -- requested -> dispatched -> probing -> committed
        --                              -> adopted      (found a previous attempt's effect)
        --                              -> duplicated   (someone else / later repair moved ref)
        --                              -> unknown      (probe inconclusive) -> blocked run
        --                              -> failed       (deterministic provider rejection)
    attempt_count       INT NOT NULL DEFAULT 0,
    max_attempts        INT NOT NULL DEFAULT 3,
    next_probe_at       TIMESTAMPTZ NULL,              -- jittered backoff for probe/dispatch
    deadline_at         TIMESTAMPTZ NULL,              -- R17: copied from step deadline
    lease_owner         VARCHAR(100) NULL,             -- ADR-0005 lease so one worker probes
    lease_expires_at    TIMESTAMPTZ NULL,
    fence_token         BIGINT NULL,                   -- monotonic, from step lease grant
    provider_object_id  VARCHAR(80) NULL,              -- commit sha / PR number / build id
    remote_result       JSONB NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, repo, target_ref, idempotency_scope, operation_key)
);
CREATE INDEX idx_pintent_pending ON publication_intents (state, next_probe_at)
    WHERE state IN ('requested', 'dispatched', 'probing');
```

This generalizes what `ActionLog` + `_CommitIntent` already do for GitLab commits; `ActionLog`
rows can keep flowing (audit log) while `publication_intents` is the *operational* recoverable
state. Fields match forge's existing vocabulary (`StepRun` lease/fence/deadline, `ActionLog`
remote_result JSON).

### Recovery handler state machine

A reaper-adjacent scanner (same pattern as `evaluate_waiting_harness_one`) claims pending intents
whose `lease_expires_at < now` or `next_probe_at <= now`:

```
requested ──(deadline check: now + effect_budget ≤ deadline_at?)── no ──> reschedule (do NOT touch provider)
    │ yes
    ▼
dispatched ──> HTTP effect ──clear response──> committed  (journal completed)
    │ crash / timeout / 5xx / lost response
    ▼
probing ──> provider probe (identity tuple) ──┬─ exactly-one match ─────> committed|adopted (+ sha)
                                              ├─ zero match, head intact ─> re-dispatch, same key,
                                              │                             attempt_count++ (≤ max)
                                              ├─ zero match, head moved ──> duplicated (new intent)
                                              ├─ ≥2 matches ──────────────> unknown → block run
                                              └─ probe transport error ───> stay probing, jittered
                                                                            next_probe_at; after
                                                                            budget → unknown
```

Invariants (mirroring ADR-0005 and `writer.py`'s current contract):

1. **Intent before I/O.** The row (with `operation_key` and `expected_parent_oid`) is committed in
   the same transaction as the state transition that authorizes the effect — this closes R11.
2. **Same key forever.** All retries of one intent reuse `operation_key`; minting per call (as
   `writer.py` does today) moves to intent creation.
3. **Ambiguity escalates, never retried blind.** `unknown` blocks the run; only `probing →
   zero-match + head-intact` may re-dispatch, and only within `max_attempts`.
4. **Adoption is a first-class outcome.** `adopted` records the *found* provider object id; the
   workflow treats it identically to `committed` (the remote effect exists; the durable result is
   now filled in).
5. **Fencing.** A probe/re-dispatch only proceeds if the intent's `fence_token` still matches the
   live step lease — a superseded/cancelled run (publication grant revoked) must not resurrect
   effects (forge already has `_fence_valid` in `github_service.py`).

### Per-provider quirks that force per-provider probe implementations

- **GitHub**: no message search off the default branch ⇒ list+filter; `clientMutationId` useless
  for dedup ⇒ marker in message; CAS failure requires tip inspection before retry; Draft PR has no
  uniqueness ⇒ head-branch find query; commit search `since`/`parent:` qualifiers unusable here.
- **GitLab**: commits API has no CAS ⇒ `expected_parent_oid` match in the probe is the only
  provenance check; `force: true` must be banned; MR uniqueness helps; `trailers=true` gives
  server-side marker parsing.
- **Azure DevOps**: push is CAS-correct (`oldObjectId` ⇒ `staleOldObjectId`) but per-ref status
  must be read from the response's `refUpdates`; PR 409 is an adoption signal; build queue needs
  client-side marker matching forever; pushes are multi-commit in one POST, so the probe matches on
  the pushed commit's comment + parent.

### Deadlines before provider I/O (R17)

- **Check, then dispatch.** Every dispatch/probe path evaluates
  `now + effect_budget(provider, operation) ≤ deadline_at` *before* recording the intent for I/O.
  If it fails, reschedule the step (`next_probe_at`) — never start an HTTP effect that the lease
  reaper may reap mid-flight, because a reaped worker whose request still lands produces exactly
  the R11 ambiguity. Temporal's separation of start-to-close vs schedule-to-close budgets is the
  model: the *whole* effect (write + confirm) must fit inside the remaining lease.
  <https://docs.temporal.io/activity-definition>
- **Budget sizing.** Set `effect_budget` at a conservative percentile of observed provider latency
  (AWS guidance: acceptable false-timeout rate ⇒ percentile; include worst-case network).
  <https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/>
- **Bounded retries with jitter.** `delay = random(0, min(cap, base · 2^attempt))` (full jitter),
  `attempt_count ≤ max_attempts`, honor `retry-after` / secondary rate-limit guidance, and stop at
  budget even if attempts remain — a retry that cannot finish inside the deadline is a new
  problem, not a retry.
- **Retry classification.** Only *ambiguous* outcomes (timeout, 5xx, lost response) go to
  `probing`. Deterministic rejections (4xx validation, branch drift, `Reference already exists`
  with foreign OID) transition directly to `failed`/`duplicated` — AWS: don't retry what won't
  change; GitHub: don't ignore repeated 4xx.

---

## Open questions

1. **Marker format for commits: suffix vs trailer.** Forge currently appends `(forge-op:<key>)`.
   A RFC-style trailer (`Forge-Op: <key>`) plus GitLab's `trailers=true` listing is cleaner to
   parse, but GitHub REST list-commits has no trailer parsing, so both providers end up doing
   substring matching anyway. Suffix is fine; document it as frozen contract.
   <https://docs.gitlab.com/api/commits>
2. **GitHub signed-commit rulesets.** Repos requiring signed commits must use
   `createCommitOnBranch` (REST `create commit` objects are unsigned unless the token signs) — the
   gh-aw PR shows the exact fallback chain and its new-branch pitfall. Does forge's GitHub path
   need the same GraphQL-first policy, and does that change the probe (GraphQL history vs REST
   list)?
   <https://github.com/github/gh-aw/pull/22008>
3. **Two-step GitHub REST path (create commit object + `updateRef`)** creates a *worse* ambiguity
   window (commit object exists, ref not moved). Should forge forbid it and require GraphQL
   `createCommitOnBranch` (atomic commit+ref-move) everywhere, accepting the app-auth requirement?
4. **Probe depth window.** Probes scan the newest N commits for the marker. A long stall (worker
   down for days, busy branch) can push the commit past N. Fix: always also probe
   `expected_parent`'s children via provider APIs (GitLab: commit refs endpoint; GitHub: ref
   compare / GraphQL `history`), or bound with `since = intent.created_at`. What N and what
   fallback?
5. **Who probes, and how often?** Intent scanning should reuse the ADR-0005 lease machinery
   (`lease_owner`/`fence_token` above) but the reaper interval and `next_probe_at` backoff base
   are tunables. Probe calls cost rate limit (GitHub 5000/h REST, GraphQL points) — do we need a
   per-provider probe rate limiter?
   <https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api>
6. **`unknown` policy.** ADR-0005 says block the run on `unknown_outcome`. Should an operator
   "reconcile now" action exist (manual probe → adopt/abandon), and does that need an audit entry?
7. **Retention/GC.** Terminal intents (`committed`/`adopted`/`duplicated`/`failed`) can be pruned
   after N days (Stripe prunes keys at 24 h; forge's effects are addressable forever via the
   remote repo, so the durable record only needs to outlive debugging). What N?
8. **Do comment/PR bodies need the machine marker**, or is run-id text enough? Bernstein's
   contract (`<!-- bernstein-key: ... -->`) hides markers from humans; forge's current notes embed
   run ids visibly. Visible ids double as audit trail — but the probe should match on an exact
   token to avoid false positives.
   <https://bernstein.readthedocs.io/en/latest/trackers/contract>
