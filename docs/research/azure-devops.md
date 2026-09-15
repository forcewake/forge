# Azure DevOps for forge — deep API research (2026-09)

Implementation reference for the Azure DevOps source/execution adapter (ADR-0024).
Extends (and where necessary corrects) the §1 ground truths of
[specs/azure-devops-brief.md](../specs/azure-devops-brief.md). Researched against
learn.microsoft.com live pages (REST reference 7.1/7.2, service-hooks events,
integrate concepts) on 2026-09-15.

Convention: **[documented]** = verified against a cited live learn.microsoft.com
page (quote or exact field list); **[community-documented]** = consistent across
practitioner sources but not on an official page; **[inference]** = derived design
conclusion; **[unverified]** = could not be checked live — treat as hypothesis
until the AZ-4 lab run.

**Corrections to the brief found during this research** (load-bearing — read §2.8,
§4.3, §6.6, §5.1 before implementing):
1. `GET /_apis/build/builds` has **no `sourceVersion` query parameter** — filter by
   `repositoryId` + time window, then match `sourceVersion` client-side (§6.6).
2. PR `lastMergeCommit` is a plain **GitCommitRef** (`commitId`, `url`). The
   `{sourceCommit, targetCommit, commonCommit}` triple does **not** exist on it;
   the three-way SHAs live on the **iteration** object (`sourceRefCommit`,
   `targetRefCommit`, `commonRefCommit`) (§4.3).
3. The `workitem.commented` webhook payload has **no comment id** — comment text
   arrives as `resource.fields["System.History"]`, author as `System.ChangedBy` (§2.5, §5.1).
4. PR-create work-item linking: the documented body field is `workItemRefs`, and the
   *reliable* programmatic link is a WIT ArtifactLink PATCH on the work item (§4.6).
5. Reviewer votes: approved = **10**, approved with suggestions = **5** (not 8),
   no vote = 0, waiting for author = −5, rejected = −10 (§4.5).

---

## 1. Hosts, auth, error shapes (edge cases)

### 1.1 Host matrix — [documented]
- Services: `https://dev.azure.com/{org}` (collection is implicit/`DefaultCollection`).
  Project-scoped resources: `https://dev.azure.com/{org}/{project}/_apis/{area}/{resource}`.
- Server: `https://{instance}/{collection}/...` (REST reference uses
  `https://{instance}/{collection}/{project}/_apis/...`; classic on-prem default
  `https://{server}:8080/tfs/{collection}`).
- Most APIs forge uses live on the main host. Others live on dedicated hosts on
  Services (`vsrm.dev.azure.com` releases, `feeds.dev.azure.com` artifacts,
  `extmgmt.dev.azure.com` extensions, `analytics.dev.azure.com` analytics) — **none of
  forge's endpoints need them**; every endpoint in this doc is on the org host.
- Identity/PAT management lives on `vssps.dev.azure.com` (e.g. PAT Lifecycle
  Management API `https://vssps.dev.azure.com/{org}/_apis/tokens/pats`), and that
  specific API is Entra-token-only (PAT-auth is rejected there). Forge never needs it
  — but `forge doctor` must not probe vssps with a PAT [documented + community].

### 1.2 Basic auth exact bytes — [documented]
- `Authorization: Basic base64(":" + PAT)` — **empty username, colon prefix**,
  base64 of the ASCII bytes `:{PAT}`. Official doc: *"Authorization: Basic
  BASE64_USERNAME_PAT_STRING"* with the curl sample `curl -u :{PAT} …`.
- Wrong encoding (PAT without colon, or `Bearer`) does **not** reliably give 401:
  the reported symptom is **HTTP 203 Non-Authoritative** with the login page HTML in
  the body [community-documented; stackoverflow.com/q/58614515]. Client rule
  [inference]: treat any 2xx/3xx that is not the expected 200/201/202 shape, or any
  `text/html` body, as an auth-config error; never parse errors off 203 bodies.
- Expired/revoked PAT: **401 Unauthorized, or `TF400813: Resource not available for
  anonymous access`** — [documented, PAT page expiration table].
- Scopes forge requests (documented scope descriptions, REST reference security
  sections): `vso.code_write` (pushes, PRs, threads create via code_write or
  `vso.threads_full`), `vso.build` / `vso.build_execute` (builds, timelines, logs,
  artifacts, run pipelines), `vso.work` / `vso.work_write` (work-item comments),
  `vso.threads_full` (PR comment threads R/W). Service-hook management
  (`/_apis/hooks/*`) is covered by the read scopes that mention "receive
  notifications ... via service hooks" plus collection-level permission to create
  subscriptions [documented scope text; subscription-create permission requirement
  itself: [inference] — the creating identity needs Edit Subscriptions rights].
- PAT carries its owner's permissions; use a dedicated service account (brief §1
  stands). PAT-over-HTTPS **git** operations: a PAT named
  `git: https://dev.azure.com/{org} on {machine}` is auto-created when git.exe
  connects via Git Credential Manager — [documented PAT page]; for raw-HTTPS git
  against Azure Repos the credential is `username: <any non-empty string>`,
  `password: <PAT>` [community-documented convention; forge never does git-over-HTTPS
  itself — the publisher uses the REST push API].

### 1.3 api-version rules — [documented]
- *"API version must be specified with every request."* Format
  `{major}.{minor}[-{stage}[.{resource-version}]]` (e.g. `7.1`, `7.1-preview.4`).
  Query param `?api-version=…` or header `Accept: application/json;api-version=…`.
- Preview stripes: while an API is in preview you may pin `7.1-preview.N`; after the
  API releases, the preview version is deprecated and *"requests that specify a
  -preview version get rejected"* after ~12 weeks. **Never let forge depend on a
  preview stripe where a GA alternative exists** [documented; consequence: inference].
- What happens on a **missing** api-version is not crisply documented as an error;
  official guidance is that behavior is unpredictable/breakage-prone
  [documented guidance]. Practical rule for the client [inference]: always inject
  api-version centrally; on `TF400813`/HTML/redirect responses, suspect a missing or
  wrong api-version.
