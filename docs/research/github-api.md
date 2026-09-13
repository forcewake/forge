# GitHub API Research: GitHub App-Based Automation Pipeline (verified 2026-09)

Implementation reference for a GitHub source/execution adapter driven by a GitHub App.
Researched against official documentation (docs.github.com, github.blog) and the live
public GraphQL schema (`https://docs.github.com/public/fpt/schema.docs.graphql`) on 2026-09-13.

Convention: **[documented]** = stated in official GitHub docs/schema; **[observed]** = verified
against the live API/schema during research; **[inference]** = derived design conclusion, not an
explicit GitHub statement.

---

## 1. GitHub App Authentication (2026 current)

### 1.1 JWT for App authentication — [documented]
- Algorithm: the JWT **must be signed with `RS256`** (`alg: RS256` header) using the App's RSA private key (PEM, `OpenSSL::PKey::RSA`-style material).
- Required claims:
  - `iss` — the App's **client ID** (recommended) or App ID. Find both on the app settings page or via `GET /app`.
  - `iat` — issued-at; docs recommend setting it **60 seconds in the past** to absorb clock skew; keep server clocks NTP-accurate.
  - `exp` — expiration; **must be no more than 10 minutes into the future**. After expiry the JWT cannot be used to request installation tokens.
- Header usage: **JWTs must use `Authorization: Bearer <JWT>`** — the `token` prefix is *not* accepted for JWTs.
- JWTs are only needed for a small surface: `GET /app`, `GET /app/installations`, `POST /app/installations/{installation_id}/access_tokens`, App webhook config/delivery endpoints. Everything else uses the installation token.

### 1.2 Installation access tokens — [documented]
- Mint: `POST /app/installations/{installation_id}/access_tokens` with `Authorization: Bearer <JWT>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: <date>` (docs currently show `2026-03-10` in examples; `2022-11-28` remains the widely referenced calendar version — send an explicit pinned value).
- Optional request body:
  - `repositories` (names) or `repository_ids` — **up to 500 repos**; narrows the token below the installation's scope (a token can never exceed the installation's access). Omit = all installation repos.
  - `permissions` — narrows to a subset of the App's granted permissions.
- Response: `{ token, expires_at, permissions, repositories }`.
- **Expiry: 1 hour.** There is **no refresh token / no refresh endpoint** for installation tokens — re-mint by repeating the JWT flow (Octokit SDKs do this automatically). [documented]
- Find installation IDs via `GET /app/installations`, `GET /users/{username}/installation`, `GET /repos/{owner}/{repo}/installation`, `GET /orgs/{org}/installation`, or from any webhook payload's `installation` object.

### 1.3 Token format change (2026) — [documented, changelog]
- Classic installation tokens were exactly 40 chars (`ghs_` + 36). A **new stateless format `ghs_APPID_JWT`** began rolling out (announced 2026-04-24; per-request override header for early testing 2026-05-15). New tokens are ~520 chars, JWT-like, and variable length.
- Do **not** validate token length/shape; GitHub's recommended regex for both formats: `ghs_[A-Za-z0-9\.\-_]{36,}`.

### 1.4 Token alternatives — [documented]
- **User access tokens (UAT)**: expire after 8 hours when expiring-tokens enabled; **refresh tokens expire after 6 months**. Use only for user-attribution flows; server automation should use installation tokens.
- Early revocation endpoints exist: `DELETE /installation/token` (installation token) and `DELETE /applications/{client_id}/token` (user token).

### 1.5 Permission sets needed for the pipeline — [documented]
GitHub Apps use fine-grained repository permissions (each `read` or `read+write`); `Metadata` (read-only) is mandatory for all apps with any repo access. For this pipeline:

| Capability | Permission | Level |
|---|---|---|
| Read issues / PR comments | `Issues` | read (write needed to *post* comments) |
| Post issue/PR comments | `Issues` | write |
| Read PRs, find PRs by branch | `Pull requests` | read |
| Create Draft PRs, merge management reads | `Pull requests` | write |
| Push commits to a branch (GraphQL commit, git data, contents) | `Contents` | write |
| Read Actions workflow runs / jobs / logs | `Actions` | read |
| Read check runs / check suites | `Checks` | read |
| Create/update `.github/workflows/*` files | `Workflows` | write (only needed if the app writes workflow files — `Contents:write` alone will not modify `.github/workflows/`) |
| Create/modify rulesets, branch protection | `Administration` | read/write (avoid granting unless the app must self-configure protection) |
| Report commit statuses (legacy Status API) | `Commit statuses` | read/write (only if emitting status contexts) |
| Manage the App's own webhook config, list deliveries | JWT, no repo permission | — |

Note [documented]: the Checks API (`/check-runs`, `/check-suites`) is GitHub App territory; fine-grained PATs do not get a `Checks` permission.

