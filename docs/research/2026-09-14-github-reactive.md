# GitHub Reactive Bot Surface for forge — Research (2026)

Port target: forge's GitLab reactive core (Codeward lineage) — inline MR review comments with
severity, incremental re-review that resolves old threads, @mention chat assistant on issues/MRs,
pipeline-failure debugging comments, security triage — mapped onto GitHub.

Legend: `[REST]` REST API · `[GraphQL]` GraphQL API v4 · `[WH]` webhook event · `[LIM]` hard limit · `[PAT]` observable bot pattern.

---

## 1. PR reviews by Apps

### 1.1 Creating a review with inline comments `[REST]`

`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`

| Field | Notes |
|---|---|
| `commit_id` | SHA the review applies to. **Defaults to the head commit** when omitted. "Using an older SHA may mark comments outdated" — pass head SHA explicitly so comments anchor to the reviewed commit. |
| `event` | `APPROVE` \| `REQUEST_CHANGES` \| `COMMENT`. **Omitted → review is created PENDING** (draft). Docs: `body` is "Required when using REQUEST_CHANGES or COMMENT". |
| `body` | Review summary (markdown, rendered above the inline comments). |
| `comments[]` | Inline comments, created atomically with the review. |

`comments[]` item fields:

| Field | Semantics (verbatim-derived) |
|---|---|
| `path` | Repo-relative file path. Must be in the PR diff. |
| `line` | "The line of the blob in the pull request diff that the comment applies to. For a multi-line comment, the **last line of the range**." Required unless commenting on a whole file via GraphQL `subjectType`. |
| `side` | `LEFT` (base/deletions) or `RIGHT` (head/additions, unchanged context). For multi-line: side of the **last** line of the range. |
| `start_line` | First line of a multi-line range; "Required when using multi-line comments". Must be ≤ `line`, and **both lines must be part of the diff**. |
| `start_side` | Side of `start_line`: `LEFT`, `RIGHT`, or same as `side`. |
| `position` | **Deprecated.** Diff-relative index counted from the first `@@` hunk header (line below `@@` = 1). Mutually exclusive with `line`. Do not use. |
| `body` | Comment text (markdown). |