- Endpoint-specific versions forge must pin (verified against each REST page):
  pushes `7.1`; PRs/threads/comments (git) `7.1` (threads create also accepts
  `7.2-preview.1`); WIT comments `7.1-preview.4` (preview is the **only** stripe for
  this API family — GA'd later; Azure DevOps Services also exposes 7.2-preview.4);
  policy configurations `7.1`; policy evaluations `7.1-preview.1` (preview-only);
  pipelines create `7.2-preview.1` under the 7.2 view (`7.1-preview.1` under 7.1);
  runs `7.1`/`7.2`; artifacts `7.1`; timeline/logs `7.1`; hooks subscriptions `7.1`.

### 1.4 Error shapes — [documented + community]
- JSON error body: `{"$id":"1","innerException":null,"message":"TF<NNNNNN>: …",
  "typeName":"…, Microsoft.TeamFoundation…","typeKey":"…","errorCode":0,"eventId":3000}`.
  Match on the `TF` code inside `message` (and `typeKey`), never on prose [inference;
  shape verified on the pipelines-create failure report: `Value cannot be null…
  Parameter name: repositoryName` delivered in exactly this envelope].
- Rate limit (throttle): HTTP **429**, message `TF400733: The request has been
  canceled: Request was blocked due to exceeding usage of resource <resource> in
  namespace <namespace ID>` — [documented, rate-limits page]. Honor `Retry-After`;
  monitor `X-RateLimit-Remaining` / `X-RateLimit-Limit` when present [documented
  best-practice section].
- 401 bad Basic / expired PAT (see 1.2); 403 = permission; 404 = missing *or* no
  permission (do not treat as "absent") [documented response-code table in the
  call-REST page: 200/201/204/400/401/403/404/409 meanings].
- Git ref-update failures do **not** surface as HTTP errors — the push returns 200
  with per-ref `updateStatus` (§3.3).

### Sources (§1)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/?view=azure-devops (URL structure)
- https://learn.microsoft.com/en-us/azure/devops/integrate/how-to/call-rest-api?view=azure-devops
- https://learn.microsoft.com/en-us/azure/devops/integrate/concepts/rest-api-versioning
- https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate?view=azure-devops&tabs=Windows
- https://learn.microsoft.com/en-us/azure/devops/integrate/concepts/rate-limits?view=azure-devops
- https://stackoverflow.com/questions/58614515/azure-devops-rest-api-returning-http-status-203-when-using-python-requests
- https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/pipelines/create?view=azure-devops-rest-7.2 (scope table, error envelope example)

---

## 2. Webhook payload reference + normalization maps

### 2.0 Envelope — [documented]
Every service-hooks payload (samples on the events page):

```json
{
  "id": "a0a0a0a0-…",                       // notification/delivery id (GUID)
  "eventType": "git.push",                  // the discriminator — parse THIS
  "publisherId": "tfs",
  "message": { "text": "…", "html": "…", "markdown": "…" },
  "detailedMessage": { "text": "…", "html": "…", "markdown": "…" },
  "resource": { … },                        // resourceVersion-dependent
  "resourceVersion": "2.0",
  "resourceContainers": {
    "collection": { "id": "…", "baseUrl": "https://dev.azure.com/{org}/" },
    "account":   { "id": "…", "baseUrl": "https://dev.azure.com/{org}/" },
    "project":   { "id": "…", "baseUrl": "https://dev.azure.com/{org}/" }
  },
  "createdDate": "2024-12-02T12:21:13.8866607Z"
}
```

Gotchas:
- `resourceContainers.*.baseUrl` is the cleanest way to recover the org URL
  regardless of `{org}.visualstudio.com` vs `dev.azure.com/{org}` provenance [inference].
- **`publisherId` is not a stable discriminator**: the documented `build.complete`
  sample appears twice, once with `"publisherId": "tfs"` and once with
  `"publisherId": "azure-devops"`. Route on `eventType` only [documented samples].
- Subscription provisioning (brief §1 stands): `POST /{org}/_apis/hooks/subscriptions?api-version=7.1`
  with `publisherId:"tfs"`, `eventType`, `resourceVersion:"1.0"`,
  `consumerId:"webHooks"`, `consumerActionId:"httpRequest"`,
  `publisherInputs:{...event filters}`, `consumerInputs:{url, basicAuthUsername,
  basicAuthPassword, httpHeaders, resourceDetailsToSend:"all"}` [documented field
  names in the webHooks consumer + events-page settings; exact `basicAuth*` input
  keys appear in the consumer UI and community samples, not in the events page
  prose]. `resourceDetailsToSend`: `all` (default) | `minimal` | `none` [documented].
  **HTTPS is required for basic auth on the consumer** [documented webhooks page].
  No HMAC exists — validation is Basic + optional custom headers only [documented
  by absence on the webhooks page].

### 2.1 `git.push` (publisher `tfs`, event `git.push`, resource `push`) — [documented]
Filters: `branch`, `pushedBy` (group), `repository` (GUID). Sample resource (trimmed
from the events page):

```json
"resource": {
  "commits": [{
    "commitId": "4444eeee455ff5aaaaabb66ccccccccc7777cccc",
    "author":    { "name": "Jamal Hartnett", "email": "fabrikamfiber4@hotmail.com", "date": "2024-02-25T19:01:00Z" },
    "committer": { "name": "Jamal Hartnett", "email": "fabrikamfiber4@hotmail.com", "date": "2024-02-25T19:01:00Z" },
    "comment": "Fixed bug in web.config file",
    "url": "https://dev.azure.com/{org}/…/_git/{repo}/commit/4444eeee…"
  }],
  "refUpdates": [{
    "name": "refs/heads/main",
    "oldObjectId": "d3d3d3d3-eeee-ffff-aaaa-b4b4b4b4b4b4",
    "newObjectId": "e4e4e4e4-ffff-aaaa-bbbb-c5c5c5c5c5c5"
  }],
  "repository": {
    "id": "f5f5f5f5-aaaa-bbbb-cccc-d6d6d6d6d6d6",
    "name": "Fabrikam-Fiber-Git",
    "url": "https://dev.azure.com/{org}/…/_apis/git/repositories/f5f5f5f5-…",
    "project": { "id": "a6a6a6a6-…", "name": "Fabrikam-Fiber-Git" },
    "defaultBranch": "refs/heads/main",
    "remoteUrl": "https://dev.azure.com/{org}/…/_git/Fabrikam-Fiber-Git"
  },
  "pushedBy": { "id": "…@Live.com", "displayName": "Jamal Hartnett",
                "uniqueName": "fabrikamfiber4@hotmail.com" },
  "pushId": 14,
  "date": "2014-05-02T19:17:13.3309587Z",
  "url": "…/_apis/git/repositories/{repoId}/pushes/14"
}
```

Normalization map (→ forge fields):

| JSON path | forge field |
|---|---|
| `eventType` | source event discriminator (`git.push`) |
| `resource.repository.id` | repo id (canonical key for repo_full_name resolution) |
| `resource.repository.name` + `resource.repository.project.name` | `repo_full_name` (`{project}/{repo}` — AzDO's display form) |
| `resource.refUpdates[0].name` | branch ref (strip `refs/heads/`) |
| `resource.refUpdates[0].oldObjectId` / `newObjectId` | before/after SHAs (CAS evidence) |
| `resource.commits[].commitId` | pushed SHAs |
| `resource.pushedBy.uniqueName` | author (bot-loop guard) |
| `resource.pushId` | delivery-key component (`push:{pushId}:{newObjectId}`) |

### 2.2 `git.pullrequest.created` (publisher `tfs`, resource `pullrequest`) — [documented]
Filters: `repository` (GUID), `pullrequestCreatedBy`, `pullrequestReviewersContains`,
`branch`. Resource (trimmed from documented sample): `pullRequestId` (int),
`status` (`active`), `title`, `description`, `creationDate`, `sourceRefName`
(`refs/heads/mytopic`), `targetRefName` (`refs/heads/main`), `mergeStatus`
(`succeeded`), `mergeId` (GUID), `lastMergeSourceCommit{commitId,url}`,
`lastMergeTargetCommit{commitId,url}`, `lastMergeCommit{commitId,url}`,
`createdBy{id,displayName,uniqueName,imageUrl}`,
`repository{id,name,url,project{id,name},defaultBranch,remoteUrl}`,
`reviewers[{vote,id,displayName,isContainer,…}]`, `commits[{commitId,url}]`.

Normalization map:

| JSON path | forge field |
|---|---|
| `resource.pullRequestId` | pr id (`pr:{id}:created`) |
| `resource.createdBy.uniqueName` | author (bot-loop + `forge/*` head guard) |
| `resource.sourceRefName` / `targetRefName` | head/base branches |
| `resource.lastMergeSourceCommit.commitId` | head SHA at creation |
| `resource.repository.{id,project.name,name}` | repo identity, `repo_full_name` |
| `resource.reviewers[].vote` | human votes (read-only display; §4.5) |

### 2.3 `git.pullrequest.updated` (publisher `tfs`, resource `pullrequest`) — [documented]
Filters: `repository`, `pullrequestCreatedBy`, `pullrequestReviewersContains`,
`branch`, **`notificationType`** with documented values:
- `PushNotification` — the source branch is updated
- `ReviewersUpdateNotification` — reviewers change
- `StatusUpdateNotification` — the status changes
- `ReviewerVoteNotification` — vote score changes

(The brief's provisional "new commits, votes, status" mapping is right, but the
filter vocabulary is exactly these four; PR completion has its own separate event
`git.pullrequest.merged`.) The documented sample resource shows the **completed**
state (`status:"completed"`, `closedDate` set); it contains **no `update` object** —
the *what changed* signal is the subscription's `notificationType` filter, so forge
should create up to four subscriptions on this eventType (or one unfiltered and
diff states via `GET /pullrequests/{id}`) [inference; filters documented]. The
payload carries `commits[]` and the same `lastMerge*`/`reviewers` shapes as §2.2.

Normalization: identical to §2.2 plus delivery key `pr:{id}:updated:{lastMergeSourceCommit.commitId}`
— the new head SHA is the incremental-review trigger (§4.3).

### 2.4 `git.pullrequest.commented-on` (publisher `tfs`, event
`ms.vss-code.git-pullrequest-comment-event`, resource `pullrequest`) — [documented]
Filters: `repository` (GUID), `branch`. Full documented sample (trimmed):

```json
"resource": {
  "comment": {
    "id": 2,
    "parentCommentId": 1,
    "author": { "displayName": "Jamal Hartnett", "id": "11bb11bb-…",
                "uniqueName": "fabrikamfiber4@hotmail.com", "imageUrl": "…" },
    "content": "This is my comment.",
    "publishedDate": "2024-06-17T11:22:33.456789Z",
    "lastUpdatedDate": "2024-06-17T16:58:33.123889Z",
    "lastContentUpdatedDate": "2024-06-17T16:58:33.123889Z",
    "commentType": "text",
    "_links": {
      "self":    { "href": "…/pullRequests/1/threads/5/comments/2" },
      "threads": { "href": "…/pullRequests/1/threads/5" }
    }
  },
  "pullRequest": {
    "repository": { "id": "c2c2c2c2-…", "name": "Fabrikam", "project": { "id": "d3d3d3d3-…", "name": "Fabrikam" },
                    "defaultBranch": "refs/heads/main", "remoteUrl": "…" },
    "pullRequestId": 1, "status": "active",
    "createdBy": { "id": "11bb11bb-…", "displayName": "Jamal Hartnett", "uniqueName": "fabrikamfiber4@hotmail.com" },
    "title": "my first pull request", "description": " - test2\r\n",
    "sourceRefName": "refs/heads/mytopic", "targetRefName": "refs/heads/main",
    "mergeStatus": "succeeded", "mergeId": "e4e4e4e4-…",
    "lastMergeSourceCommit": { "commitId": "4444eeee…", "url": "…" },
    "lastMergeTargetCommit": { "commitId": "5555ffff…", "url": "…" },
    "lastMergeCommit":       { "commitId": "6666aaaa…", "url": "…" },
    "reviewers": [ { "vote": 0, "id": "22cc22cc-…", "displayName": "[Mobile]\\Mobile Team", "isContainer": true } ],
    "commits": [ { "commitId": "6666aaaa…", "url": "…" } ]
  }
}
```

Notes:
- The author field is **`comment.author`** (not `commentedBy`).
- **The thread id is only in `_links`** (`threads/5/comments/2` and the `threads`
  href) in the documented sample — no `threadId` field. Parse it out of the
  `threads` href [documented sample; field absence]. Forge only needs the comment
  content + PR id for commands, so this is informational.
- The documented message text is *"Jamal Hartnett **has edited** a pull request
  comment"* — the event fires on edits as well as creation [documented sample];
  treat re-deliveries/edits idempotently via the delivery key
  `comment:{id}:{lastContentUpdatedDate}` [inference].
- Comment `content` is markdown (PR comments render markdown in the portal; the
  webhook `message` object separately carries `text`/`html`/`markdown` renderings)
  [documented envelope renderings].

| JSON path | forge field |
|---|---|
| `resource.comment.id` | delivery key `comment:{id}` |
| `resource.comment.author.uniqueName` | author (bot-loop guard) |
| `resource.comment.content` | command text (same parser as GitHub/GitLab) |
| `resource.pullRequest.pullRequestId` | pr id |
| `resource.pullRequest.repository.{project.name,name}` | `repo_full_name` |
| `resource.pullRequest.sourceRefName` | head branch (`forge/*` guard) |

### 2.5 `build.complete` (publisher `tfs` **or** `azure-devops`, resource `build`) — [documented]
Filters: `definitionName`, `buildStatus` (`Succeeded|PartiallySucceeded|Failed|Stopped`).
Two documented sample payloads exist: a **minimal** resource
(`{id, buildNumber, status, result, url, definition{id,name}, project{id,name}}`)
and a **full** Build resource (resourceVersion 2.0) carrying `_links.self` /
`_links.timeline`, `sourceBranch` (`refs/heads/main`), `sourceVersion` (commit SHA),
`repository{id,type:"TfsGit",name,url}`, `definition{id,name,project}`,
`queue{id,name,pool{isHosted}}`, `requestedFor{id,uniqueName,descriptor}`,
`reason` (`manual`/`individualCI`/`pullRequest`/…), `finishTime` [documented].
Set the subscription's `buildStatus` filter to reduce noise; still guard
`definition.id != FORGE_AZDO_LANE_PIPELINE_ID` in the router (our own runs) [inference
per brief §3].

| JSON path (full variant) | forge field |
|---|---|
| `resource.id` | build/run id; delivery key `build:{id}:{result}` |
| `resource.result` | `succeeded`/`partiallySucceeded`/`failed`/`canceled` (§6.4) — `failed` → `debug_ci` |
| `resource.sourceVersion` | commit SHA for PR correlation (via PR head SHAs, not builds-list filter — §6.6) |
| `resource.sourceBranch` | branch ref |
| `resource.definition.id` | skip-if-lane guard |
| `resource.project.id` | project key for follow-up timeline/log calls |
| `resource._links.timeline.href` | direct handle for `debug_ci` fetch |

### 2.6 `workitem.commented` (publisher `tfs`, resource `workitem`) — [documented]
Filters: `areaPath`, **`commentPattern`** (substring match — usable for `/implement`
pre-filtering at the source), `workItemType`, `tag`. Documented resource (trimmed):

```json
"resource": {
  "id": 5,
  "rev": 4,
  "fields": {
    "System.AreaPath": "FabrikamCloud",
    "System.TeamProject": "FabrikamCloud",
    "System.WorkItemType": "Bug",
    "System.State": "New",
    "System.CreatedBy": "Jamal Hartnett",
    "System.ChangedBy": "Jamal Hartnett",
    "System.Title": "Some great new idea!",
    "System.History": "This is a great new idea"
  },
  "url": "https://dev.azure.com/{org}/…/_apis/wit/workItems/5"
}
```

**The payload has no comment object and no comment id.** The comment body is
`fields["System.History"]`; the author is `fields["System.ChangedBy"]`. Delivery
identity: there is nothing unique to the comment in the payload — use
`workitem:{id}:comment:{rev}` (the rev bumps per change) or a content hash
`workitem:{id}:{sha1(System.History)}`; the inbox unique-index rule stays intact
[inference built on documented shape].

| JSON path | forge field |
|---|---|
| `resource.id` | work-item id (issue id) |
| `resource.fields["System.History"]` | comment text → command (`/implement`, `/go`, `/cancel`, `/security`) |
| `resource.fields["System.ChangedBy"]` | author (bot-loop guard vs `FORGE_AZDO_BOT_NAME`) |
| `resource.fields["System.TeamProject"]` | project key |
| `resource.rev` | delivery-key component |
| `eventType` | `workitem.commented` |

### 2.7 Subscription filters worth wiring (onboarding)
- `git.push`: `repository` GUID + `branch` (limit to target branches) — [documented].
- PR events: `repository` + `branch` (target branch) — [documented].
- `workitem.commented`: `commentPattern` can be one fixed string; commands are
  several (`/implement`, `/go`, …) so prefer no pattern + router-side parse
  [inference].
- `build.complete`: `buildStatus=Failed` if forge only debugs failures — but keep
  `Succeeded` flowing if the reconciler also closes runs on success [inference].

### 2.8 Cross-event invariants for the router [inference, built on §2.1–2.6]
- Always `eventType` for routing; `publisherId` unstable (§2.5).
- Always resolve org from `resourceContainers` for cross-checking
  `FORGE_AZDO_ORG_URL` (multi-connection hygiene).
- `uniqueName` is the e-mail-like identity everywhere — the single author field to
  normalize; `displayName` is non-unique.
- All ids that matter are ints (`pullRequestId`, `pushId`, build `id`, work-item
  `id`) except repo/project ids (GUIDs) — durable models must reflect that split.

### Sources (§2)
- https://learn.microsoft.com/en-us/azure/devops/service-hooks/events?view=azure-devops (all six payload samples; the GitHub mirror MicrosoftDocs/azure-devops-docs `docs/service-hooks/events.md` was used to lift verbatim JSON)
- https://learn.microsoft.com/en-us/azure/devops/service-hooks/services/webhooks?view=azure-devops (consumer inputs, resourceDetailsToSend, HTTPS-for-basic-auth)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/hooks/publishers/list-event-types?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/core/http-clients?view=azure-devops-rest-7.1 (envelope reference class)

---

## 3. Git CAS: pushes/refs

### 3.1 Push API — [documented]
`POST https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repositoryId}/pushes?api-version=7.1`
(`repositoryId` = name **or** GUID). Body:

```json
{
  "refUpdates": [
    { "name": "refs/heads/forge/wi-42", "oldObjectId": "0000000000000000000000000000000000000000" }
  ],
  "commits": [
    {
      "comment": "forge: candidate for WI-42 (attempt 1)",
      "changes": [
        { "changeType": "add",    "item": { "path": "/docs/plan.md" },
          "newContent": { "content": "# Plan …", "contentType": "rawtext" } },
        { "changeType": "edit",   "item": { "path": "/src/app.py" },
          "newContent": { "content": "…", "contentType": "rawtext" } },
        { "changeType": "delete", "item": { "path": "/src/old.py" } }
      ]
    }
  ]
}
```

- `changeType`: `add|edit|delete` (the enum has more members; forge uses these three).
- `contentType`: `rawtext` | `base64encoded` (binary files documented with a
  base64encoded sample) — [documented sample].
- Branch creation = push with `oldObjectId` = 40×`0` **plus the initial commit in the
  same request** (branch-creation-by-push); a bare "create branch at existing commit"
  uses `POST …/_apis/git/repositories/{repoId}/refs` with
  `{ "refUpdates": [{ "name": "refs/heads/x", "oldObjectId":
  "0000000000000000000000000000000000000000", "newObjectId": "<base sha>" }] }` —
  the Refs-UpdateRefs doc states *"You must specify both the old and new commit to
  avoid race conditions"* — [documented].
- Multi-commit pushes are allowed (commits array) — forge uses one commit per publish
  for CAS cleanliness [inference].

### 3.2 Refs read — [documented]
`GET …/git/repositories/{repoId}/refs?filter=heads/{branch}&api-version=7.1` →
`{ "value": [ { "name": "refs/heads/x", "objectId": "<sha>", "peeledObjectId": …,
"isLocked": false } ], "count": 1 }`. The `objectId` is the CAS `oldObjectId` for
the next push.

### 3.3 Failure taxonomy: `GitRefUpdateStatus` — [documented]
Push responses return per-ref results (and `POST /refs` returns
`GitRefUpdateResult { repositoryId, name, oldObjectId, newObjectId, isLocked,
updateStatus, success, customMessage, rejectedBy }`). Documented enum values
include:

| updateStatus | meaning (documented text, trimmed) |
|---|---|
| `succeeded` | completed successfully |
| `forcePushRequired` | part of the graph would be disconnected and caller lacks ForcePush |
| `staleObjectId` | the oldObjectId did not match the ref tip (our CAS-miss case) |
| `createBranchPermissionRequired` / `createBranchNoParent` | branch creation rejected |
| `unauthorized` / `rejectedByPolicy` / `locked` | policy/permission/lock rejections |
| (further members exist in the enum) | |

→ `BranchDriftError` maps from `staleObjectId`; permission-shaped statuses map to
distinct typed errors so the reconciler can distinguish "drifted, re-base" from
"misconfigured PAT" [inference per ADR-0024 §4].
- Unauthenticated/unauthorized REST calls return HTTP 401/403 with the `TF…` envelope
  (§1.4); a successful-but-rejected ref update is the 200-with-`updateStatus` case —
  **both paths must be typed** [inference].

### 3.4 File caps relevant to pushes — see §8.2 (5 GB push, path-length
`VS403729`, 100 MB file guidance).