### 1.6 Private key → credential broker pattern — [inference, built on documented primitives]
GitHub's docs mandate key-vault storage of the private key (it "grants access to every account that the app is installed on") and recommend caching tokens until expiry. The broker pattern follows:
- Exactly one service (the broker) holds the App private key. Worker/execution services never see the key.
- Broker mints a short-lived JWT (`iat` −60 s, `exp` ≤ 10 min), calls `POST /app/installations/{id}/access_tokens` scoped to `repositories` + minimal `permissions`, and hands the worker only the token + `expires_at`.
- Worker caches the token until `expires_at − safety-margin` (e.g., 5 min) or until a `401`; on `401`, request a fresh token rather than retrying with the same one [documented guidance: regenerate on expiry; treat 401 as re-auth signal per best-practices page].
- Per-job tokens give per-job blast radius; token scoping is enforced server-side by GitHub.

### Sources (§1)
- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
- https://github.blog/changelog/2026-04-24-notice-about-upcoming-new-format-for-github-app-installation-tokens/
- https://github.blog/changelog/2026-05-15-github-app-installation-tokens-per-request-override-header/
- https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types

---

## 2. Webhooks

### 2.1 Signature validation — [documented]
- Header: `X-Hub-Signature-256`, value = `sha256=` + lowercase hex of **HMAC-SHA256(webhook_secret, RAW request body bytes)**.
- Compute over the **raw bytes before any parsing**; if your stack decodes text, treat the payload as UTF-8 (payloads may contain Unicode). Proxies/load balancers must not modify body or headers before verification.
- Use **constant-time comparison** (`crypto.timingSafeEqual`, `Rack::Utils.secure_compare`, Python `hmac.compare_digest`); never `==`.
- Legacy `X-Hub-Signature` is HMAC-**SHA1**, kept only for compatibility — require and validate only `X-Hub-Signature-256`.
- If no secret is configured on the webhook, the header is **not sent at all** — treat a missing header as invalid.
- Official test vector: secret `It's a Secret to Everybody`, payload `Hello, World!` → `757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17` (header value `sha256=757107…`).