- Multi-line rule: `start_line`+`start_side` open the range, `line`+`side` close it. Single-line: only `line`+`side`.
- One review = one atomic request. There is no documented maximum inline-comment count; the practical caps are secondary rate limits (§2) and UI collapse. Single-shot (create + submit in one POST with `event`) is what reviewdog/CodeRabbit-style tools do; two-phase (omit `event` → `POST .../reviews/{review_id}/events` with `{"event":"COMMENT"}`) exists for late assembly.
- **422 "Validation failed, or the endpoint has been spammed."** is dual-purpose: malformed line/path/commit_id *and* secondary rate limiting. Treat any 422 on this endpoint as retry-after-backoff + validation check.
- File-level comments: `comments[]` in the REST create-review body does **not** document `subject_type`. File-level exists on the standalone comment endpoint `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments` with `subject_type: "file"` (then `line` not required), and in GraphQL via `subjectType` on `DraftPullRequestReviewThread`. Community confirmed pure file-level comments are otherwise unsupported (#143197).

### 1.2 Review lifecycle endpoints `[REST]`

- `GET /repos/{o}/{r}/pulls/{n}/reviews` — chronological; each review carries `commit_id`, `user.login`, `state`, `submitted_at`. **This is the incremental-review anchor** (§1.4).
- `PUT .../reviews/{review_id}` — updates **only the review summary `body`** (after submission). Inline comments are immutable via REST.
- `DELETE .../reviews/{review_id}` — **only PENDING reviews**.
- `POST .../reviews/{review_id}/events` — submit a pending review; `event` required (blank → 422).
- `PUT .../reviews/{review_id}/dismissals` — `{"message": "..."}`; needs admin on protected branches.
- Single (non-review) comment: `POST /repos/{o}/{r}/pulls/{n}/comments` with `in_reply_to` — "When specified, all parameters other than body are ignored"; replies-to-replies unsupported; `POST .../comments/{id}/replies` is the same thing for top-level comments only.

### 1.3 Threads: resolve/unresolve `[GraphQL]` — required for incremental re-review

**REST has no thread object** (thread IDs exist only in GraphQL — confirmed in community/Reddit threads). For "re-review resolves old threads", forge must use GraphQL:

- Enumerate threads: `PullRequest.reviewThreads(first: 100)` → `PullRequestReviewThread { id, isResolved, isOutdated, path, line, startLine, diffSide, comments(first: 100) { nodes { id, databaseId, body, outdated, author { login }, replyTo { id }, createdAt } } }`.
- `mutation { resolveReviewThread(input: { threadId: "…" }) { thread { isResolved } } }`
  - `ResolveReviewThreadInput`: `threadId: ID!`, `clientMutationId: String`, **`resolutionReason: PullRequestReviewThreadResolutionReason`** — enum `ADDRESSED` / `WONT_FIX` / `INVALID` ("The reason a **Copilot code review** thread was resolved" — new field, verify behavior for non-Copilot authors).
- `unresolveReviewThread(input: { threadId: ID! })` — same shape, no reason.
- Create thread: `addPullRequestReviewThread(input: { pullRequestReviewId, path, body!, line, side, startLine, startSide, subjectType })`.
- Reply: `addPullRequestReviewThreadReply(input: { body!, pullRequestReviewThreadId!, pullRequestReviewId })`. (`addPullRequestReviewComment` is deprecated/removed since 2023-10-01.)
- One-shot GraphQL review: `addPullRequestReview(input: { pullRequestId: ID!, commitOID, body, event, threads: [DraftPullRequestReviewThread!] })` where `DraftPullRequestReviewThread = { path, body!, line, side, startLine, startSide }`. Follow with `submitPullRequestReview(input: { pullRequestReviewId: ID!, event, body })` if created PENDING. Advantage over REST: supports `subjectType` (FILE) threads and returns thread IDs directly.
- Permissions gotcha: Apps need `pull_requests: write`; community reports `resolveReviewThread` also failing with "Resource not accessible by integration" without **Contents: write** (thread resolution touches the diff) — grant both.

### 1.4 Incremental review pattern

GitLab equivalent is `dif`-since-last-run + resolving old discussions. GitHub mapping:

1. **Detect new commits**: `pull_request` WH, action `synchronize` — payload carries `before` (old head SHA) and `after` (new head SHA). Zero API calls needed to compute the delta. Fallback: `GET /repos/{o}/{r}/pulls/{n}/commits` and diff the SHA list against the stored last-reviewed SHA.
2. **Recall last review**: `GET /repos/{o}/{r}/pulls/{n}/reviews` filtered to `user.login == "<app-slug>[bot]"`; take latest `commit_id`. Also list own still-open threads via GraphQL `reviewThreads` (see §1.3).
3. **Diff delta**: `GET /repos/{o}/{r}/compare/{before}...{after}` → `files[]` (same schema as PR files; beware §2 truncation limits: ~300 files, patches truncated). For cold start (no last review), review the full PR via `GET /repos/{o}/{r}/pulls/{n}/files`.
4. **Post**: one review per push with only new findings (`event: COMMENT`, head SHA as `commit_id`), or REQUEST_CHANGES per severity policy.
5. **Resolve stale**: for each own thread where `isResolved == false && isOutdated == true` → `resolveReviewThread`. GitHub auto-flags `outdated` when a later commit touches the line (also exposed as `comment.outdated` / review-comment REST field). CodeRabbit additionally offers users `@coderabbitai resolve` to bulk-resolve all bot threads — cheap to copy as a forge chat command.

Sources:
- https://docs.github.com/en/rest/pulls/reviews
- https://docs.github.com/en/rest/pulls/comments
- https://docs.github.com/en/graphql/reference/pulls
- https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit
- https://github.com/orgs/community/discussions/44650 (resolveReviewThread permissions), https://github.com/orgs/community/discussions/204269 (Contents requirement), https://github.com/orgs/community/discussions/143197 (no file-level review comments), https://github.com/orgs/community/discussions/65174 (422 thread errors)

---

## 2. Rate & size discipline `[LIM]`

| Constraint | Value | Handling |
|---|---|---|
| Comment/PR body max | **65,536 chars** (all bodies: issue comments, review comments, PR bodies; MySQL mediumblob 262,144 bytes ÷ 4-byte max). Error: `body is too long (maximum is 65536 characters)` | Truncate with `… [truncated]` footer + link to full artifact; or split into "part 2" comment |
| Annotations per check-run request | **50 max** per `output.annotations[]`; each Update **appends** | Batch via `PATCH /repos/{o}/{r}/check-runs/{id}` |
| Annotation `message` / `raw_details` | 64 KB each; `title` ≤ 255 chars | — |
| Actions UI annotation display | 10 warning + 10 error **per step** | Extra ones only visible in check-run summary |
| Check-run `actions[]` buttons | Max **3** (label ≤20 chars, description ≤40) | Use for "forge: retry / explain / fix" |
| REST primary rate | **5,000 req/h per installation token**; `GITHUB_TOKEN` in Actions: **1,000 req/h/repo** | Never use GITHUB_TOKEN for forge; use App installation tokens |
| REST secondary | Content-creation endpoints (comments, reviews, issues): "Creating content too quickly … may result in secondary rate limiting". Best practices: min ~1s between content POSTs, no concurrency | Serialize review-comment POSTs ≥1s apart; jitter + `Retry-After` honoring |
| 422 semantics | `Validation failed, or the endpoint has been spammed` covers both validation AND spam/secondary | Backoff on 422 before concluding it's a payload bug |
| GraphQL | Points = (sum of first/last page requests across connections) ÷ 100, rounded; min 1. 5,000 pts/h per installation (≤12,500 with org/repo bonuses; 10,000/h on GHES/EC orgs). **≤ 500,000 nodes/query** | `reviewThreads(first: 100)` is ~1 pt; cheap |
| PR files API | `GET /repos/{o}/{r}/pulls/{n}/files`: `per_page` ≤ 100, up to 3,000 files; **`patch` field omitted** for binary files and oversized diffs (≥ ~400 lines/file, 64 KB change cap); whole endpoint can 406 ("diff too large", reviewdog outage) or 5xx on huge PRs | Three-tier fallback: (1) `files[].patch`, (2) raw diff `GET /repos/{o}/{r}/pulls/{n}` with `Accept: application/vnd.github.diff`, (3) clone + `git fetch origin refs/pull/{n}/merge` + local diff. Skip `patch`-less files for inline comments; review them repo-side |
| Compare API | `GET /repos/{o}/{r}/compare/{base}...{head}` — files capped (~300), patches truncated same way | For incremental deltas only |

Body-size implementation note for forge: keep a `renderReport()` that hard-caps at ~60,000 chars with a `<details><summary>N findings omitted</summary>…</details>` overflow block, matching what scout-action/changesets hit (Sources below).

Sources:
- https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- https://docs.github.com/en/graphql/overview/rate-limits-and-node-limits-for-the-graphql-api
- https://docs.github.com/en/rest/checks/runs
- https://github.com/orgs/community/discussions/27190 (65536 origin), https://github.com/orgs/community/discussions/41331 (gzipped payload nuance)
- https://github.com/reviewdog/reviewdog/issues/1696 (406 diff too large), https://github.com/orgs/community/discussions/118311 (3000 files), https://docs.github.com/en/rest/pulls/files
- https://github.com/docker/scout-action/issues/66, https://github.com/changesets/action/issues/174 (truncation workarounds)

---

## 3. Issue/PR chat UX for bots

### 3.1 Triggers `[WH]`

- `issue_comment` (`created`/`edited`/`deleted`) — fires for **both issues and PR conversation comments**. Payload: `{ action, issue { number, title, body, user, pull_request { url } }, comment { id, body, user { login, type }, author_association, created_at }, sender, repository }`. Distinguish PR vs issue by presence of `issue.pull_request` (not part of community #24312's complaint only if you know this).
- `issues` (`opened`, `edited`, …) — for @mention-in-body on issues (new issue mentioning forge).
- `pull_request_review_comment` (`created`/`edited`/`deleted`) and `pull_request_review` (`submitted`/`edited`/`dismissed`) — for chat *inside review threads*.
- **Mention detection is DIY**: there is no "bot mentioned" event. Parse `comment.body` for `@forge-app` (case-insensitive, word-boundary). All major agents do exactly this (Claude: `@claude` on `issue_comment`+`pull_request_review_comment`+`pull_request_review`+`issues` events; Codex: `@codex`; CodeRabbit: `@coderabbitai`; Qodo: slash commands `/review` `/improve` `/ask`). Recommend forge: `@forge <free text>` plus leading-slash aliases (`/forge review`) since slash commands are the other established convention.
- Conversation threading: issue comments are **flat — no native threads** (threads exist only for review comments via `in_reply_to` / GraphQL `addPullRequestReviewThreadReply`). For multi-turn issue chat, keep state server-side keyed by `issue.id`, and make bot replies quote-anchor (`> @user asked: …`) — what Qodo's `/ask` output does.

### 3.2 Reactions as acks `[REST]` `[PAT]`

- `POST /repos/{o}/{r}/issues/comments/{comment_id}/reactions {"content":"eyes"}` — idempotent (returns 200 if already exists). Content enum: `+1, -1, laugh, confused, heart, hooray, rocket, eyes`. Same shape for issues (`/issues/{n}/reactions`), PRs (`/pulls/{n}/reactions`), review comments (`/pulls/comments/{id}/reactions`).
- Observable convention: **👀 on receipt** (Claude app, Codex app — `openai/codex#30858` shows 👀 as the "started working" signal), **🚀 on completion/ship**. Cheap, notification-free. forge: react `eyes` at task start; react `rocket` (or remove `eyes`) on completion.

### 3.3 Editing bot comments vs new comments `[REST]` `[PAT]`

- `PATCH /repos/{o}/{r}/issues/comments/{comment_id}` (and `PATCH /repos/{o}/{r}/pulls/comments/{comment_id}` for review comments) — full body replace, markdown.
- What major bots do (observable):
  - **Claude / Codex**: one progress comment, edited in place repeatedly while the agent works (live-updating), final state = result. New comments only for genuinely new turns.
  - **CodeRabbit**: a single PR "walkthrough/summary" comment **edited on every push** (`synchronize`) instead of a new comment per commit; new comments only for new inline findings.
  - **Qodo**: edits its existing "PR Improve" suggestions comment on incremental runs; skips the LLM entirely if no new commits (docs: "later run with no new changes exits without calling the AI").
  - **github-actions "sticky comment" pattern** (peter-evans/create-or-update-comment lineage): find bot's previous comment by hidden HTML marker → `PATCH` instead of POST.
- forge convention to adopt: every persistent bot comment embeds a machine-readable marker `<!-- forge:<kind>:<key> -->` (e.g. `forge:walkthrough:42`, `forge:ci-debug:<sha>`). On any event: `GET /repos/{o}/{r}/issues/{n}/comments?per_page=100`, match marker + `user.login == forge-app[bot]`, then PATCH; else POST. Add hidden marker only, no visible duplication.
- Long work (chat tasks): immediately POST `<!-- forge:task:<id> --> ⏳ forge is working on this…` then PATCH at intervals (throttle ≥ 10–15s to stay under abuse heuristics) with step/elapsed updates; finish with result + `🚀`. Alternative for review-type work: create a **check run** `forge/review` with `status: in_progress` → `completed` (surfaces on the PR merge box; §4), and link to the comment.

Sources:
- https://docs.github.com/en/webhooks/webhook-events-and-payloads (issue_comment/issues/pull_request_review_comment payloads)
- https://docs.github.com/en/rest/reactions/reactions
- https://code.claude.com/docs/en/github-actions
- https://learn.chatgpt.com/docs/third-party/github (Codex @codex semantics), https://github.com/openai/codex/issues/30858 (👀 ack)
- https://docs.coderabbit.ai/guides/commands, https://docs.coderabbit.ai/reference/glossary (incremental)
- https://docs.pr-agent.ai/tools/improve/ (incremental skip-when-no-new-changes)
- https://github.com/orgs/community/discussions/24312 (payload limitations)

---

## 4. Actions failure debugging

### 4.1 Trigger + PR correlation `[WH]` `[REST]`

- Preferred events (fire even for users who don't install a workflow file — they're App-level webhooks):
  - `workflow_job` (`queued`/`in_progress`/`completed`): payload `workflow_job { id, run_id, head_sha, head_branch, status, conclusion, steps[] { name, status, conclusion, number }, html_url }` — **per-step failure granularity, best for root-cause scoping**.
  - `check_suite` / `check_run` (`completed`, `conclusion: failure`): `check_suite { head_sha, conclusion, pull_requests[] }`.
  - `workflow_run` (`requested`/`completed`): `workflow_run { id, name, head_branch, head_sha, status, conclusion (success|failure|cancelled|neutral|skipped|timed_out|action_required|startup_failure), pull_requests[] }`.
- **Fork caveat**: for fork-PR-triggered runs, `workflow_run.pull_requests` is `[]` and `head_branch` may be `null` (security restriction). **Universal correlation key = `head_sha`**:
  - `GET /repos/{o}/{r}/commits/{sha}/pulls` — "Lists the merged pull request that introduced the commit… If the commit is not present in the default branch, it will return merged and **open** pull requests associated with the commit" (branch name also accepted as `{sha}`). This is the one-call PR association for any run.
  - Then comment on the PR: `POST /repos/{o}/{r}/issues/{pr_number}/comments` (PR comments = issue comments).

### 4.2 Log retrieval for the debugging agent `[REST]`

- `GET /repos/{o}/{r}/actions/runs/{run_id}/jobs` — jobs + `steps[]` with `conclusion` per step → find first failing step.
- `GET /repos/{o}/{r}/actions/jobs/{job_id}/logs` → **302 redirect to plain-text log** (follow with `Location`).
- `GET /repos/{o}/{r}/actions/runs/{run_id}/logs` → **302 to a signed ZIP** of all logs (URL valid ~60s).
- Permission: `actions: read`. Feed only the failing step's log slice (tail N KB) to the agent, not whole-run ZIPs.

### 4.3 Surface the diagnosis: annotations vs comments `[REST]`

- **Check-run annotations** (rendered inline on the PR *Files changed* view): forge can create its own check run
  `POST /repos/{o}/{r}/check-runs` (name e.g. `forge/ci-diagnosis`, `head_sha` = failed run's sha, `status: completed`, `conclusion: failure` or `neutral`) with
  `output { title, summary (markdown), text, annotations: [{ path, start_line, end_line, start_column?, end_column?, annotation_level: notice|warning|failure, message, title?, raw_details? }] }`.
  50 annotations/request, appends via `PATCH /repos/{o}/{r}/check-runs/{id}`; max 3 `actions[]` buttons (label ≤20 chars) — GitHub delivers `check_run.requested_action` WH when clicked (use for "forge: re-run failing job" → `POST /repos/{o}/{r}/actions/runs/{run_id}/rerun-failed-jobs`). Only GitHub Apps can create check runs — another reason forge must be an App.
- **PR comment** with root cause (what users actually read): sticky-comment pattern `<!-- forge:ci-debug:{head_sha} -->` + summary table (job → failing step → cause → suggested fix → link to `workflow_job.html_url`). Edit on retry rather than re-post (§3.3). This mirrors what CI-report bots (e.g. test-reporter) and agent CI-debuggers do.
- Recommendation: annotations for *code-line* findings surfaced by CI; comment for the *run-level* narrative. Both keyed by `head_sha`.

Sources:
- https://docs.github.com/en/webhooks/webhook-events-and-payloads (workflow_run/check_run/workflow_job payloads; fork `pull_requests: []` note)
- https://docs.github.com/en/rest/actions/workflow-runs#download-workflow-run-logs
- https://docs.github.com/en/rest/actions/workflow-jobs (Get a job / Download job logs, 302 semantics)
- https://docs.github.com/en/rest/checks/runs (annotations, actions[])
- https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit
- https://github.com/orgs/community/discussions/170143, https://github.com/orgs/community/discussions/66784 (fork correlation)

---

## 5. Code scanning / security triage UX

### 5.1 Reading alerts `[REST]`

- `GET /repos/{o}/{r}/code-scanning/alerts` (filters: `state=open|dismissed|fixed`, `severity`, `rule_id`, `ref`) / `GET .../alerts/{alert_number}`.
- Key fields: `number`, `state` (`open|dismissed|fixed`), `rule { id, severity (error|warning|note), security_severity_level (critical|high|medium|low), description }`, `most_recent_instance { path, start_line, end_line, message.text, state }`, `html_url`, `created_at`, `dismissed_reason`.
- Alert↔diff location: `GET .../alerts/{alert_number}/instances` (per-`ref` occurrences) for where an alert manifests (branch/PR head).
- PR-context alerts are also surfaced as check-run annotations on the PR (Code scanning / Copilot checks) — same annotation UI as §4.3.
- Same-family triage APIs for a complete security lane: Dependabot `GET /repos/{o}/{r}/dependabot/alerts` (repo, `security_advisory.severity`), Secret scanning `GET /repos/{o}/{r}/secret-scanning/alerts`.

### 5.2 Dismissing / resolving

- `PATCH /repos/{o}/{r}/code-scanning/alerts/{alert_number}` with `{ "state": "dismissed", "dismissed_reason": "…", "dismissed_comment": "…" }`.
- `dismissed_reason` enum (verbatim): `false positive`, `won't fix`, `used in tests`, `mitigated`. `dismissed_comment` is the free-text justification. `dismissed_by`/`dismissed_at` come back on the alert.
- There is **no comment API on code scanning alerts themselves**; the UX for "explain/annotate an alert" is either the PR timeline or a linked issue (GitHub's own "Create issue from alert" flow creates an issue whose body embeds `alert.html_url`).

### 5.3 Alert → fix PR linking (Copilot autofix pattern)

- Copilot Autofix REST (Public preview Dec 2024, GHAS):
  - `POST /repos/{o}/{r}/code-scanning/alerts/{alert_number}/autofix` — request generation. **202** = generating; **200** = already exists. Default branch alerts only.
  - `GET .../autofix` — `{ status: pending|error|success|outdated, description, started_at }` (description = the fix narrative).
  - `POST .../autofix/commits` — commit the generated fix (201).
- Newer agentic path (changelog Oct 2025): **assign alerts to Copilot** (one or more) → Copilot opens an **agentic draft fix PR** and iterates; the alert page shows the linked PR. Operationally the alert↔PR linkage is UI-side; the PR references the alert.
- forge pattern (port of Codeward security triage): severity routing — `critical|high` → triage comment on the affected PR + attempt autofix/agent fix-PR whose body cites `alert.html_url` and `rule.security_severity_level`, and whose head, once merged to default, flips the alert to `fixed`; `medium|low` → batch digest comment on a tracking issue. Dismissals only with explicit human command (`/forge dismiss wont-fix: <alert#>`), executed via the PATCH above with `dismissed_comment` = link to the human who approved.

Sources:
- https://docs.github.com/en/rest/code-scanning/code-scanning (alerts, PATCH alert + dismissed_reason enum, autofix GET/POST/commits)
- https://github.blog/changelog/2024-12-16-copilot-autofix-can-now-be-generated-with-the-rest-api-public-preview/
- https://github.blog/changelog/2025-10-28-assign-code-scanning-alerts-to-copilot-for-automated-fixes-in-public-preview/
- https://docs.github.com/en/code-security/how-tos/manage-security-alerts/manage-code-scanning-alerts/resolve-alerts
- https://docs.github.com/en/code-security/secure-coding/triaging-code-scanning-alerts-in-pull-requests

---

## 6. Identity & trust

### 6.1 forge as GitHub App (not user bot)

- Decide **GitHub App**, hard requirement for: check runs (`POST /check-runs` is Apps-only), fine-grained per-repo permissions (pull_requests: write, contents: read, issues: write, actions: read, checks: write, security_events for code scanning), short-lived installation tokens (no PAT lifecycle), and the **5,000 req/h per-installation** REST budget vs 1,000/h GITHUB_TOKEN.
- Visual identity is automatic: comments/PRs authored via the App's installation token render as `forge-app[bot]` with the App avatar + "App" badge; GraphQL `author { __typename: Bot }`, REST `user.type: "Bot"`. No extra markup needed for distinctness; keep a visible header line anyway (`**forge** · automated review`) like CodeRabbit does.
- `author_association` for App-authored comments is `NONE` — don't gate logic on it; gate on `user.type`.

### 6.2 Recursion / self-trigger avoidance

- Every webhook handler first-line guard: skip when `payload.sender.type === "Bot"` — and more precisely `sender.login === "<app-slug>[bot]"`. Apply to `issue_comment`, `issues` (bot-opened issues), `pull_request` (`opened`/`synchronize` on **bot-authored PRs**: `pull_request.user.type === "Bot"`), `pull_request_review(_comment)`.
- `GITHUB_TOKEN`-triggered events never re-trigger workflow runs (platform recursion guard, discussion #55906), but **App-token events DO fire webhooks** — the platform will not save forge; the guard must be in forge.
- Security rule (chained-bot attacks, what Cursor/others filter): don't let bot-authored comments command forge even with `@forge` mention — default allowlist humans; optionally allow Bot senders on an explicit allowlist.
- CodeRabbit-style escape hatch: a repo-config file (`.forge.yml`) controlling auto-review, mention aliases, and which bot authors are ignored.

### 6.3 Content attachments — not relevant

The 2018 Content Attachments API (App-attached rich cards on URLs) is a dormant beta with a 6-hour creation window; no modern bot uses it. Skip; use markdown links, tables, `<details>`, and check-run `images[]` instead.

Sources:
- https://docs.github.com/en/apps/creating-github-apps (identity/permissions), https://docs.github.com/en/rest/apps (installation tokens)
- https://github.com/orgs/community/discussions/55906 (GITHUB_TOKEN no-retrigger)
- https://github.com/The-PR-Agent/pr-agent/issues/2398 (sender.type skip), bot-loop patterns per §3 sources
- https://github.blog/changelog/2018-12-09-content-attachments-api-public-beta/, https://developer.github.com/changes/5/

---

## 7. Prior art — observable bot patterns

| Bot | Trigger UX | Ack & progress | Output shape | Incremental |
|---|---|---|---|---|
| **OpenAI Codex** (`codex[bot]`) | `@codex <task>` in PR/issue comments; special-cased `@codex review` (docs: "anything other than review" starts a cloud chat/task with PR as context) | 👀 reaction while working (observed in openai/codex#30858) | Single PR comment thread; review findings via PR review; task results as new PR or comment | Re-review on new mention; cloud task reuses PR context |
| **Claude Code GitHub app** (`claude[bot]`) | `@claude` in issues, PR comments, review comments (docs list triggers: `issue_comment`, `pull_request_review_comment`, `pull_request_review`, `issues`) | 👀 ack on receipt; then live progress via **editing its comment in place** | Result comment (can open PR from issue); supports custom trigger phrases & prompts via repo workflow file | Stateless per-mention; users re-mention to iterate |
| **CodeRabbit** (`coderabbitai[bot]`) | Auto on `opened`/`synchronize`/`ready_for_review`; commands `@coderabbitai review` (manual incremental), `@coderabbitai full`, `@coderabbitai resolve`, `@coderabbitai plan`, `@coderabbitai summary` | Posts review when ready; no ack reaction; "pausing" after N commits (auto-review controls) | One sticky **walkthrough** summary comment (edited every push) + inline review comments with severity styling and `suggestion` fenced blocks; resolves its own outdated comments on pushes | Core feature ("incremental reviews" in glossary: re-review only fresh commits of an already-reviewed PR); inline replies command fixes scoped to that thread |
| **Greptile** (`greptile-app[bot]`) | Auto on PR open/push; config via `greptile.json` (trigger filters, severity threshold, effort) | None (posts when done) | Single summary comment including **"Review effort: N/5"** + inline line comments with confidence/severity; 👍/👎 on its comments feed its learning loop | Reviews only new commits on push; summarizing + effort estimate per PR |
| **Qodo (PR-Agent/Qodo Merge)** | Slash commands in comments: `/review`, `/improve`, `/ask "<q>"`, `/describe`, `/update_changelog`; also auto mode | Edits its existing suggestions comment on re-runs | "PR Review"/"PR Improve" table-style comments: per-file analysis, collapsible sections, `suggestion` fenced blocks for one-click Apply | Documented incremental `/improve`: first run analyzes full PR; later runs analyze only new commits; **exits without LLM call if no new changes** (dedupe by comparing commit list) |

Cross-cutting conventions to copy verbatim into forge:
1. **One sticky canonical comment per concern**, edited in place, marked with hidden HTML comment (§3.3).
2. **👀 then 🚀** reactions as lifecycle acks.
3. Inline findings always as **PR review comments** (not plain comments), severity in the first line, ` ```suggestion ` blocks when a replacement exists (GitHub renders "Apply suggestion" for exact-range suggestions).
4. Commands: `@forge <text>` plus `/forge …` aliases; a `resolve`-all command; per-thread reply commands.
5. Review summaries carry effort/severity stats; incremental posts only deltas + a "reviewed <sha1>..<sha2>" line.

Sources:
- https://learn.chatgpt.com/docs/third-party/github, https://github.com/openai/codex/issues/30858
- https://code.claude.com/docs/en/github-actions, https://github.com/anthropics/claude-code-action (issues #1044, #591 — trigger surface details)
- https://docs.coderabbit.ai/guides/commands, https://docs.coderabbit.ai/reference/review-commands, https://docs.coderabbit.ai/configuration/auto-review, https://docs.coderabbit.ai/reference/glossary
- https://docs.pr-agent.ai/tools/improve/, https://github.com/The-PR-Agent/pr-agent
- https://www.greptile.com/docs/introduction, https://www.greptile.com/docs/code-review/greptile-json-reference

---

## 8. Recommended forge-on-GitHub reactive feature set + phased port plan

Feature → exact API calls (App = GitHub App `forge-app`; all calls use installation tokens):

**F1 — PR review engine** (port of GitLab MR inline review + incremental)
- Trigger: `pull_request` WH `[opened, synchronize, ready_for_review]` (skip `sender.type == Bot`, skip `pull_request.user.type == Bot` unless mentioned).
- Fetch delta: synchronize → `before`/`after` SHAs from payload; else last own review via `GET /repos/{o}/{r}/pulls/{n}/reviews` (filter `user.login == forge-app[bot]`, take `commit_id`) + `GET /repos/{o}/{r}/pulls/{n}/commits`; diff via `GET /repos/{o}/{r}/compare/{base}...{head}` or `GET /repos/{o}/{r}/pulls/{n}/files` (fallback chain §2).
- Post: `POST /repos/{o}/{r}/pulls/{n}/reviews` with `commit_id = head`, `event: COMMENT|REQUEST_CHANGES` by severity policy, `comments[]` = `{ path, side, line, start_line, start_side, body }` (severity badge line 1, `suggestion` blocks where applicable), serialized ≥1s apart; body hard-capped <65,536 chars.
- Incremental resolve: GraphQL `pullRequest.reviewThreads` → for own threads `isOutdated && !isResolved` → `resolveReviewThread(input: { threadId })`.
- Optionally create the review via GraphQL `addPullRequestReview` + `threads: [DraftPullRequestReviewThread]` + `submitPullRequestReview` to get thread IDs in one round trip.

**F2 — @mention chat assistant** (port of issue/MR assistant)
- Trigger: `issue_comment.created` (+ `pull_request_review_comment.created`, `issues.opened` when body mentions forge).
- Guard: `sender.type != Bot`; parse `@forge` / `/forge`.
- Ack: `POST /repos/{o}/{r}/issues/comments/{id}/reactions {"content":"eyes"}`.
- Progress: `POST /repos/{o}/{r}/issues/{n}/comments` with `<!-- forge:task:<id> -->` placeholder → repeated `PATCH /repos/{o}/{r}/issues/comments/{comment_id}` (throttled).
- Finish: final PATCH with result; `POST .../issues/{n}/comments/{comment_id}/reactions {"content":"rocket"}`; remove ack reaction if desired (`DELETE .../reactions/{reaction_id}`).
- State: conversation history keyed by `issue.id` + marker thread; `resolve`-all command maps to `resolveReviewThread` per open own thread.

**F3 — Pipeline-failure debugging** (port of pipeline-failure comments)
- Trigger: `workflow_job` WH `completed` with `conclusion == failure` (primary; per-step data). Secondary: `check_suite.completed`.
- Correlate: `workflow_job.head_sha` → `GET /repos/{o}/{r}/commits/{sha}/pulls` → PR number.
- Analyze: `GET /repos/{o}/{r}/actions/runs/{run_id}/jobs` (failing step) → `GET /repos/{o}/{r}/actions/jobs/{job_id}/logs` (302, tail slice) → agent diagnosis.
- Surface: sticky PR comment `<!-- forge:ci-debug:{sha} -->` via `POST/ PATCH /repos/{o}/{r}/issues/{n}/comments`; plus a forge check run `POST /repos/{o}/{r}/check-runs` (name `forge/ci-diagnosis`, `head_sha`, `output.annotations[]` ≤50/batch) for line-level findings; `actions[]` button "Retry failed jobs" → `POST /repos/{o}/{r}/actions/runs/{run_id}/rerun-failed-jobs` via `check_run.requested_action`.

**F4 — Security triage** (port of Codeward security triage)
- Trigger: poll on `code_scanning_alert.created` WH (or scheduled sweep) `GET /repos/{o}/{r}/code-scanning/alerts?state=open`.
- Route by `rule.security_severity_level`: critical/high → PR-context comment (find PR via `instances` ref → head SHA → `commits/{sha}/pulls`) + optional fix; medium/low → tracking-issue digest.
- Fix loop: forge fix PR whose body cites `alert.html_url` + severity; mirror Copilot autofix shape (`POST .../autofix` equivalents are GHAS-only — implement forge's own fixer).
- Dismissal: human command only → `PATCH .../alerts/{n} { state: "dismissed", dismissed_reason, dismissed_comment }`.
- Later: dependabot + secret-scanning alert APIs (same shape of lane).

### Phased port plan (Codeward-style GitLab core → GitHub)

- **Phase 0 — App chassis** (prereq for everything): GitHub App registration (permissions: pull_requests rw, contents r, issues rw, checks rw, actions r, security_events r), webhook receiver with `X-Hub-Signature-256` validation, installation-token cache, event de-dup (`delivery.id`), identity guard (`sender.type`), rate limiter (1s/content POST, 422 backoff), markdown renderer with 65,536-char truncation + marker management (sticky-comment helper). No user-visible features yet.
- **Phase 1 — F1 review engine**: highest parity with the GitLab core's flagship feature; pure PR surface, no CI dependency. Ship: full + incremental review, severity model, outdated-thread resolution, `.forge.yml` config.
- **Phase 2 — F2 chat**: reuses Phase 0 chassis + Phase 1's PR context builders; adds issue surface, reactions, live-progress comments, `/forge resolve`.
- **Phase 3 — F3 pipeline debug**: adds actions: read + checks: write; depends on F2's sticky-comment infra for the ci-debug comment.
- **Phase 4 — F4 security triage**: adds security_events scope; severities route through F1 (fix PRs get reviewed by F1 automatically) and F2 (triage chat).
- Cross-cutting: recursion guards from §6.2 in every phase; GraphQL only where REST can't (threads); check runs only from the App identity.

*Researched 2026-09-14 against current docs.github.com; GraphQL input shapes introspected live from the API schema (ResolveReviewThreadInput incl. `resolutionReason`, DraftPullRequestReviewThread).*