### Sources (§3)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pushes/create?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/refs/update-refs?view=azure-devops-rest-7.1 (GitRefUpdateResult, GitRefUpdateStatus enum, 40-zero oldObjectId example)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pushes/get?view=azure-devops-rest-7.1 (includeRefUpdates/includeCommits)
- https://stackoverflow.com/questions/59846426/azure-devops-rest-api-to-create-a-branch-from-a-specific-branch (refs filter=read recipe)

---

## 4. Pull requests: lifecycle, iterations/diffs, threads, change tracking, votes

### 4.1 Create (Draft PR) — [documented]
`POST …/git/repositories/{repoId}/pullrequests?api-version=7.1` body:

```json
{
  "sourceRefName": "refs/heads/forge/wi-42",
  "targetRefName": "refs/heads/main",
  "title": "forge: WI-42 candidate",
  "description": "Attempt 1 from attempt-base a1b2c3…",
  "isDraft": true
}
```

- `isDraft` is a documented field of GitPullRequest (`"Draft / WIP pull request"`)
  [documented; supported on Services ≥ 2020 per brief].
- Response: `pullRequestId`, `status` (`active`), `creationDate`, `mergeStatus`,
  `lastMergeSourceCommit{commitId,url}`, `lastMergeTargetCommit{commitId,url}`,
  `lastMergeCommit{commitId,url}`, `repository{…}`, `createdBy{…}`, `reviewers[]`,
  `supportsIterations`. `artifactId` =
  `vstfs:///Git/PullRequestId/{projectId}/{repositoryId}/{pullRequestId}` [documented].
- Updateable properties (Pull Requests - Update doc): Status, Title, Description
  (≤ 4000 chars), CompletionOptions, MergeOptions, AutoCompleteSetBy.Id,
  TargetRefName (retargeting) — *"Attempting to update other properties … will
  either cause the server to throw an InvalidArgumentValueException, or to silently
  ignore the update"* — [documented].
- Reviewer cap: **≤ 1000 reviewers per PR** (new PRs are rejected beyond that)
  [documented, about-pull-requests].

### 4.2 PR object: `lastMerge*` semantics — [documented]
- `lastMergeSourceCommit` — *"the commit at the head of the source branch at the time
  of the last pull request merge"* (i.e. the current head SHA, materialized on read).
- `lastMergeTargetCommit` — head of target at last merge computation.
- `lastMergeCommit` — *"the commit of the most recent pull request merge. If empty,
  the most recent merge is in progress or was unsuccessful."* Typed **GitCommitRef**
  (commitId + url) in the 7.1/6.0 REST references and in every documented webhook
  sample. **The `{sourceCommit, targetCommit, commonCommit}` object in the brief's
  §1 is not present in the documented REST schema** — correct the model to
  GitCommitRef (this changes the typed handle for reactive review; see 4.3).