### 2.2 Delivery headers and idempotency — [documented]
- `X-GitHub-Event` — event name (e.g. `issue_comment`); check it before dispatching; event/action surface keeps growing.
- `X-GitHub-Delivery` — GUID identifying the delivery. Use it to ensure uniqueness per event (dedup/idempotency key). **Caveat [documented]: on redelivery, `X-GitHub-Delivery` is identical to the original delivery** — so "seen before" must not be treated as "malicious replay"; dedupe means *drop duplicates*, and manual redelivery intentionally reuses the ID. (This GUID is what GitHub's REST calls the delivery `id`; there is no separate documented `Delivery-ID` HTTP header for standard webhooks.)
- `X-GitHub-Hook-ID` / `X-GitHub-Hook-Installation-Target-ID` are also present on App deliveries [observed in delivery records; not described on the validation page].
- Respond **2XX within 10 seconds** or GitHub drops the connection and marks the delivery failed. **No automatic retries** — process asynchronously (enqueue raw body + headers, ack fast), and backfill via the deliveries API.
- Delivery management REST (JWT-authenticated, App-level webhook) [documented]:
  - `GET /app/hook/deliveries` — `per_page` (max 100, default 30), `cursor` (opaque cursor pagination via Link header), `status` (`success` | `failure`; success = HTTP 200–399, failure = 400–599).
  - `GET /app/hook/deliveries/{delivery_id}`
  - `POST /app/hook/deliveries/{delivery_id}/attempts` — redeliver; returns `202`.
  - `GET /app/hook/config`, `PATCH /app/hook/config` — webhook config.
- At-least-once semantics: design the consumer to be idempotent keyed on `X-GitHub-Delivery` + `X-GitHub-Event` + event `action` + resource id, and tolerate out-of-order arrival (e.g., `push` for an older SHA after a newer one) [inference; ordering is not guaranteed or documented].

### 2.3 `issue_comment` event — [documented]
- Fires for comments on **issues and pull requests** (PRs are issues): `action ∈ {created, deleted, edited}` plus issue-specific `pinned`/`unpinned` variants listed in the event reference. Payload: `action`, `comment`, `issue`, `repository`, `sender`, `organization`, `installation`.
- **Distinguishing PR comments from issue comments:** the `issue` object contains a `pull_request` key when the issue is a PR (presence check on `payload.issue.pull_request`). This is the standard technique; the docs page itself doesn't spell it out [inference on technique, payload shape documented].
- Inline diff review comments are a different event: `pull_request_review_comment` (and `pull_request_review` for review submit/state). Do not parse those out of `issue_comment`.
- App subscription requirement: at least **Issues: read**.

### 2.4 `ping` event — [documented]
- Sent **when you create a webhook** ("a confirmation from GitHub that you configured the webhook correctly"). No `action` field. Payload: `zen` (random GitHub wisdom string), `hook_id`, `hook`.
- Respond 2XX; a successful ping is the cheapest end-to-end check that the URL + secret validation path works.
- App-level webhooks have **no documented ping-trigger API** (`POST /app/hook/pings` does not exist; open feature request). Repository/org webhooks do: `POST /repos/{owner}/{repo}/hooks/{hook_id}/pings`, `POST /orgs/{org}/hooks/{hook_id}/pings`.

### 2.5 IP allowlists / transport security — [documented + observed]
- Best practice: put the receiver behind an IP allowlist containing GitHub's webhook egress ranges, obtained from `GET https://api.github.com/meta` → `hooks` key. Live values [observed 2026-09-13]:
  `192.30.252.0/22`, `185.199.108.0/22`, `140.82.112.0/20`, `143.55.64.0/20`, `2a0a:a440::/29`, `2606:50c0::/32`.
- IP allowlisting is a secondary defense; **signature verification remains the primary control** (ranges change without notice — refresh periodically). Use HTTPS, high-entropy secret, secure storage (never hardcode/commit).

### Sources (§2)
- https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
- https://docs.github.com/en/webhooks/webhook-events-and-payloads
- https://docs.github.com/en/rest/apps/webhooks
- https://api.github.com/meta (`hooks` key)
- https://github.com/orgs/community/discussions/179374 (no app-level ping endpoint)

---

## 3. Writing Changes: `createCommitOnBranch`

### 3.1 Mutation shape — [documented + verified in public schema]
```graphql
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    clientMutationId
    commit { oid url message committedDate }
    ref  { name target { ... on Commit { oid } } }
  }
}
```
`CreateCommitOnBranchInput` (exact fields from the public schema):
- `branch: CommittableBranch!` — either `{ id: <Ref node ID> }` or `{ repositoryNameWithOwner: "owner/repo", branchName: "feature/x" }` (`branchName` is the **unqualified** name; internally the ref must live under `refs/heads/`).
- `message: CommitMessage!` — `{ headline: String!, body: String? }`.
- `fileChanges: FileChanges` — `{ additions: [FileAddition!] = [], deletions: [FileDeletion!] = [] }`
  - `FileAddition`: `{ path: String!, contents: Base64String! }` — contents are **base64-encoded**.
  - `FileDeletion`: `{ path: String! }`.
- `expectedHeadOid: GitObjectID!` — "The git commit oid expected at the head of the branch prior to the commit." This is the CAS token: the mutation fails if the branch tip differs. Fetch it via `repository { ref(qualifiedName: "refs/heads/branch") { target { ... on Commit { oid } } } }`.
- `clientMutationId: String` — "A unique identifier for the client performing the mutation"; echoed verbatim in the payload. Pure client-side correlation only.

Semantics [documented]: appends a commit whose **parent is the branch HEAD** and repoints the branch ref (like `git commit` + push). The commit is authored by the authenticated credential; the API **"does not support specifying the author or committer"** and never will (use the Git Database REST API if you need custom authorship). Commits are **automatically signed by GitHub where supported and marked Verified**. Branch creation is implicit if the branch name doesn't exist? — **No** [documented]: the ref must be a branch; to start from a base commit create the ref first via REST `POST /repos/{owner}/{repo}/git/refs` (`{"ref": "refs/heads/x", "sha": "<base>"}`) or GraphQL `createRef`.

### 3.2 CAS mismatch behavior
- **[documented in schema/docs]:** `expectedHeadOid` is required and acts as an optimistic-concurrency check.
- **[community-verified, not in official docs]**: on mismatch GitHub returns HTTP **200** (GraphQL uses HTTP only as transport) with `data.createCommitOnBranch = null` and an errors entry:
```json
{
  "type": "STALE_DATA",
  "path": ["createCommitOnBranch"],
  "locations": [{ "line": 1, "column": 52 }],
  "message": "Expected branch to point to \"f786b7e…\" but it did not. Pull and try again."
}
```
  Match on `errors[].type == "STALE_DATA"` (do not string-match the message). Recovery: re-read head `oid`, re-apply, retry — i.e., last-writer-wins is *not* silent; concurrent writers are detected.

### 3.3 Limits and file-type restrictions
- **[documented]** The GraphQL commits reference and schema publish **no explicit byte-size limit** for `fileChanges`. Do not rely on the absence of a limit: GraphQL endpoints are subject to secondary rate limits (2,000 points/min; mutations cost 5 points each) and the documented request-body/CPU secondary limits.
- **[community-documented]** Only regular, non-executable files can be added/updated (no symlinks, no submodules, no mode changes) — community discussion #191953. If you need modes/symlinks or custom authorship → REST Git Data API.
- REST comparison points [documented]: reading content via `GET /repos/{owner}/{repo}/contents/{path}` supports everything ≤ 1 MB; 1–100 MB only `raw`/`object` media types; > 100 MB unsupported. Git blobs via `POST /repos/{owner}/{repo}/git/blobs` accept up to 100 MB blobs.

### 3.4 `createCommitOnBranch` vs REST Git Data API — [inference on selection]
- Use **GraphQL `createCommitOnBranch`** for the common automation case: append/replace/delete a handful of regular files on a branch, single call, automatic verified signature, CAS safety via `expectedHeadOid`.
- Use **REST Git Data API** (`git/blobs` → `git/trees` → `git/commits` → `patch git/refs`) when you need: executable bits/symlinks/submodules, custom author/committer/date, very large content streams (blobs up to 100 MB), or non-branch ref surgery. Multi-request = no transactionality; you must implement the CAS yourself by passing the old sha to `update ref` and handling `422`.
- `clientMutationId` is **not** an idempotency key [documented semantics: echoed opaque string]. GitHub does not dedupe on it. However, `expectedHeadOid` gives de-facto exactly-once-per-base semantics: a retry after a *successful but timed-out* attempt fails with `STALE_DATA` because the head moved, so duplicate commits cannot be silently created [inference]. Timeout-handling rule: on network timeout, re-read head and only retry if `oid` still equals your `expectedHeadOid`.

### Sources (§3)
- https://docs.github.com/en/graphql/reference/commits (`createCommitOnBranch`)
- https://docs.github.com/public/fpt/schema.docs.graphql (CreateCommitOnBranchInput, FileChanges, FileAddition, FileDeletion, CommittableBranch, CommitMessage, CreateCommitOnBranchPayload)
- https://gist.github.com/brasic/964dfc371d524a09d602745ae3b238ff (STALE_DATA error example)
- https://github.com/orgs/community/discussions/191953 (file type limitation)
- https://docs.github.com/en/rest/repos/contents (size tiers)
- https://docs.github.com/en/rest/git/blobs (100 MB blob limit)

---

## 4. Draft Pull Requests and SHA Semantics

### 4.1 Creation — [documented]
- REST: `POST /repos/{owner}/{repo}/pulls` with `{ title, head, base, body?, draft: true, maintainer_can_modify? }`.
  - `head`: branch name; for cross-repo PRs in the same network: `username:branch`. `base`: existing branch in the **same repository** (you cannot target another repo's base).
  - Response `head` object: `{ label, ref, repo, sha, user }`; `head.sha` = tip of the head branch.
- GraphQL: `createPullRequest(CreatePullRequestInput)` — fields (schema-verified): `baseRefName: String!`, `headRefName: String!`, `repositoryId: ID!`, `title: String!`, `body`, `draft: Boolean = false`, `maintainerCanModify: Boolean = true`, `headRepositoryId: ID` (cross-repo).
- Draft → ready: GraphQL `markPullRequestReadyForReview` [documented in GraphQL mutations reference].

### 4.2 Finding an existing open PR for a branch — [documented]
- REST: `GET /repos/{owner}/{repo}/pulls?state=open&head=<owner>:<branch>&base=<base>` (`head` filter format: "user:ref-name"; default `state=open`, `sort=created`).
- GraphQL: `repository.pullRequests(headRefName: "branch", states: OPEN)` or `repository.ref(qualifiedName: …) { associatedPullRequests(states: OPEN) }`.

### 4.3 Which SHA do checks run on? — [documented]
- GitHub materializes read-only PR refs: `refs/pull/<N>/head` (head tip) and `refs/pull/<N>/merge`.
- For Actions `pull_request` runs [documented, events reference]: **`GITHUB_REF = refs/pull/<N>/merge`** and **`GITHUB_SHA = "Last merge commit on the GITHUB_REF branch"`** — i.e., **CI runs against the merged result, not the head commit** ("If you want to get the commit ID for the last commit to the head branch of the pull request, use `github.event.pull_request.head.sha` instead").
- Consequence: check runs for a PR commonly report `head_sha` = the **merge commit SHA**, not `pull_request.head.sha`.
- `GET /repos/{owner}/{repo}/pulls/{n}` → `merge_commit_sha` = "the SHA of the test merge commit" **before** merging (post-merge it becomes the actual merge/squash/rebase result SHA, method-dependent). `mergeable` may be `null` while a background job computes mergeability — poll.
- **How to know which SHA a check reported on:** every check run object carries `head_sha`; every workflow run carries `head_sha` too. To assemble PR status: fetch check runs for **both** `pull_request.head.sha` and the PR's merge ref, then group by the `head_sha` each item reports [inference on the collection strategy; field semantics documented].

### 4.4 Useful PR state fields (GraphQL `PullRequest`) — [schema-verified]
- `headRefOid`, `baseRefOid` (survive ref deletion), `isDraft: Boolean!`, `isInMergeQueue: Boolean!`
- `mergeable: MergeableState!` (`MERGEABLE`, `CONFLICTING`, `UNKNOWN`)
- `mergeStateStatus: MergeStateStatus!` — `BEHIND` (out of date vs base), `BLOCKED` (failing/unsatisfied protection), `CLEAN` (mergeable & passing), `DIRTY` (merge conflict), `HAS_HOOKS` (passing + pre-receive hooks), `UNSTABLE` (mergeable but non-passing commit status), `UNKNOWN` (`DRAFT` value is deprecated — use `isDraft`).
- `reviewDecision` (`APPROVED`/`CHANGES_REQUESTED`/`REVIEW_REQUIRED`), `statusCheckRollup: StatusCheckRollup` (connection whose nodes are union `StatusCheckRollupContext = CheckRun | StatusContext`), `potentialMergeCommit: Commit`.

### Sources (§4)
- https://docs.github.com/en/rest/pulls/pulls
- https://docs.github.com/en/graphql/reference/mutations (createPullRequest, markPullRequestReadyForReview)
- https://docs.github.com/public/fpt/schema.docs.graphql (CreatePullRequestInput, PullRequest, MergeStateStatus, StatusCheckRollupContext)
- https://docs.github.com/actions/using-workflows/events-that-trigger-workflows (GITHUB_SHA/GITHUB_REF for pull_request)

---

## 5. Checks & Actions Status APIs

### 5.1 Two status systems — [documented]
- **Commit statuses** (legacy): `POST /repos/{owner}/{repo}/statuses/{sha}` creates a context (e.g. `ci/lint`); `GET /repos/{owner}/{repo}/commits/{ref}/status` returns the combined status (latest per context); `GET …/commits/{ref}/statuses` returns the raw list.
- **Check runs** (modern): created by GitHub Apps — "GitHub Actions generates checks, not commit statuses, when workflows are run." A PR's Checks tab is populated by check runs only.
- GraphQL `statusCheckRollup` merges both: nodes are `CheckRun | StatusContext`.

### 5.2 Check runs / check suites — [documented]
- `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` — `ref` can be a commit SHA, `heads/BRANCH_NAME`, or `tags/TAG_NAME`. Filters: `check_name`, `status`, `filter`, `per_page` (max 100)/`page`.
  - `status` enum: `queued, in_progress, completed, waiting, requested, pending` (`waiting`/`requested`/`pending` are Actions-only states, not settable manually).
  - `conclusion` enum: `success, failure, neutral, cancelled, skipped, timed_out, action_required, null`. (`neutral` and `skipped` count as success for dependent checks; a **skipped required job reports Success and does not block merge** [documented, status-checks page].)
  - **Producer identification:** each check run embeds `app` — "the GitHub App that created the run" (`id`, `slug`, …). Actions-created checks appear under the `github-actions` App [observed behavior; the field contract is documented], so `app.slug`/`app.id` is the reliable "check producer" discriminator. External checks can also carry `external_id` (integrator's own reference).
  - Caps: max 1,000 check runs with the same name; listing for a ref is limited to the **1,000 most recent check suites**.
- `GET /repos/{owner}/{repo}/commits/{ref}/check-suites` — suites group runs; suite carries `head_branch`, `head_sha`, `conclusion`, and `app` (producer). Suite events: `check_suite` webhook (`requested`/`re-requested`/`completed`).

### 5.3 Required checks semantics — [documented]
- If a branch target requires status checks, "they must pass before the pull request can be merged."
- Required-but-never-reported: a required check that hasn't reported yet keeps the PR in blocked state; Actions-only transient states include `expected` ("waiting for a status to be reported") / `waiting`. Rules may be skipped per-commit with git trailers `skip-checks: true` / requested via `request-checks: true` (skipping a required check may still be refused).
- Check data retention: **400 days**, then archived, deleted 10 days after archival; archived *required* checks must be rerun before merging.

### 5.4 Actions workflow runs — [documented]
- `GET /repos/{owner}/{repo}/actions/runs` filters: `head_sha` ("workflow runs associated with the specified head_sha"), `branch`, `event`, `status`, `actor`, `created`, `check_suite_id`, `exclude_pull_requests`. **Filtered searches return up to 1,000 results** — iterate with narrower windows (time/event) for long histories [documented limit; strategy inference].
- Workflow run fields: `id`, `name`, `workflow_id`, `run_number` (per-workflow counter), `run_attempt` (re-run counter), `event`, `status`, `conclusion`, `head_branch`, `head_sha`, `path` (`.github/workflows/x.yml@ref`), `triggering_actor`, `check_suite_id`, `pull_requests[]`, `created_at`, `updated_at`, `display_title`.
- Re-runs/attempts: `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}` (same schema as Get a workflow run).
- Logs: `GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs` and `GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/logs` → **302** with expiring `Location` URL (**expires in 1 minute**). Per-job logs: `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs` (also 302); list jobs with `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs`.
- Bridge to checks: `check_suite_id` on the run links to the suite → check runs → the `app` producer and per-check conclusions shown on the PR.

### Sources (§5)
- https://docs.github.com/en/rest/checks/runs
- https://docs.github.com/en/rest/checks/suites
- https://docs.github.com/en/rest/actions/workflow-runs
- https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
- https://docs.github.com/public/fpt/schema.docs.graphql (StatusCheckRollupContext, CheckRun, CheckSuite)

---

## 6. Actions Trigger Semantics (2026)

### 6.1 `GITHUB_TOKEN` recursion prevention — [documented, exact quotes]
- "When you use the repository's `GITHUB_TOKEN` to perform tasks, events triggered by the `GITHUB_TOKEN` … **will not create a new workflow run**" — "this behavior prevents you from accidentally creating recursive workflow runs."
- Exceptions: "**`workflow_dispatch` and `repository_dispatch` events always create workflow runs**."
- Push-created `pull_request` events: "When a pull request is created or updated by a workflow using `GITHUB_TOKEN`, `pull_request` events with the `opened`, `synchronize`, or `reopened` activity types create workflow runs that **require approval**"; other activity types create no runs.

### 6.2 App-token pushes — what our pipeline must expect — [documented]
- "**You can use a GitHub App installation access token or a personal access token instead of `GITHUB_TOKEN`**" to trigger further workflows. "Using one of these alternatives also lets `pull_request` workflows run **automatically (without the approval prompt)**."
- Therefore: **commits pushed by our App's installation token DO trigger `push`-triggered workflows normally** (that is the documented workaround for recursion prevention), and PRs opened by the App get their `pull_request` workflows started without the fork-approval prompt. If the pipeline must *not* re-trigger CI, suppress explicitly via commit message markers or `skip-checks: true` trailer rather than relying on any token behavior [inference].
- Skip markers [documented, "Skipping workflow runs"]: commit messages containing `[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]`, `[actions skip]` skip `push`/`pull_request`-triggered workflows; the git trailer `skip-checks: true` (documented on the status-checks page) similarly opts a push out.

### 6.3 First-time contributor approval — [documented]
- "When a first-time contributor submits a pull request to a public repository, a maintainer with write access may need to approve running workflows on the pull request." Approving requires **write access**; approvers inspect (especially `.github/workflows/` changes) then "Approve workflows to run."
- "**Workflow runs that have been awaiting approval for more than 30 days are automatically deleted.**"
- Configurable at repo/org/enterprise level ("Approval for running fork pull request workflows from contributors": first-time/all outside contributors). Plan automation around a `waiting`/approval-pending state for external PRs.

### 6.4 `workflow_run` — [documented]
- Triggers *after* another workflow runs; filter by triggering workflow name + branches (e.g., "when the workflow named `Build` runs on a branch whose name starts with `releases/`"); cannot combine `branches` and `branches-ignore`. Runs in the context of the default branch with a privileged token — the documented safer alternative to `pull_request_target` for privilege separation, but "should treat artifacts uploaded from other workflows with caution."

### 6.5 `pull_request_target` risks — [documented, security-hardening guide]
- "`pull_request_target` and `workflow_run` workflow triggers, when used with the checkout of an untrusted pull request, expose the repository to security compromises… These workflows are privileged, which means they share the same cache of the main branch" and "may have repository write access and access to referenced secrets… These vulnerabilities can be exploited to take over a repository."
- "Workflows that use these triggers must not explicitly check out untrusted code." "Avoid using the `pull_request_target` workflow trigger if it's not necessary." (See GitHub Security Lab "Preventing pwn requests".)

### 6.6 `GITHUB_TOKEN` permissions and rate budget — [documented]
- Default `GITHUB_TOKEN` permissions for newly created repos/orgs are **read-only** (changelog 2023-02-02); legacy repos default read/write; the repo/org "Workflow permissions" setting and the workflow-level `permissions:` key control it.
- Rate budget: `GITHUB_TOKEN` = **1,000 requests/hour per repository** (15,000 on Enterprise Cloud).

### Sources (§6)
- https://docs.github.com/actions/using-workflows/triggering-a-workflow
- https://docs.github.com/actions/using-workflows/events-that-trigger-workflows
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks
- https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
- https://docs.github.com/actions/reference/authentication-in-a-workflow
- https://github.blog/changelog/2023-02-02-github-actions-updating-the-default-github_token-permissions-to-read-only/
- https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api

---

## 7. No-Merge Enforcement (rulesets)

### 7.1 Rulesets REST API — [documented]
- Repo-level: `GET|POST /repos/{owner}/{repo}/rulesets`, `GET|PUT|DELETE /repos/{owner}/{repo}/rulesets/{ruleset_id}`, history at `GET …/{ruleset_id}/history[/versions/{version_id}]`, per-branch view `GET /repos/{owner}/{repo}/rules/branches/{branch}`. Org-level equivalents under `/orgs/{org}/rulesets` with `ruleset_source`/`ruleset_target` selectors.
- `enforcement`: `active` | `disabled` | `evaluate` (evaluate = Enterprise only).
- Conditions: `{ "ref_name": { "include": ["refs/heads/main", "~DEFAULT_BRANCH"], "exclude": ["refs/heads/dev*"] } }`.
- `target`: `branch` | `tag` | `push`.
- Managing rulesets requires the **Administration** repo permission (read/write) — map rules endpoints accordingly per the Apps permission reference [documented table pattern; confirm exact row when wiring].

### 7.2 Rule types (exact `type` strings) — [documented]
`creation`, `update`, `deletion`, `required_linear_history`, `merge_queue`, `required_deployments`, `required_signatures`, `pull_request`, `required_status_checks`, `non_fast_forward`, `commit_message_pattern`, `commit_author_email_pattern`, `committer_email_pattern`, `branch_name_pattern`, `tag_name_pattern`, `workflows`, `code_scanning`, `copilot_code_review`, `file_path_restriction`, `max_file_path_length`, `file_extension_restriction`, `max_file_size`.

Key parameter sets:
- `required_status_checks`: `{ required_status_checks: [{ context, integration_id? }], strict_required_status_checks_policy: bool, do_not_enforce_on_create: bool }`. `context` = check/status name; `integration_id` = the **GitHub App ID** that must have produced the check (disambiguates same-named checks from different producers; the producing app "must have recently submitted a check run"). `strict_required_status_checks_policy` = "require branches to be up to date before merging".
- `pull_request`: `{ required_approving_review_count, dismiss_stale_reviews_on_push, require_code_owner_review, require_last_push_approval, required_review_thread_resolution, allowed_merge_methods: ["merge","squash","rebase"], dismissal_restriction, required_reviewers }`.
- `merge_queue`: `grouping_strategy: ALLGREEN|HEADGREEN`, `merge_method`, `check_response_timeout_minutes`, `max_entries_to_build`, `min_entries_to_merge(_wait_minutes)`.
- Pattern rules: `{ name, negate, operator: starts_with|ends_with|contains|regex, pattern }`.

### 7.3 Bypass actors and whether an App can be exempted — [documented]
```json
"bypass_actors": [
  { "actor_id": <id>, "actor_type": "Integration", "bypass_mode": "always" }
]
```
- `actor_type`: `Integration` (= a GitHub App installation), `OrganizationAdmin`, `RepositoryRole`, `Team`, `DeployKey`, `User`. `bypass_mode`: `always` (default), `pull_request` (branch rulesets only), `exempt`.
- So yes — **an App can be exempted from rulesets** by listing its installation as an `Integration` bypass actor. `GET …/rulesets` response includes `current_user_can_bypass: always|pull_requests_only|never|exempt` for the caller; `bypass_actors` is only returned to callers with write access to the ruleset.

### 7.4 "Can commit but never merge" — confirmation
- **Confirmed [documented by absence]:** GitHub's permission model has **no token scope or permission named "merge"**. The fine-grained permission list (Actions, Administration, Checks, Commit statuses, Contents, Issues, Metadata, Pull requests, Workflows, …) contains nothing that gates merging. `Contents: write` (required to push commits) inherently permits fast-forward merge actions on unprotected branches; merging a PR requires `Pull requests: write` **or** `Contents: write`.
- Merge gating is therefore **policy, not scope**: it comes from rulesets/branch protection on the base branches.
- Enforcement recipe for an automation App that must write files but never merge [inference, built strictly on documented mechanics]:
  1. Request only `Contents: write` + the read/`Issues: write` set above; **do not add the App installation to any `bypass_actors`** list on base-branch rulesets.
  2. Apply an `active` ruleset targeting `refs/heads/main` (and release branches) with rule `pull_request` (required approvals, `require_last_push_approval`) and `required_status_checks` bound to the pipeline's check contexts (optionally pinned via `integration_id` to a CI App).
  3. With no bypass grant, the App's pushes are subject to the same rules as any actor: it can create feature branches and commit to them, but it cannot merge into protected bases, cannot force-push (`non_fast_forward` default-blocked), and cannot delete protected refs (`deletion` rule).
  4. Keep `Administration` ungranted so the App cannot alter the rulesets that constrain it.
- Residual risk [inference]: anyone who controls the App's private key controls a `Contents: write` actor on unprotected refs — constrain feature-branch/creation rules too (`creation`, `ref_name` patterns) if branch sprawl matters.

### Sources (§7)
- https://docs.github.com/en/rest/repos/rules
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/defining-the-mergeability-of-pull-requests/about-protected-branches
- https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps

---

## 8. Best Practices for CI/Automation Apps

### 8.1 Token lifecycle — [documented]
- Cache installation tokens and **use them until expiry** (1 h); regenerate when expired or on `401`. Do not mint a fresh token per request (wasteful; token-creation requests are themselves secondary-rate-limited: no more than **2,000 OAuth/access-token creation requests per hour**).
- Early revocation available via `DELETE /installation/token` — use when a credential may have leaked or before long-lived cache invalidation.
- Never authenticate as a PAT; use user access tokens only when user attribution is required.

### 8.2 Mid-run failure handling — [documented guidance + inference on mapping]
- Installation uninstalled / suspended / permissions pending → the App receives `installation` webhook events (`deleted`, `new_permissions_accepted`, …); API calls start failing with `401`/`403`/`404`. Run state machine should: stop issuing new work, persist progress, surface "installation revoked" distinctly from transient errors, and require re-install before resume [event names documented on the webhook events page; run-state mapping is inference].
- Token expiring mid-run: prefer re-minting transparently at `expires_at − margin`; treat a `401` on a token you believe valid as "expired/revoked → re-mint once, then fail" [inference].

### 8.3 Rate limits (installation tokens) — [documented, exact numbers]
| Token type | Primary REST limit |
|---|---|
| Installation token, any org/repo | **min 5,000 req/h** |
| Installation on a GitHub Enterprise Cloud org | **15,000 req/h** |
| Installation with > 20 repos (non-GHEC scaling) | **+50 req/h per repo**; orgs with > 20 users: +50/h per user; **caps at 12,500 req/h** |
| User access token | authenticated user's personal 5,000/h (15,000/h if the app is Enterprise-Cloud-owned and acting for an org member; usage draws down the shared budget) |
| `GITHUB_TOKEN` in Actions | 1,000 req/h per repository (15,000 GHEC) |
| GraphQL | 5,000 points/h; **1 point/query, 5 points/mutation** |

Secondary limits: ≤ **100 concurrent requests**; REST **900 points/min** per endpoint; GraphQL **2,000 points/min**; content creation **80 requests/min** and **500/hour**; token creation **2,000/hour**; CPU-time ≤ 90 s per 60 s of real time.

### 8.4 Retry headers — [documented]
- Primary exceeded: `403` or `429` with `x-ratelimit-remaining: 0` → wait until `x-ratelimit-reset` (UTC epoch seconds).
- Secondary exceeded: `403`/`429` with an explanatory body; if `retry-after` present, wait that many seconds; else wait ≥ 1 minute, then back off exponentially; abort after a bounded number of attempts (best-practices page).
- Monitor `x-ratelimit-limit`, `-remaining`, `-used`, `-reset`, `-resource` on every response. `GET /rate_limit` is cheap (doesn't count against the primary limit).
- Webhook side: respond 2XX within 10 s, enqueue, and rely on delivery listing/redelivery for backfill rather than auto-retry assumptions.

### 8.5 Least-privilege permission table (pipeline baseline) — [inference on composition, documented levels]
| Permission | Level | Why |
|---|---|---|
| Metadata | read | mandatory |
| Issues | write | read issues + post agent updates as comments (write implies read) |
| Pull requests | write | create Draft PRs, read PR/branch state |
| Contents | write | commit files to branches |
| Actions | read | workflow runs/jobs/logs |
| Checks | read | check runs/suites, rollup |
| Workflows | — omit | only if the app must write `.github/workflows/*` |
| Administration | — omit | never needed at runtime; avoids self-modification of protections |
| Commit statuses | — omit | unless emitting legacy status contexts |

### Sources (§8)
- https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- https://docs.github.com/en/webhooks/webhook-events-and-payloads (installation events)
- https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps

---

## 9. REST Pagination & Error Shapes

### 9.1 Pagination — [documented]
- Paginated responses carry a `link` header with `rel="next"`, `rel="prev"`, `rel="first"`, `rel="last"`:
  `link: <https://api.github.com/repositories/1300192/issues?page=2>; rel="prev", <…?page=4>; rel="next", <…?page=53>; rel="last", <…?page=1>; rel="first"`
- Header omitted entirely when everything fits on one page. `rel="last"` may be absent "if it can't be calculated" — **never compute totals from item counts; iterate until no `rel="next"`** [documented guidance].
- `per_page`: default **30**, max **100** for most endpoints; oversized values are **silently clamped** (no error — you may just get fewer results). `per_page` is preserved inside `link` URLs.
- Most endpoints use `page`/`per_page`; some use `before`/`after` or `since` instead. **Cursor-based**: App webhook deliveries use an opaque `cursor` query param (plus `per_page` ≤ 100) [documented on the apps/webhooks page].
- Hard caps to design around: Actions filtered lists return up to **1,000 results per search**; check runs per ref limited to 1,000 most recent suites; repository selection on token mint ≤ 500 repos.
- Octokit: `octokit.paginate()` / `paginate.iterator()` handle Link following.

### 9.2 Error shapes — [documented]
- Primary rate limit: HTTP `403` (or `429`) with body `{"message": "API rate limit exceeded …", "documentation_url": …}` and `x-ratelimit-remaining: 0`.
- Secondary rate limit: `403`/`429`, message names the limit; `retry-after` header when GitHub wants a specific wait; otherwise ≥ 60 s exponential backoff.
- Validation failures: `422` with `{"message": "Validation Failed", "errors": [{"resource": …, "field": …, "code": …}], "documentation_url": …}`.
- Auth: `401` (bad/expired token), `403` (forbidden or secondary limit — check body/headers), `404` (also returned for "exists but no permission" — do not treat as "missing").
- GraphQL: HTTP stays `200`; failures appear in `errors[]` with `type` (e.g. `STALE_DATA`, `UNPROCESSABLE`, `NOT_FOUND`), `path`, `locations`, `message`; partial `data` may coexist with errors.
- Response headers for capability discovery: `X-Accepted-GitHub-Permissions` reports which fine-grained permissions an endpoint requires [documented on the permissions page].

### Sources (§9)
- https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api
- https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- https://docs.github.com/en/rest/apps/webhooks (cursor pagination example)
- https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps

---

## Appendix: Quick endpoint index (pipeline-relevant)

| Purpose | Endpoint / field |
|---|---|
| Verify app | `GET /app` |
| List installations | `GET /app/installations` |
| Mint installation token | `POST /app/installations/{id}/access_tokens` |
| List deliveries / redeliver (App webhook) | `GET /app/hook/deliveries`, `POST /app/hook/deliveries/{delivery_id}/attempts` |
| Commit to branch | GraphQL `createCommitOnBranch` (CAS: `expectedHeadOid`) |
| Create branch | `POST /repos/{owner}/{repo}/git/refs` or GraphQL `createRef` |
| Create Draft PR | `POST /repos/{owner}/{repo}/pulls` (`draft: true`) or GraphQL `createPullRequest` |
| Find PR by branch | `GET /repos/{owner}/{repo}/pulls?head=owner:branch&state=open` |
| PR state | GraphQL `PullRequest { isDraft mergeStateStatus statusCheckRollup reviewDecision headRefOid }` |
| Check runs for SHA | `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` (producer: `app`) |
| Combined status | `GET /repos/{owner}/{repo}/commits/{ref}/status` |
| Workflow runs for SHA | `GET /repos/{owner}/{repo}/actions/runs?head_sha=<sha>` |
| Run attempt / logs | `GET …/actions/runs/{run_id}/attempts/{n}`, `GET …/runs/{run_id}/logs` (302, 1-min URL) |
| Rulesets CRUD | `GET|POST /repos/{owner}/{repo}/rulesets`, `GET|PUT|DELETE …/{ruleset_id}` |
| Effective rules per branch | `GET /repos/{owner}/{repo}/rules/branches/{branch}` |
| Webhook egress IPs | `GET https://api.github.com/meta` → `hooks` |
