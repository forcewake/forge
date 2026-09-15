# Stage AZ: Azure DevOps adapter — solution design & brief

Status: in progress (2026-09-15) · Implements [ADR-0024](../adr/0024-azure-devops-adapter.md) · Research: [research/azure-devops.md](../research/azure-devops.md)

## 0. Scope & success bar

Provider id `azure_devops`, peer of `gitlab` and `github`. Success bar =
the GitHub slice's bar, proven live once a PAT + organization exist:
`/implement` on a work item → plan comment → `/go` → lane pipeline in
Azure Pipelines → candidate artifact → trusted publisher (CAS) → Draft PR
→ PR CI via branch policy → reactive review on push → `ready_for_human`;
`/cancel` = revoke; build failure → `debug_ci` comment; `forge doctor`
green. Everything unit/contract-tested without Azure (recorded payload
shapes), live verification gated on the user's credentials.

## 1. API ground truths (verified 2026-09 against learn.microsoft.com)

| Concern | Ground truth |
|---|---|
| Base URL | Services: `https://dev.azure.com/{org}`; Server: `https://{instance}/{collection}`. Project-scoped resources: `/{org}/{project}/_apis/...`. `api-version=7.1` query param (preview stripes like `7.1-preview.4` for comments). |
| Auth | `Authorization: Basic base64(":" + PAT)` — empty username, colon prefix. Scopes: `vso.code`, `vso.code_write` (branches/PRs/threads mgmt), `vso.build`, `vso.build_execute`, `vso.work`, `vso.work_write`, `vso.threads_full` (PR threads R/W). PAT carries its owner's permissions; use a dedicated service account. |
| Webhook envelope | `{ "eventType": "git.push", "publisherId": "tfs", "resource": {...}, "resourceContainers": {...}, "messages"/"detailedMessage": {text,html,markdown}, "createdDate": ... }`. Consumer `webHooks` / action `httpRequest`; **no HMAC** — auth = Basic username/password (HTTPS required) and/or custom headers. Subscriptions: `POST /{org}/_apis/hooks/subscriptions?api-version=7.1` with `{publisherId:"tfs", eventType, resourceVersion:"1.0", consumerId:"webHooks", consumerActionId:"httpRequest", publisherInputs:{...filters}, consumerInputs:{url, basicAuthUsername, basicAuthPassword, httpHeaders, resourceDetailsToSend:"all"}}`. |
| Events consumed | `git.push`; `git.pullrequest.created`; `git.pullrequest.updated` (new commits, votes, status); `git.pullrequest.commented-on` (event id `ms.vss-code.git-pullrequest-comment-event`); `build.complete`; `workitem.commented` (event id `ms.vss-work.workitem-commented-event`). |
| Git CAS | `POST /{project}/_apis/git/repositories/{repoId}/pushes?api-version=7.1` — body `{refUpdates:[{name:"refs/heads/x", oldObjectId}], commits:[{comment, changes:[{changeType: add|edit|delete, item:{path}, newContent:{content, contentType: rawtext\|base64encoded}}]}]}`. `oldObjectId` = expected parent (CAS); branch creation = `oldObjectId: "0000000000000000000000000000000000000000"`. Failure taxonomy `GitRefUpdateStatus`: `staleObjectId`, `forcePushRequired`, `createBranchPermissionRequired`, … → typed drift errors. `GET /refs?filter=heads/{branch}` reads branch heads. |
| Draft PR | `POST .../pullrequests?api-version=7.1` `{sourceRefName, targetRefName, title, description, isDraft:true, workItems:[{id}]}` (work-item linking optional). PR object carries `pullRequestId`, `status`, and `lastMergeCommit` as a PLAIN GitCommitRef (`commitId` only — the `{sourceCommit,targetCommit,commonCommit}` triple from the brief draft does NOT exist there). The three-way SHAs live on `GitPullRequestIteration` (`sourceRefCommit`/`targetRefCommit`/`commonRefCommit`, `GET .../pullRequests/{id}/iterations`) — the reactive review's before/after MUST go through iterations. |
| PR comments | Threads: `POST .../pullRequests/{id}/threads` `{comments:[{parentCommentId:0, content, commentType:"text"}], status:"active", threadContext:{filePath, rightFileStart:{line,offset}, rightFileEnd:{...}}, pullRequestThreadContext:{changeTrackingId, iterationContext:{firstComparingIteration, secondComparingIteration}}}`; reply `POST .../threads/{threadId}/comments`; sticky progress = update thread status (`Update Thread`: `active`/`fixed`/`wontFix`/`closed`). List: `GET .../threads`. |
| Plan comments | Work items: `POST /{project}/_apis/wit/workItems/{id}/comments?api-version=7.1-preview.4&format=markdown` `{text}` (vso.work_write). Read: `GET .../comments` (batch). |
| Pipelines dispatch | `POST /{project}/_apis/pipelines/{pipelineId}/runs?api-version=7.1` `{resources:{repositories:{self:{refName}}}, templateParameters:{run_id, attempt_base, driver, model, work_item_id}, variables:{...}}` → response carries the **run id** (`{id, state, result, url, web}`) — correlate directly. Poll `GET .../runs/{runId}`: `state: inProgress|completed`, `result: succeeded|failed|canceled|succeededWithIssues`. `previewRun: true` returns the compiled YAML (`finalYaml`) — useful for lane verification. Legacy builds API alternative: `POST /build/builds` with `definition.id` + `sourceBranch` + `templateParameters`. |
| CI ↔ commit correlation | **`builds` has NO `sourceVersion` query param** (response field only): correlate via the `build.complete` webhook (carries `sourceVersion`) or `GET /build/builds?repositoryId=…&definitions=…&minFinishTime=…` + client-side match. runId == buildId [community]. Cancel: the Runs area has NO cancel — `PATCH /build/builds/{id}` `{"status":"cancelling"}`, poll to `result:canceled` (queued jobs may survive). Per-task granularity: `GET .../build/builds/{buildId}/timeline` (records: result `failed`, log ids) → `GET .../builds/{buildId}/logs/{logId}`. Artifacts: `GET /pipelines/{runId}/artifacts?$expand=signedContent` → expiring signed URL. (branch policies' "Build validation" decides PR mergeability — YAML `pr:` triggers are IGNORED for Azure Repos) |
| Lane checkout | `checkout: self` then explicit `git fetch origin "$ATTEMPT_BASE_REF" && git checkout --detach "$ATTEMPT_BASE"` (YAML cannot pin a commit SHA in `resources...refName`); `persistCredentials: false` (default — the OAuth token is NOT left in git config); system access token NOT used (`System.AccessToken` never referenced); artifact: `publish: PipelineArtifact` task → `targetPath: .forge/candidate.diff` etc. `download: current` on later jobs. Agent: self-hosted docker/container jobs mirror the ADR-0002 execution profile; lane env vars = pipeline secret variables (`$(_SECRET)` macro) mapped into `env:`. |
| Errors | `{"$id":"1","message":"TF400813: ..."}` / `TF401027`-style codes; 203 Non-Authoritative on some PAT-scope problems; 401 on bad Basic. |

## 2. Module design

```
src/forge/integrations/azure.py      AzureDevOpsClient (httpx, ADR-0014 pattern)
                                     - _request: Basic auth, api-version injection, TF-error typing
                                     - get_project/get_repo/get_refs/get_item (read surface)
                                     - create_branch_from / push_commits (CAS, typed drift)
                                     - create_draft_pr / get_pr / list_pr_threads /
                                       create_pr_thread / reply_pr_thread / update_thread_status
                                     - add_work_item_comment / get_work_item / get_work_item_comments
                                     - create_hook_subscription / list_hook_subscriptions (onboarding)
                                     - run_pipeline / get_run / get_builds_by_source_version /
                                       get_timeline / get_task_log / get_run_artifact
                                     AzureRepositoryReader (AuthoritativeReader semantics:
                                     get_file/get_tree/get_blob at SHA, default branch)
src/forge/gateway/azure_webhook.py   azure_router: POST /webhook/azure_devops
                                     - fail-closed: FORGE_AZDO_ENABLED + FORGE_AZDO_WEBHOOK_USERNAME/PASSWORD
                                       (constant-time Basic check); 503 disabled / 401 bad creds
                                     - capture raw payload (FORGE_CAPTURE_DIR, header_field="x_ado_event")
                                     - normalize per event → run_command | review_pr | debug_ci |
                                       inbox-only; content-stable source_event_id; ONE transaction
                                       (inbox row + first scheduled step) before the 202; wake stamps
                                       the actual inbox identity (the 64243ff rule)
src/forge/runs/azure_service.py      AzureRunService: start_run (work item → plan comment +
                                     RunSpec freeze + pending decision) / handle_go / cancel-as-revoke;
                                     one active run per (project, work item) — reuses the partial index
                                     semantics via provider column; harness dispatch when a pipeline
                                     is configured, builtin otherwise (GitHubRunService shape)
src/forge/execution/azure_pipelines.py AzurePipelinesExecutor: launch (Runs API, returns run id)
                                     / poll (state/result + artifacts via API) / cancel
                                     (Runs → build cancel) / reconcile_launch (Server fallback:
                                     discover by templateParameters.run_id + created-window)
src/forge/reactive/azure_review.py   reactive review engine over PR threads (GitHubReview shape:
                                     sticky thread via status updates, incremental via
                                     lastMergeCommit before/after, severity→thread status)
src/forge/reactive/azure_ci_debug.py build.complete failure → timeline + task logs → sticky comment
ci/templates/forge-lane.azure-pipelines.yml  proposal-only lane (dispatch-only via Runs API:
                                     no CI/PR triggers at all; parameters: run_id, attempt_base,
                                     driver, model, work_item_id; checkout detached at attempt base,
                                     persistCredentials false; driver step = pip install forge@
                                     pinned ref + python -m forge.harness_entry (same contract as
                                     the Actions lane); publish .forge/candidate.diff + meta,
                                     condition: always())
docs/azure-setup.md                  PAT scopes, service account, service-hooks provisioning
                                     (via forge or by hand), pipeline creation pointing at the lane
                                     yaml, variable groups for harness keys, branch policy
                                     (Build validation) for the quality contract, doctor gates
```

Config (Settings): `FORGE_AZDO_ENABLED=False`, `FORGE_AZDO_ORG_URL`,
`FORGE_AZDO_PAT: SecretStr|None`, `FORGE_AZDO_WEBHOOK_USERNAME`,
`FORGE_AZDO_WEBHOOK_PASSWORD: SecretStr|None`,
`FORGE_AZDO_APPROVERS=""`, `FORGE_AZDO_BOT_NAME="forge-bot"`,
`FORGE_AZDO_LANE_PIPELINE_ID: int|None` (dispatch target, per onboarding).

## 3. Normalization table (ingress → commands)

| Event | Guard | Command |
|---|---|---|
| `workitem.commented` | payload has NO comment id — text = `fields["System.History"]`, author = `System.ChangedBy`; delivery key `workitem:{id}:comment:{rev}`; bot-loop by identity | `/implement`/`/go`/`/cancel`/`/security` → `start_run`/`go`/`cancel`/`security_triage` (same parser, provider `azure_devops`) |
| `git.pullrequest.commented-on` | bot-loop + forge/* branch guards | same command set on PRs |
| `git.pullrequest.created/updated` | Bot sender, `forge/*` head branch | `review_pr` (reactive lane; incremental via `lastMergeCommit`) |
| `build.complete` (`result=failed`) | forge's own lane runs skipped (`FORGE_AZDO_LANE_PIPELINE_ID`) | `debug_ci` (fork-safe: correlate `sourceVersion` → open PR) |
| `git.push`, everything else | — | inbox-only |

Delivery-key conventions: `pr:{id}:comment:{commentId}` (PR comment events DO carry the comment),
`pr:{id}:{event}:{afterSha}`, `build:{buildId}:{result}`,
`workitem:{id}:comment:{rev}` — re-deliveries collapse on the inbox unique
index. Route on `eventType` only: `publisherId` is unstable across API
versions (`tfs` AND `azure-devops` appear in documented samples). Votes
(numeric, for read-only display): approved=10, approvedWithSuggestions=5,
none=0, waitingForAuthor=-5, rejected=-10. Thread creates take NUMERIC
enums (`status:1`, `commentType:1`) and return strings; thread statuses
also include `byDesign`. Policy type ids [documented]: build-validation
`0609b952-1397-4640-95ec-e00a01b2c241`, min-reviewers
`fa4e907d-c16b-4a4c-9dfa-4906e5d171dd`; policy evaluations use artifactId
`vstfs:///CodeReview/CodeReviewId/{projectId}/{prId}`. Pipeline creation
pointing at an existing yaml path works but is PREVIEW-only
(`7.2-preview.1`) — onboarding may prefer manual creation + doctor check.
WIT comments `7.1-preview.4` is a preview stripe (preview stripes are
rejected ~12 weeks post-GA — pin GA stripes where they exist).

## 4. Milestones (sub-agent slices)

- **AZ-1 (client)** — `integrations/azure.py` + `AzureRepositoryReader` +
  contract tests against recorded payload shapes (fixtures from the
  research doc; no network in tests). Gate: mypy clean, ≥40 new tests.
- **AZ-2 (ingress + run service)** — webhook route, normalization table,
  `AzureRunService` (plan/gate/cancel/one-active-run), admission via
  `FORGE_AZDO_APPROVERS`, execute_run_command dispatch, Settings, tests
  (ingress matrix incl. bad creds/bot-loop/redelivery). Gate: full suite
  green.
- **AZ-3 (execution + lanes + reactive)** — executor, lane template,
  harness dispatch in `/go`, waiting_harness reconcile, reactive review +
  debug lanes, tests. Gate: full suite green + template contract tests
  extended to the new lane (proposal-only invariants).
- **AZ-4 (docs + onboarding)** — `docs/azure-setup.md`, doctor checks
  (PAT identity, scopes via cheap API probes, webhook reachability,
  lane pipeline existence, harness variables NAMES), README parity
  matrix, runbook for the user's live verification. Gate: doctor --json
  schema stable; docs cross-linked.

Shared-file touchpoints (coordinate through the coordinator only):
`Settings` (config.py), `execute_run_command` dispatch (runs/service.py),
gateway router registration (main.py), provider literals
(durable/models.py checks), doctor, README.

## 5. Security posture (invariants carried over)

- Lane gets NO forge credential: no PAT, no webhook password; harness
  keys only, as pipeline secret variables the repo owner provisions.
- Publisher is the only writer, via CAS from the frozen attempt base.
- The bot identity cannot approve/complete PRs (PAT permission shape;
  doctor asserts the identity's Contribute-to-PR without Approve).
- Webhook credentials = forge-only secret; responses stay 2XX-on-commit
  (durability contract) and 401 on anything unauthenticated.
- Evidence policy redaction applies to repair contexts/summaries as
  everywhere else (F23).