- `mergeStatus`: `succeeded` | `notSet` | `conflicts` | `pending` | `failure`
  (PullRequestAsyncStatus family; `mergeFailureMessage`/`mergeFailureType` carried on
  conflict) [documented fields].
- `supportsIterations` — true ⇒ *"comments left in one iteration will be tracked
  across future iterations"* [documented].

### 4.3 Iterations and diffs (incremental reactive review) — [documented]
- `GET …/pullRequests/{id}/iterations?api-version=7.1` (optionally
  `includeCommits=true`) — iterations are created by PR creation and by every push.
- `GET …/pullRequests/{id}/iterations/{iterationId}?api-version=7.1` returns
  `GitPullRequestIteration` with exactly the three-way SHAs the incremental
  reviewer needs:

| field | documented meaning |
|---|---|
| `sourceRefCommit` (GitCommitRef) | "The source Git commit of this iteration" — the head SHA **after** this push |
| `targetRefCommit` (GitCommitRef) | "The target Git commit of this iteration" |
| `commonRefCommit` (GitCommitRef) | "The first common Git commit of the source and target refs" (merge base) |
| `push` (GitPushRef) | the push that created the iteration |
| `commits` | commits included (may be truncated: `hasMoreCommits`) |
| `changeList` | GitPullRequestChange[] for the iteration |

  → **before/after model for the reactive lane**: after `git.pullrequest.updated`,
  list iterations, take latest iteration N; diff scope = `iteration N.sourceRefCommit`
  vs `iteration N−1.sourceRefCommit` (source-side increment), and the merge base =
  `commonRefCommit` (retarget/base drift shows up as a changed `commonRefCommit`)
  [inference built on documented fields].
- `GET …/pullRequests/{id}/iterations/{iterationId}/changes?$compareTo={N−1}&$top&$skip&api-version=7.1`
  — *"Retrieve the changes made in a pull request between two iterations"*, returns
  `GitPullRequestIterationChanges { changeEntries: GitPullRequestChange[],
  nextSkip, nextTop }` (paginated; `nextSkip=0` = done). `iterationId` must be
  between 1 and the latest [documented].
- `GitPullRequestChange` carries `changeId`, `changeType`, `item` (path), and
  `originalPath`/`sourceServerItem` for renames [documented].

### 4.4 Threads (forge's review surface) — [documented]
- Create: `POST …/pullRequests/{id}/threads?api-version=7.1` (7.2 view shows
  `7.2-preview.1`) body:

```json
{
  "comments": [ { "parentCommentId": 0, "content": "…finding…", "commentType": 1 } ],
  "status": 1,
  "threadContext": {
    "filePath": "/new_feature.cpp",
    "rightFileStart": { "line": 5, "offset": 1 },
    "rightFileEnd":   { "line": 5, "offset": 13 }
  },
  "pullRequestThreadContext": {
    "changeTrackingId": 1,
    "iterationContext": { "firstComparingIteration": 1, "secondComparingIteration": 2 }
  }
}
```

  The documented example uses **numeric enums in the request** (`commentType: 1` =
  text, `status: 1` = active) and returns **strings** (`"commentType": "text"`,
  `"status": "active"`) — accept both on parse [documented sample].
- Reply: `POST …/threads/{threadId}/comments` body `{"content": "…",
  "parentCommentId": <id>, "commentType": 1}`. *"up to 500 comments can be created
  per thread"* [documented, verbatim cap].
- List: `GET …/pullRequests/{id}/threads?$iteration={n}&$baseIteration={n−1}&api-version=7.1`
  — iteration-scoped thread listing exists and is how a reviewer sees the threads
  relevant to a diff [documented parameters].
- Update (sticky progress): `PATCH …/threads/{threadId}?api-version=7.1` body
  `{"status": "fixed"}` — thread statuses documented: `unknown`, `active`, `fixed`,
  `wontFix`, `closed`, `byDesign` (plus `pending` in the tracking-criteria context)
  [documented enum on the threads update/list pages]. The brief's
  `active/fixed/wontFix/closed` is correct but incomplete — add `byDesign`.
- Thread context fields (documented): `filePath`, `leftFileStart/End`,
  `rightFileStart/End` (1-based line + offset; `CommentPosition`). Left side =
  target/base version, right side = source/head version [documented semantics;
  side assignment per PR-diff UI].
- `pullRequestThreadContext.changeTrackingId` — set it when creating the thread; the
  server then re-tracks the comment across iterations (CommentTrackingCriteria:
  *"used to identify which iteration context the thread has been tracked to … along
  with some detail about the original position and filename"*). `iterationContext.
  firstComparingIteration/secondComparingIteration` record the diff the thread was
  created on; per the schema docs *"If FirstComparingIteration equals
  SecondComparingIteration, then this version is the common commit"* [documented].
  This is the sticky-thread mechanism — do **not** re-derive positions client-side
  per push [inference].
- Scopes: threads create/read require `vso.code_write` or `vso.threads_full`
  [documented on each operation].

### 4.5 Votes (read-only for forge) — [documented]
`IdentityRefWithVote.vote`: **`10` approved, `5` approved with suggestions, `0` no
vote, `-5` waiting for author, `-10` rejected** (documented verbatim on the
Pull Request Reviewers operations; forge displays, never votes — ADR-0024 §5).
Group/team reviewers: members' votes roll up into the container's `vote` with
`votedFor` detail [documented]. Reset-on-push is a policy behavior (§7) — a vote can
disappear between `updated` events; the reviewer list in the PR payload is the
single source of truth at read time [inference].

### 4.6 Work-item linking on PR create — [documented + community]
- The brief's `workItems:[{id}]` body field is **not** the documented contract.
  GitPullRequest carries `workItemRefs` (ResourceRef collection) — surfaced in the
  REST docs and in the `includeWorkItemRefs=true` read flag; practitioners report
  passing refs at create-time is unreliable for the visible PR⇄WI link
  [documented field; community reports on reliability].
- Reliable programmatic linking (recommended for `/go`): PATCH the **work item**
  with an ArtifactLink relation after PR creation:

```json
PATCH …/_apis/wit/workItems/{workItemId}?api-version=7.1
Content-Type: application/json-patch+json
[ { "op": "add", "path": "/relations/-",
    "value": { "rel": "ArtifactLink",
               "url": "vstfs:///Git/PullRequestId/{projectId}%2F{repositoryId}%2F{pullRequestId}",
               "attributes": { "name": "Pull Request" } } } ]
```

  — `attributes.name` is **case-sensitive ("Pull Request")**; the vstfs URL must
  use the exact `artifactId` format (segments separated, URL-encoded `%2F` when
  embedded) or the link renders one-way [community-documented, consistent across
  multiple threads; artifactId template itself documented in the PR REST reference].
- Repo-wide link types are enumerable: `GET /_apis/wit/artifactlinktypes` lists
  `{toolType:"Git", artifactType:"PullRequestId", linkType:"Pull Request"}`
  [documented].

### Sources (§4)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/create?view=azure-devops-rest-7.1 (and …/update, …/get-pull-request)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-iterations/list | /get | https://…/pull-request-iteration-changes/get?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-threads/create | /list | /update | https://…/pull-request-thread-comments/create?view=azure-devops-rest-7.1 (500-comment cap verbatim)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-reviewers/create-pull-request-reviewer?view=azure-devops-rest-7.1 (vote values verbatim)
- https://learn.microsoft.com/en-us/azure/devops/repos/git/about-pull-requests?view=azure-devops (1000-reviewer cap)
- https://stackoverflow.com/questions/66588043 + /77597174 + /66657038 (ArtifactLink recipe, case sensitivity, %2F)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/artifact-link-types/list?view=azure-devops-rest-7.1

---

## 5. Work items: comments (plan/gate surface)

### 5.1 Add (plan comment) — [documented]
`POST https://dev.azure.com/{org}/{project}/_apis/wit/workItems/{workItemId}/comments?format={format}&api-version=7.1-preview.4`
body `{ "text": "…markdown…" }`.

- `format` enum `CommentFormat`: **`markdown` | `html`** — pass `format=markdown`
  for plan comments exactly as the brief specifies. Response `Comment`:
  `{ "workItemId": 299, "commentId": 42, "version": 1, "text": "…",
  "renderedText": "<html when format=html>", "createdBy": {…}, "modifiedDate": …,
  "isDeleted": false, "reactions": …, "mentions": … }`. **The response id is the
  comment identity** — persist it for the plan-comment provenance chain [documented
  response shapes; persistence rule inference].
- Scope: `vso.work_write` [documented].
- Bot-loop guard interplay: our own plan comment will trigger `workitem.commented`
  back at forge (author = forge identity) — the `System.ChangedBy` vs
  `FORGE_AZDO_BOT_NAME` guard (brief §3) is the defense [inference, per brief].
- Comments batch read: `GET …/comments?ids={ids}&api-version=7.1-preview.4` →
  `{ "totalCount": N, "count": N, "comments": [...] }`; plain list
  `GET …/comments` paginates for full history [documented operation set:
  Get Comment / Get Comments Batch / list]. Update: `PATCH …/comments/{commentId}`
  [documented].
- No documented hard length cap on a comment `text` in the REST reference
  [documented by absence] — keep plan comments modest anyway (UI usability)
  [inference].

### 5.2 Markdown rendering differences (threads vs WIT comments)
- PR thread comments: markdown rendered in the portal discussion UI; the API takes
  a plain `content` string with **no format parameter** [documented by absence;
  rendering is client-side].
- WIT comments: explicit `format=markdown|html` on add/update/get; `renderedText`
  returns HTML [documented].
- Both support @-mentions; WIT comment mentions come back structured in the
  response `mentions` collection [documented shape].
- Webhook `message` objects give all three renderings (text/html/markdown) for both
  surfaces [documented envelope].

### Sources (§5)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/comments/add-work-item-comment?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/comments/add-comment | /update-comment | /get-comment | /get-comments-batch?view=azure-devops-rest-7.1 (CommentFormat enum, response shapes)

---

## 6. Pipelines: definitions, dispatch, artifacts, timeline/logs, cancel, commit correlation

### 6.1 Create a pipeline pointing at an existing YAML path — [documented + community]
`POST https://dev.azure.com/{org}/{project}/_apis/pipelines?api-version=7.2-preview.1`
(7.1 view: `7.1-preview.1`) — **yes, you can create a definition referencing a YAML
file path already in the repo**:

```json
{
  "name": "forge-lane",
  "folder": "\\forge",
  "configuration": {
    "type": "yaml",
    "path": "/ci/templates/forge-lane.azure-pipelines.yml",
    "repository": { "id": "<repo GUID>", "name": "<repo name>", "type": "azureReposGit" }
  }
}
```

- `ConfigurationType` enum: `unknown`, `yaml`, `designerJson`, `justInTime`,
  `designerHyphenJson` [documented].
- Response `Pipeline`: `{ id (int32), name, folder, revision, url, configuration,
  _links }` — **this id is `FORGE_AZDO_LANE_PIPELINE_ID`** [documented].
- Known failure mode: missing/null `repository.name` or null URL on the repository
  object → 400 `Value cannot be null. Parameter name: repositoryName` (documented in
  the community thread; the fix is passing id + name + type) [community-documented
  with exact envelope].
- The created pipeline's default branch/version semantics: it runs the YAML at
  `path` on the repository's **default branch** unless a run overrides
  `resources.repositories.self.refName` [documented Run parameters refName +
  community confirmation; "default branch" rule is the documented dispatch
  behavior].

### 6.2 Dispatch (Runs API) — [documented]
`POST …/_apis/pipelines/{pipelineId}/runs?api-version=7.1` body:

```json
{
  "resources": { "repositories": { "self": { "refName": "refs/heads/forge/wi-42" } } },
  "templateParameters": { "run_id": "…", "attempt_base": "a1b2c3…",
                          "driver": "claude-code", "model": "…", "work_item_id": "42" },
  "variables": { "SOME_VAR": { "value": "…", "isSecret": false } }
}
```

- Response **carries the run id** (the correlation handle): `Run` =
  `{ "id": int32, "name": "20xx…", "state": "inProgress", "result": null,
  "pipeline": {…}, "resources": {…}, "templateParameters": {…}, "variables": {…},
  "url", "web", "createdDate", "finishedDate", "finalYaml" }` [documented Run schema].
- `RunPipelineParameters`: `previewRun` ("If true, don't actually create a new run.
  Instead, return the final YAML document after parsing templates"), `resources`,
  `stagesToSkip`, `templateParameters`, `variables`, `yamlOverride` (preview only)
  [documented]. A dedicated `POST …/pipelines/{pipelineId}/preview?api-version=7.1`
  exists for the same purpose [documented]. → lane verification uses
  `previewRun`/preview and asserts on `finalYaml` [inference per brief].
- `Run.state`: `inProgress | canceling | canceled | completed | notStarted |
  postponed`; `Run.result`: `succeeded | failed | canceled | unknown |
  canceledByUser | succeededWithIssues` (RunState/RunResult enums; the brief's
  four results map onto the documented superset) [documented enums on the Pipelines
  area; exact member lists per the REST reference definitions].
- `GET …/pipelines/{pipelineId}/runs/{runId}?api-version=7.1` polls; a run id equals
  the underlying **build id** (the two APIs describe the same object — community-
  verified; supports the "Runs for launch, Builds for forensics" split)
  [community-documented].
- Template-parameter type coercion: REST-queued `templateParameters` arrive at YAML
  template evaluation effectively as strings (the REST type is a bare `object`;
  `Build.parameters` is documented as `string`), so YAML parameter types
  (`boolean`, `number`, `step`) are coerced at template-expansion time — never rely
  on native booleans/ints crossing the REST boundary; the lane template should take
  everything as strings [inference built on documented schema; coerce-on-receive
  pattern is community-documented].
- Legacy alternative (Server fallback): `POST …/_apis/build/builds?api-version=7.1`
  with `{ "definition": { "id": N }, "sourceBranch": "refs/heads/x",
  "templateParameters": {…} }` (the Build object documents `sourceBranch`,
  `templateParameters`; the `az pipelines build queue` CLI exposes the same queue
  path with `--definition-id --branch --commit-id --variables`) [documented object
  fields + CLI surface; exact queue-body recipe is brief §1 ground truth].

### 6.3 Artifacts — [documented]
- Pipelines area: `GET …/_apis/pipelines/{pipelineId}/runs/{runId}/artifacts?artifactName={name}&$expand=signedContent&api-version=7.1`
  → `Artifact { "name": …, "url": self, "signedContent": { "url": <signed,
  limited-time anonymous download URL> } }` (`GetArtifactExpandOptions`: `none`,
  `signedContent`). **The signed URL expires** — download promptly, do not persist
  [documented "limited-time anonymous access"; lifetime unspecified → treat as
  minutes-not-hours, inference].
- Builds area: `GET …/_apis/build/builds/{buildId}/artifacts?artifactName={name}&api-version=7.1`
  → `BuildArtifact { id, name, resource: ArtifactResource { type ("PipelineArtifact" |
  "FileContainer" | …), data, url, downloadUrl } }`. The `downloadUrl` serves a
  **zip** by default (`?format=zip`); a single file inside can be fetched by
  rewriting to `?format=file&subPath={path}` on the container URL
  [documented endpoints; the zip/file/subPath trick community-documented].
- Same object, two doors: `runId` (Pipelines) and `buildId` (Builds) are the same
  number for YAML pipeline runs [community-documented].
- Scope for both: `vso.build` [documented].

### 6.4 Timeline + task logs (debug lane feed) — [documented]
- `GET …/_apis/build/builds/{buildId}/timeline?api-version=7.1` (optionally
  `/{timelineId}`, `changeId`, `planId`) → `Timeline { id (uuid), changeId,
  records: TimelineRecord[], url }`. `TimelineRecord`: `{ id (uuid), type, name,
  identifier, order, state, result, startTime, finishTime, lastModified, issues[],
  log: BuildLogReference { id, type, url }, errorCount, warningCount, attempt }`.
  `record.result` values include `succeeded | failed | canceled | skipped |
  abandoned` (+ `succeededWithIssues`) [documented definitions].
- Debug recipe: pick `records[]` where `type=="Task"` and `result=="failed"`, take
  `record.log.url` — download with the PAT [documented pattern shown in the
  Microsoft Q&A example against the real timeline schema].
- Raw log fetch: `GET …/_apis/build/builds/{buildId}/logs` lists
  `BuildLogReference[]`; `GET …/_apis/build/builds/{buildId}/logs/{logId}` returns
  the plain-text log body [documented Builds/Logs operations; log ids are the
  `record.log.id`].
- Size caps on logs: no documented byte cap in the REST reference [documented by
  absence]; defensive truncation to a bounded window around `##[error]` lines is a
  forge-side policy [inference].

### 6.5 Cancel — [documented + community]
- The **Pipelines Runs area has no cancel operation** (Run Pipeline / Get Run /
  List Runs only) — cancellation goes through the **Builds** area:
  `PATCH https://dev.azure.com/{org}/{project}/_apis/build/builds/{buildId}?api-version=7.1`
  body `{"status": "cancelling"}` (`BuildStatus` enum includes `cancelling`;
  response `result` becomes `canceled`) [documented BuildStatus enum; the PATCH
  recipe is the community-standard cancel and consistent with the documented
  status values]. `/cancel` = revoke maps here; permission needed is "Stop Builds"
  [community-documented requirement].
- A `canceling` build may leave queued jobs behind per community reports — the
  reconciler should poll until `state==completed` and `result==canceled` rather
  than trusting the PATCH [community-documented caveat].

### 6.6 Build ↔ commit correlation — [documented — **corrects the brief**]
- `GET …/_apis/build/builds?api-version=7.1` **full documented query-parameter list**:
  `definitions, queues, buildNumber, minTime, maxTime, requestedFor, reasonFilter,
  statusFilter, resultFilter, tagFilters, properties, $top, continuationToken,
  maxBuildsPerDefinition, deletedFilter, queryOrder, branchName, buildIds,
  repositoryId, repositoryType`. **There is no `sourceVersion` parameter.**
  `sourceVersion` exists only as a **response field** on `Build`.
- Therefore the brief's `GET /build/builds?repositoryId={repoId}&sourceVersion={sha}`
  will not filter (unknown params are ignored [inference]). Correct recipe
  [inference built on documented params]:
  `GET /build/builds?repositoryId={repoId}&definitions={laneDefId}&minTime={t}&$top=25&queryOrder=queueTimeDescending`
  then match `build.sourceVersion == sha` client-side; the `build.complete`
  webhook (§2.5) makes most of this unnecessary because it carries
  `sourceVersion` and `definition.id` directly.
- For policy-driven PR builds the build's `reason` is `pullRequest` / `validateShelveset`
  family and `triggerInfo` carries PR context [documented Build fields] — use it to
  distinguish PR-CI builds from lane dispatches [inference].
- Branch-policy "Build validation" runs build the **merge commit**
  (`refs/pull/{id}/merge`-style synthetic), so `sourceVersion` of a policy build is
  the merge commit, not the PR head SHA — correlate policy builds to a PR via
  `triggerInfo`/`Build.reason`+`Build.repository`, or via the policy evaluations
  API (§7.3), not by naive `sourceVersion` equality with the head SHA
  [community-documented merge-build behavior; mapping is inference].

### Sources (§6)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/pipelines/create?view=azure-devops-rest-7.2
- https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/run-pipeline?view=azure-devops-rest-7.1 (+ Runs - Get)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/preview/preview?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/artifacts/get?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/build/artifacts/get-artifact?view=azure-devops-rest-7.1 (+ Artifacts - List)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/list?view=azure-devops-rest-7.1 (full param table — sourceVersion absence)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/build/timeline/get?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/update?view=azure-devops-rest-7.1 (BuildStatus `cancelling`)
- https://stackoverflow.com/questions/69012854/how-to-cancel-running-pipelines-using-azure-devops-restapi (+ 62044055)
- https://learn.microsoft.com/en-us/cli/azure/pipelines/build?view=azure-cli-latest (legacy queue surface)
- https://johnnyreilly.com/create-pipeline-with-azure-devops-api + https://stackoverflow.com/questions/78487743 (create-pipeline recipe + exact error)
- https://stackoverflow.com/questions/77928206/how-do-i-download-an-artifact-from-the-devops-run-artifacts-rest-api (runId==buildId, zip download)

---

## 7. Branch policies: build validation (+ onboarding scope)

### 7.1 Policy REST — [documented]
- `GET|POST https://dev.azure.com/{org}/{project}/_apis/policy/configurations?api-version=7.1`
  (create is `…/configurations?api-version=7.1-preview.1`-stripe acceptable; the
  documented samples use plain versions), `PUT|DELETE …/configurations/{id}`.
- Body shape: `{ "isEnabled": true, "isBlocking": true, "type": { "id": "<policy
  type GUID>" }, "settings": { …, "scope": [ { "repositoryId": null|"<repoGuid>",
  "refName": "refs/heads/main", "matchKind": "exact|prefix" } ] } }` [documented
  samples].
- `isBlocking=true` = "Required" in the UI; `isBlocking=false` = optional/advisory
  [documented field semantics].

### 7.2 Policy type ids — [documented (from official REST examples)]
| Policy | type id | settings seen in docs |
|---|---|---|
| **Build validation** | `0609b952-1397-4640-95ec-e00a01b2c241` | `buildDefinitionId`, `displayName`, `manualQueueOnly`, `queueOnSourceUpdateOnly`, `validDuration` (hours), `filenamePatterns` (where supported), `scope[]` |
| Minimum approval count | `fa4e907d-c16b-4a4c-9dfa-4906e5d171dd` | `minimumApproverCount`, `creatorVoteCounts`, `allowDownvotes`, `resetOnSourcePush`, `scope[]` |
| Required reviewer | not pinned during research — enumerate at runtime via `GET /_apis/policy/types` and match `displayName=="Required reviewers"` [inference; the az CLI exposes `az repos policy required-reviewer create`, so the type exists] |

**Onboarding rule** [inference per ADR-0024 §5]: forge's ONBOARDING may create
**Build validation** (pointing at the lane pipeline? **no —** the PR-CI policy
points at the humans' CI pipeline; the lane stays dispatch-only) and *may* read
everything else. Vote/required-reviewer/work-item-linking policies are the humans'
configuration — the doctor asserts their presence, never mutates them. PAT-level:
policy mutation needs project-level "Manage policies" permission, which the forge
service account should not have unless onboarding is being run [inference].

### 7.3 How policy status reflects build completion — [documented]
- `GET …/_apis/policy/evaluations?artifactId=vstfs:///CodeReview/CodeReviewId/{projectId}/{pullRequestId}&api-version=7.1-preview.1`
  → `PolicyEvaluationRecord[]`: `{ configuration (PolicyConfiguration),
  status (PolicyEvaluationStatus: queued | running | approved | rejected |
  …), completedDate, context (build linkage), _links, evaluationId }`
  [documented definitions].
- The artifactId template is **CodeReviewId**, *not* `vstfs:///Git/PullRequestId/…`
  — using the latter returns "Artifact id … does not exist" [documented template;
  the failure mode community-documented].
- Re-queue an expired/failed evaluation: `PATCH …/_apis/policy/evaluations/{evaluationId}?api-version=7.1-preview.1`
  [documented operation; community recipe]. For build-validation policies, the
  evaluation `context` carries the build id — the clean PR⇄build join for the
  reactive lane [inference].

### Sources (§7)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/policy/configurations/create?view=azure-devops-rest-7.1 (build-validation + approval examples with type ids)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/policy/evaluations/list | /get?view=azure-devops-rest-7.1
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/policy-configurations/get?view=azure-devops-rest-7.2 (scope/repo-level behavior)
- https://stackoverflow.com/questions/68758251 (build-validation settings body: manualQueueOnly, queueOnSourceUpdateOnly)
- https://codewrecks.com/post/azdo/api/reschedule-pr-check-with-api (evaluation requeue recipe, CodeReviewId artifactId)
- https://secopslog.com/courses/az-devops/azd-repos (policy builds merge commit behavior, community)
- https://github.com/github/awesome-copilot/blob/main/skills/azure-devops-cli/references/repos-and-prs.md (az repos policy surface incl. required-reviewer/work-item-linking types)

---

## 8. Limits, throttling, Server-vs-Services deltas

### 8.1 Rate limits (Services) — [documented]
- Model: **TSTU** (throughput service threshold unit) per identity, sliding
  **5-minute window**, global cap **200 TSTU**; normal activity < 10 TSTU/window,
  spikes to ~100 tolerated [documented].
- Progressive throttle: first **delays** responses (still HTTP 200, with
  `Retry-After` / `X-RateLimit-Delay`), then **blocks** with HTTP **429**
  `TF400733 …` [documented].
- Headers to monitor: `X-RateLimit-Remaining`, `X-RateLimit-Limit`; honor
  `Retry-After` [documented best practices].
- Identity on **Basic + Test Plans** access level gets raised limits — documented
  escape hatch for the forge service account if throttled [documented].
- HTTP-client policy [inference]: single retry honoring `Retry-After`, then surface;
  webhook path is unaffected (inbound).

### 8.2 Git limits — [documented]
- Repository: ≤ **250 GB** hard guidance (10 GB recommended for performance).
- **Push size ≤ 5 GB** per push (import via web above that); LFS blobs excluded.
- File size: **100 MB** current file size limit (merge conflicts/pushes with larger
  binaries are the pain point) [documented FAQ].
- Path limits: total path ≤ **32,766 chars**, component ≤ **4,096**; violations
  reject the push with `VS403729` (and sibling codes) naming the offending commit
  [documented error text].
- No documented per-push **file count** cap [documented by absence]; forge's
  publishes are tiny (a handful of files) — irrelevant in practice [inference].

### 8.3 Comment/thread caps — [documented]
- **500 comments per PR thread** (verbatim cap on the comments create operation).
- **1000 reviewers per PR**.
- Thread status enum surface: `unknown, active, fixed, wontFix, closed, byDesign` (§4.4).
- PR description ≤ 4000 chars on update [documented].

### 8.4 Markdown support — see §5.2 (PR threads render markdown, no API format
param; WIT comments take `format=markdown`; webhooks carry all three renderings).

### 8.5 TFVC vs Git — [documented]
Forge targets **git only** (`git.push` event, `TfsGit` repository type in build
resources). TFVC surfaces a different event family (`tfvc.checkin` "Code checked
in") and different repository type — the router must simply not match them
[documented events list; guard inference].

### 8.6 Azure DevOps Server (on-prem) deltas worth coding defensively
- Base URL `https://{instance}/{collection}` and per-version REST views
  (`azure-devops-server-rest-5.0|6.0|7.0|7.1`); **Server 2022 = API 7.1**, older
  servers cap lower — pin the client's api-version per connection, not globally
  [documented version views].
- The **Pipelines area exists on Server 2020+** (Run Pipeline documented for
  `azure-devops-server-rest-7.1` and 6.0) but `Pipelines - Create` samples on
  Server show `6.0-preview` behavior with the same repositoryName strictness —
  keep the create call behind a capability probe [documented views; probe inference].
- `resourceContainers.baseUrl` still resolves the instance URL in Server payloads —
  reuse for org-URL normalization [documented envelope].
- NTLM removal in git/libcurl is being rolled out for Server customers (2026
  advisory) — forge does REST-only, unaffected, but `forge doctor` should not
  recommend NTLM git remotes [community-documented advisory; relevance inference].
- Server webhooks lack the "supportedResourceVersions" spread of Services in some
  events; pin `resourceVersion: "1.0"` at subscription creation as the brief does
  and treat unknown newer fields as absent [inference].
- Preview stripes can behave differently on Server; prefer the GA stripe everywhere
  except the two preview-only APIs (WIT comments `7.1-preview.4`, policy
  evaluations `7.1-preview.1`) where Server 2022 exposes the same preview stripes
  [documented per-operation views; last clause inference].

### Sources (§8)
- https://learn.microsoft.com/en-us/azure/devops/integrate/concepts/rate-limits?view=azure-devops
- https://learn.microsoft.com/en-us/azure/devops/repos/git/limits (250 GB / 5 GB / path limits / VS403729)
- https://learn.microsoft.com/en-us/azure/devops/repos/git/howto?view=azure-devops (100 MB file limit)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-thread-comments/create?view=azure-devops-rest-7.1 (500 cap)
- https://learn.microsoft.com/en-us/azure/devops/repos/git/about-pull-requests?view=azure-devops (1000 reviewers)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/update?view=azure-devops-rest-7.1 (BuildStatus cancelling)
- https://learn.microsoft.com/en-us/rest/api/azure/devops/?view=azure-devops (server view matrix)

---

## 9. Prior art

### 9.1 qodo pr-agent / PR-Agent on Azure DevOps — [documented]
- Provider selection: `config.git_provider = "azure"`; auth via
  `azure_devops.pat` (PAT of a service identity) — same credential class forge uses.
- **PR CI on Azure Repos**: their pipeline template disables YAML `pr:` triggers
  with the explicit note *"Azure Repos does not honor YAML pr: triggers. Configure
  Build Validation via Branch Policies instead"* — independent confirmation of
  ADR-0024 §6's ground truth, from a vendor running this in production.
- **Comment commands**: PR-Agent's AzDO path is webhook-driven — a "Pull request
  commented on" service hook with Basic username/password (their docs recommend "a
  sporadic username/password pair", i.e. exactly forge's
  `FORGE_AZDO_WEBHOOK_USERNAME/PASSWORD` scheme), noting for that trigger "only API
  v2.0 is supported" in their server [vendor-documented caveat; treat as their
  deployment note, not an ADO constraint].
- Command mapping (their `/describe`, `/review`, `/improve`, `/ask`,
  `/update_changelog`) maps onto forge's plan/review/debug family; their
  incremental mode `/improve -i` ("analyze only changes made after the latest
  code-suggestions pass") is the same incremental-review pattern as forge's
  iteration diff (§4.3) [documented vendor docs].

### 9.2 Official Azure DevOps MCP Server — [documented, prior art only]
Microsoft ships a first-party Azure DevOps MCP Server (GA announced 2026-07-28;
public preview 2026-03). Per ADR-0021 §8.2 forge's durable core stays on
first-party REST; the MCP server is useful prior art for tool granularity (repo /
PR / work-item / pipeline tool families) and as a doctor-side probe idea — not a
dependency [vendor announcement; positioning per ADR].

### 9.3 Sync/migration tooling
- GitHub's `azure/ado-to-git` class of sync tools and Azure DevOps' own
  **Enterprise Live Migrations** (public preview 2026-08: continuous sync of
  repositories → GitHub, including PR metadata) demonstrate that webhook+PAT sync
  loops are the standard integration shape; none address forge's loop
  (plan→harness→publish), so no reusable component — pattern confirmation only
  [documented announcements; conclusion inference].

### Sources (§9)
- https://docs.pr-agent.ai/installation/azure (+ https://qodo-merge-docs.qodo.ai/installation/azure)
- https://github.com/qodo-ai/pr-agent (provider list incl. `azure`; automations docs)
- https://devblogs.microsoft.com/devops/enterprise-live-migrations-public-preview
- https://azurecharts.com/updates (Azure DevOps MCP Server GA 2026-07-28)

---

## 10. Fixtures for contract tests

**All fixtures below are SYNTHESIZED from the documented schema (§2/§4/§6) — to be
replaced by recorded lab payloads after live verification (AZ-4 runbook).** Values
are deterministic; GUIDs are pattern-valid. Trimmed to the fields forge normalizes
plus enough envelope to be realistic.

### 10.1 `git.push` — branch create (oldObjectId = 40 zeros)
```json
{
  "id": "6f2f6f2f-1111-2222-3333-444455556666",
  "eventType": "git.push",
  "publisherId": "tfs",
  "message": { "text": "pushed a new branch forge/wi-42 to Fabrikam / core", "html": "…", "markdown": "…" },
  "detailedMessage": { "text": "…", "html": "…", "markdown": "…" },
  "resource": {
    "commits": [{
      "commitId": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      "author": { "name": "Forge Bot", "email": "forge-bot@fabrikam.example", "date": "2026-09-15T10:00:00Z" },
      "committer": { "name": "Forge Bot", "email": "forge-bot@fabrikam.example", "date": "2026-09-15T10:00:00Z" },
      "comment": "forge: branch for WI-42",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_git/core/commit/a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    }],
    "refUpdates": [{
      "name": "refs/heads/forge/wi-42",
      "oldObjectId": "0000000000000000000000000000000000000000",
      "newObjectId": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    }],
    "repository": {
      "id": "1a2b3c4d-0000-0000-0000-000000000001",
      "name": "core",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001",
      "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" },
      "defaultBranch": "refs/heads/main",
      "remoteUrl": "https://dev.azure.com/fabrikam/Fabrikam/_git/core"
    },
    "pushedBy": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                  "uniqueName": "forge-bot@fabrikam.example" },
    "pushId": 1042,
    "date": "2026-09-15T10:00:01.1000000Z",
    "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/pushes/1042"
  },
  "resourceVersion": "1.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T10:00:01.2000000Z"
}
```

### 10.2 `git.push` — normal push (before/after SHAs)
```json
{
  "id": "6f2f6f2f-1111-2222-3333-444455557777",
  "eventType": "git.push",
  "publisherId": "tfs",
  "message": { "text": "Dev User pushed to forge/wi-42", "html": "…", "markdown": "…" },
  "resource": {
    "commits": [{
      "commitId": "b2c3d4e5f60718293a4b5c6d7e8f901234567890",
      "author": { "name": "Dev User", "email": "dev@fabrikam.example", "date": "2026-09-15T11:00:00Z" },
      "committer": { "name": "Dev User", "email": "dev@fabrikam.example", "date": "2026-09-15T11:00:00Z" },
      "comment": "address review: tighten guard",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_git/core/commit/b2c3d4e5f60718293a4b5c6d7e8f901234567890"
    }],
    "refUpdates": [{
      "name": "refs/heads/forge/wi-42",
      "oldObjectId": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      "newObjectId": "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
    }],
    "repository": {
      "id": "1a2b3c4d-0000-0000-0000-000000000001",
      "name": "core",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001",
      "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" },
      "defaultBranch": "refs/heads/main",
      "remoteUrl": "https://dev.azure.com/fabrikam/Fabrikam/_git/core"
    },
    "pushedBy": { "id": "bb22cc33-0000-0000-0000-0000000000bb", "displayName": "Dev User",
                  "uniqueName": "dev@fabrikam.example" },
    "pushId": 1043,
    "date": "2026-09-15T11:00:02.0000000Z",
    "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/pushes/1043"
  },
  "resourceVersion": "1.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T11:00:02.1000000Z"
}
```

### 10.3 `git.pullrequest.created`
```json
{
  "id": "70707070-aaaa-bbbb-cccc-ddddeeee0001",
  "eventType": "git.pullrequest.created",
  "publisherId": "tfs",
  "message": { "text": "Forge Bot created a pull request", "html": "…", "markdown": "…" },
  "resource": {
    "pullRequestId": 512,
    "status": "active",
    "creationDate": "2026-09-15T10:05:00.0000000Z",
    "title": "forge: WI-42 candidate (attempt 1)",
    "description": "Draft candidate from attempt base a1b2c3d4…",
    "sourceRefName": "refs/heads/forge/wi-42",
    "targetRefName": "refs/heads/main",
    "mergeStatus": "succeeded",
    "mergeId": "5e5e5e5e-0000-0000-0000-000000000005",
    "lastMergeSourceCommit": { "commitId": "b2c3d4e5f60718293a4b5c6d7e8f901234567890",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/commits/b2c3d4e5…" },
    "lastMergeTargetCommit": { "commitId": "c3d4e5f60718293a4b5c6d7e8f90123456789012", "url": "…" },
    "lastMergeCommit": { "commitId": "d4e5f60718293a4b5c6d7e8f9012345678901234", "url": "…" },
    "createdBy": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                   "uniqueName": "forge-bot@fabrikam.example",
                   "imageUrl": "https://dev.azure.com/fabrikam/_api/_common/identityImage?id=aa11bb22-0000-0000-0000-0000000000aa" },
    "repository": {
      "id": "1a2b3c4d-0000-0000-0000-000000000001",
      "name": "core",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001",
      "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" },
      "defaultBranch": "refs/heads/main",
      "remoteUrl": "https://dev.azure.com/fabrikam/Fabrikam/_git/core"
    },
    "reviewers": [ { "vote": 0, "id": "cc33dd44-0000-0000-0000-0000000000cc",
                     "displayName": "Dev User", "isContainer": false } ],
    "supportsIterations": true,
    "isDraft": true
  },
  "resourceVersion": "2.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T10:05:00.5000000Z"
}
```

### 10.4 `git.pullrequest.updated` (new push — the incremental-review trigger)
```json
{
  "id": "70707070-aaaa-bbbb-cccc-ddddeeee0002",
  "eventType": "git.pullrequest.updated",
  "publisherId": "tfs",
  "message": { "text": "Dev User updated pull request 512 (new push)", "html": "…", "markdown": "…" },
  "resource": {
    "pullRequestId": 512,
    "status": "active",
    "creationDate": "2026-09-15T10:05:00.0000000Z",
    "closedDate": null,
    "title": "forge: WI-42 candidate (attempt 1)",
    "sourceRefName": "refs/heads/forge/wi-42",
    "targetRefName": "refs/heads/main",
    "mergeStatus": "succeeded",
    "mergeId": "5e5e5e5e-0000-0000-0000-000000000005",
    "lastMergeSourceCommit": { "commitId": "e5f60718293a4b5c6d7e8f9012345678901234567", "url": "…" },
    "lastMergeTargetCommit": { "commitId": "c3d4e5f60718293a4b5c6d7e8f90123456789012", "url": "…" },
    "lastMergeCommit": { "commitId": "f60718293a4b5c6d7e8f901234567890123456789", "url": "…" },
    "createdBy": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                   "uniqueName": "forge-bot@fabrikam.example" },
    "repository": {
      "id": "1a2b3c4d-0000-0000-0000-000000000001",
      "name": "core",
      "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001",
      "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" }
    },
    "reviewers": [ { "vote": 10, "id": "cc33dd44-0000-0000-0000-0000000000cc",
                     "displayName": "Dev User", "isContainer": false } ],
    "commits": [ { "commitId": "e5f60718293a4b5c6d7e8f9012345678901234567", "url": "…" } ],
    "supportsIterations": true,
    "isDraft": true
  },
  "resourceVersion": "2.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T12:00:00.0000000Z"
}
```
(Subscription-side, this delivery corresponds to `notificationType: PushNotification`;
forge may subscribe once unfiltered and diff via the API — see §2.3.)

### 10.5 PR comment event (`ms.vss-code.git-pullrequest-comment-event`) — `/cancel` on a PR
```json
{
  "id": "80808080-aaaa-bbbb-cccc-ddddeeee0003",
  "eventType": "ms.vss-code.git-pullrequest-comment-event",
  "publisherId": "tfs",
  "message": { "text": "Dev User has added a pull request comment",
               "html": "Dev User has <a href=\"…/pullrequest/512?discussionId=77\">added</a> a pull request comment",
               "markdown": "Dev User has [added](…/pullrequest/512?discussionId=77) a pull request comment" },
  "resource": {
    "comment": {
      "id": 301,
      "parentCommentId": 0,
      "author": { "displayName": "Dev User", "id": "cc33dd44-0000-0000-0000-0000000000cc",
                  "uniqueName": "dev@fabrikam.example", "imageUrl": "…" },
      "content": "/cancel",
      "publishedDate": "2026-09-15T12:10:00.0000000Z",
      "lastUpdatedDate": "2026-09-15T12:10:00.0000000Z",
      "lastContentUpdatedDate": "2026-09-15T12:10:00.0000000Z",
      "commentType": "text",
      "_links": {
        "self": { "href": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/pullRequests/512/threads/77/comments/301" },
        "threads": { "href": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/pullRequests/512/threads/77" }
      }
    },
    "pullRequest": {
      "repository": { "id": "1a2b3c4d-0000-0000-0000-000000000001", "name": "core",
        "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" },
        "defaultBranch": "refs/heads/main" },
      "pullRequestId": 512,
      "status": "active",
      "createdBy": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                     "uniqueName": "forge-bot@fabrikam.example" },
      "title": "forge: WI-42 candidate (attempt 1)",
      "sourceRefName": "refs/heads/forge/wi-42",
      "targetRefName": "refs/heads/main",
      "mergeStatus": "succeeded",
      "lastMergeSourceCommit": { "commitId": "e5f60718293a4b5c6d7e8f9012345678901234567", "url": "…" },
      "lastMergeTargetCommit": { "commitId": "c3d4e5f60718293a4b5c6d7e8f90123456789012", "url": "…" },
      "lastMergeCommit": { "commitId": "f60718293a4b5c6d7e8f901234567890123456789", "url": "…" }
    }
  },
  "resourceVersion": "2.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T12:10:00.3000000Z"
}
```

### 10.6 `build.complete` — failed PR-CI build (debug_ci trigger)
```json
{
  "id": "90909090-aaaa-bbbb-cccc-ddddeeee0004",
  "eventType": "build.complete",
  "publisherId": "tfs",
  "message": { "text": "Build 20260915.3 failed", "html": "…", "markdown": "…" },
  "resource": {
    "_links": {
      "self": { "href": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/Builds/88231" },
      "web": { "href": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_build/results?buildId=88231" },
      "timeline": { "href": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/builds/88231/Timeline" }
    },
    "id": 88231,
    "buildNumber": "20260915.3",
    "status": "completed",
    "result": "failed",
    "queueTime": "2026-09-15T12:05:00.0000000Z",
    "startTime": "2026-09-15T12:05:10.0000000Z",
    "finishTime": "2026-09-15T12:11:42.0000000Z",
    "url": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/Builds/88231",
    "definition": { "id": 207, "name": "core-ci", "type": "build",
      "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" } },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "name": "Fabrikam" },
    "sourceBranch": "refs/pull/512/merge",
    "sourceVersion": "f60718293a4b5c6d7e8f901234567890123456789",
    "reason": "pullRequest",
    "triggerInfo": { "pr.number": "512" },
    "requestedFor": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                      "uniqueName": "forge-bot@fabrikam.example" },
    "repository": { "id": "1a2b3c4d-0000-0000-0000-000000000001", "type": "TfsGit",
                    "name": "core", "url": "https://dev.azure.com/fabrikam/Fabrikam/_git/core" }
  },
  "resourceVersion": "2.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T12:11:43.0000000Z"
}
```
**Handle with extra caution:** `sourceBranch: refs/pull/512/merge`, `reason:
pullRequest`, and the `triggerInfo` shape are **inferred conveniences**, not from a
documented sample (the documented full sample shows a `manual` build on
`refs/heads/main`; it carries `triggerInfo: {}`). The real triggerInfo keys for PR
builds must be confirmed in the AZ-4 lab run before the debug_ci correlator relies
on them (§6.6 third bullet).

### 10.7 `workitem.commented` — `/implement` command
```json
{
  "id": "a0a0a0a0-aaaa-bbbb-cccc-ddddeeee0005",
  "eventType": "workitem.commented",
  "publisherId": "tfs",
  "scope": "all",
  "message": { "text": "Task #142 (Ship the flux capacitor) commented on by Dev User.",
               "html": "…", "markdown": "…" },
  "resource": {
    "id": 142,
    "rev": 9,
    "fields": {
      "System.AreaPath": "Fabrikam",
      "System.TeamProject": "Fabrikam",
      "System.IterationPath": "Fabrikam\\Sprint 3",
      "System.WorkItemType": "Task",
      "System.State": "Approved",
      "System.CreatedDate": "2026-09-01T09:00:00.000Z",
      "System.CreatedBy": "Dev User",
      "System.ChangedDate": "2026-09-15T13:00:00.000Z",
      "System.ChangedBy": "dev@fabrikam.example",
      "System.Title": "Ship the flux capacitor",
      "System.History": "/implement please focus on the retry path"
    },
    "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/wit/workItems/142"
  },
  "resourceVersion": "1.0",
  "resourceContainers": {
    "collection": { "id": "cccc0000-0000-0000-0000-0000000000cc", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "account": { "id": "aaaa0000-0000-0000-0000-0000000000aa", "baseUrl": "https://dev.azure.com/fabrikam/" },
    "project": { "id": "9f8e7d6c-0000-0000-0000-000000000009", "baseUrl": "https://dev.azure.com/fabrikam/" }
  },
  "createdDate": "2026-09-15T13:00:00.1000000Z"
}
```
(No comment id exists in this payload — delivery key must be
`workitem:{id}:comment:{rev}` or content-hash; see §2.5.)

### 10.8 Pipeline run created (Runs API response — dispatch correlation)
```json
{
  "id": 99001,
  "name": "20260915.7",
  "state": "inProgress",
  "result": null,
  "pipeline": { "id": 207, "name": "forge-lane", "revision": 3,
                "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/Pipelines/207?revision=3" },
  "resources": {
    "repositories": {
      "self": { "id": "1a2b3c4d-0000-0000-0000-000000000001", "type": "azureReposGit",
                "name": "core", "refName": "refs/heads/forge/wi-42", "version": "e5f60718…" }
    }
  },
  "templateParameters": { "run_id": "run-7f3a", "attempt_base": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                          "driver": "claude-code", "model": "glm-5", "work_item_id": "142" },
  "variables": {},
  "url": "https://dev.azure.com/fabrikam/Fabrikam/_apis/Pipelines/207/runs/99001",
  "createdDate": "2026-09-15T13:10:00.0000000Z",
  "finishedDate": null
}
```
(`finalYaml` appears on Run objects once materialized; absent on create.)

### 10.9 Threads create — REQUEST body (forge → AzDO; inline finding, sticky)
```json
{
  "comments": [{
    "parentCommentId": 0,
    "content": "**forge review** — missing: retry guard swallows `TimeoutError` without re-raise (severity: must_fix)",
    "commentType": 1
  }],
  "status": 1,
  "threadContext": {
    "filePath": "/src/retry.py",
    "rightFileStart": { "line": 42, "offset": 1 },
    "rightFileEnd": { "line": 48, "offset": 20 }
  },
  "pullRequestThreadContext": {
    "changeTrackingId": 4,
    "iterationContext": { "firstComparingIteration": 2, "secondComparingIteration": 3 }
  }
}
```

### 10.10 Threads create — RESPONSE (documented sample shape, synthesized values)
```json
{
  "pullRequestThreadContext": {
    "changeTrackingId": 4,
    "iterationContext": { "firstComparingIteration": 2, "secondComparingIteration": 3 }
  },
  "id": 77,
  "publishedDate": "2026-09-15T14:00:00.000Z",
  "lastUpdatedDate": "2026-09-15T14:00:00.000Z",
  "comments": [{
    "id": 310,
    "parentCommentId": 0,
    "author": { "id": "aa11bb22-0000-0000-0000-0000000000aa", "displayName": "Forge Bot",
                "uniqueName": "forge-bot@fabrikam.example", "url": "…" },
    "content": "**forge review** — missing: retry guard swallows `TimeoutError` without re-raise (severity: must_fix)",
    "publishedDate": "2026-09-15T14:00:00.000Z",
    "lastUpdatedDate": "2026-09-15T14:00:00.000Z",
    "commentType": "text"
  }],
  "status": "active",
  "threadContext": {
    "filePath": "/src/retry.py",
    "rightFileStart": { "line": 42, "offset": 1 },
    "rightFileEnd": { "line": 48, "offset": 20 }
  },
  "properties": {},
  "isDeleted": false,
  "_links": {
    "self": { "href": "https://dev.azure.com/fabrikam/Fabrikam/_apis/git/repositories/1a2b3c4d-0000-0000-0000-000000000001/pullRequests/512/threads/77" }
  }
}
```

### 10.11 Push response — CAS failure (`staleObjectId`) — **extra caution: response
envelope synthesized from GitRefUpdateResult schema; the wrapping object (`value`
array vs top-level) for the pushes endpoint is NOT shown in documented samples —
confirm in the AZ-4 lab run**
```json
{
  "value": [{
    "repositoryId": "1a2b3c4d-0000-0000-0000-000000000001",
    "name": "refs/heads/forge/wi-42",
    "oldObjectId": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "newObjectId": "b2c3d4e5f60718293a4b5c6d7e8f901234567890",
    "isLocked": false,
    "updateStatus": "staleObjectId",
    "success": false,
    "customMessage": "The ref update failed because the old object id does not match the current tip."
  }]
}
```

### 10.12 Build timeline — trimmed (debug lane input)
```json
{
  "id": "3d3d3d3d-0000-0000-0000-00000000003d",
  "changeId": 1,
  "records": [
    { "id": "11110000-0000-0000-0000-000000000011", "type": "Stage", "name": "__default",
      "state": "completed", "result": "failed", "order": 1,
      "startTime": "2026-09-15T12:05:10.000Z", "finishTime": "2026-09-15T12:11:42.000Z" },
    { "id": "22220000-0000-0000-0000-000000000022", "type": "Job", "name": "forge-harness",
      "state": "completed", "result": "failed", "order": 2,
      "startTime": "2026-09-15T12:05:12.000Z", "finishTime": "2026-09-15T12:11:40.000Z",
      "log": { "id": 3, "type": "Container",
               "url": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/builds/88231/logs/3" } },
    { "id": "33330000-0000-0000-0000-000000000033", "type": "Task", "identifier": "harness.ClaudeCodeDriver",
      "name": "Driver: forge harness", "state": "completed", "result": "failed", "order": 3,
      "errorCount": 1, "warningCount": 0,
      "issues": [ { "type": "error", "message": "##[error]harness exit code 1: driver crashed after 12 steps" } ],
      "startTime": "2026-09-15T12:05:20.000Z", "finishTime": "2026-09-15T12:11:38.000Z",
      "log": { "id": 5, "type": "Container",
               "url": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/builds/88231/logs/5" } }
  ],
  "url": "https://dev.azure.com/fabrikam/9f8e7d6c-0000-0000-0000-000000000009/_apis/build/builds/88231/timeline"
}
```
(`identifier`, `errorCount`/`warningCount`, and `issues[].type` spellings follow the
documented TimelineRecord/TimelineIssue definitions; values synthesized.)

**Fixture inventory** — 12 files for `tests/fixtures/azure_payloads/`:
`push_branch_create.json`, `push_normal.json`, `pr_created.json`,
`pr_updated_push.json`, `pr_commented_on.json`, `build_complete_failed.json`,
`workitem_commented_implement.json`, `pipeline_run_created.json`,
`thread_create_request.json`, `thread_create_response.json`,
`push_response_stale_object_id.json`, `build_timeline_failed.json`.
Extra-caution fixtures: **10.6** (PR-build triggerInfo/sourceBranch inferred), **10.11**
(push-response envelope shape unverified).