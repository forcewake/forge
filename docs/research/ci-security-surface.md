# CI Pipeline + Security Surface: GitLab CE and GitHub — forge Implementation Reference (2026)

Implementation reference for forge's pipeline-debugging agent and security-triage agent on
both providers. Facts from official docs fetched 2026-09-14; each fact tagged
[documented] / [inference]. GitLab tier mapping: "Free" tier feature set ≡ GitLab CE
(CE is the self-managed packaging of Free; docs pages state "Tier: Free/Premium/Ultimate"
for self-managed installs) [documented; tier lines quoted per page]. GitHub current API
version header observed in docs samples: `2026-03-10`.

---

## 1. Pipeline/run APIs forge should integrate

### 1.1 GitLab

Auth: `PRIVATE-TOKEN: <token>` header; all CI/CD read endpoints below are Free tier
[documented].

**Pipeline list + detail**

- `GET /projects/:id/pipelines` — filter params: `scope` (`running|pending|finished|
  branches|tags`), `status`, `source`, `ref`, `sha`, `username`, `yaml_errors`,
  `updated_after/before`, `created_after/before`, `name`, `order_by`, `sort`, plus
  `page/per_page` [documented].
  - `source` values include: `push`, `web`, `trigger`, `schedule`, `api`, `external`,
    `pipeline`, `chat`, `webide`, `merge_request_event`, `parent_pipeline`, `security`
    [documented enum in pipelines API].
- `GET /projects/:id/pipelines/:pipeline_id` returns: `id`, `iid`, `project_id`, `name`,
  `sha`, `ref`, `status`, `source`, `before_sha`, `tag`, `yaml_errors`, `user`,
  `started_at`, `finished_at`, `committed_at`, `duration`, `queued_duration`, `coverage`,
  `detailed_status` (group/label/tooltip/details_path), `web_url`, `archived`
  [documented, quoted example]. `merge_request` (object with `iid`) is present on pipeline
  detail for MR pipelines [documented; used to correlate pipeline→MR].
- Jobs: `GET /projects/:id/pipelines/:pipeline_id/jobs` (params `scope[]`,
  `include_retried`) and project-wide `GET /projects/:id/jobs`. Bridge jobs (child
  pipeline triggers) come from `GET /projects/:id/pipelines/:pipeline_id/bridges`, whose
  objects include `downstream_pipeline` `{id, project_id, sha, ref, status, web_url}`
  [documented].
- Retried job correlation: use `include_retried=true` to get superseded runs, else the
  default hides them [documented].

**MR pipelines vs branch pipelines vs merged-results / merge trains**

- A merge request pipeline runs on the source branch contents only (ignores target
  branch) and is labeled `merge request`; branch (push) pipelines run on pushes to the
  branch. Both can exist for the same SHA [documented].
- MR pipelines require `workflow:rules` / job `rules` matching
  `CI_PIPELINE_SOURCE == "merge_request_event"` [documented].
- All pipelines for an MR: `GET /projects/:id/merge_requests/:merge_request_iid/pipelines`
  (returns pipeline objects incl. `source`) [documented]. Create an MR pipeline on demand:
  `POST /projects/:id/merge_requests/:merge_request_iid/pipelines` with optional
  `async` [documented].
- **Merged results pipelines** (test the merge of source+target) and **merge trains** are
  **Premium/Ultimate**, not CE [documented, merge trains page "Tier: Premium, Ultimate"].
  Merge train semantics: queued MRs get car pipelines against combined state; a failing
  car ejects its MR and restarts pipelines for MRs queued after it [documented].
  - **forge consequence [inference]:** on CE a green MR pipeline never proves the merged
    state. If forge is the merging actor on CE it should run its own verification against
    a merged ref (e.g., a pipeline triggered on a temp merge commit) before merging.

**Child pipeline discovery/correlation**

- Trigger/bridge jobs with `include:` + `strategy: depend` create child (same project) or
  multi-project pipelines [documented].
- Discovery: from the parent pipeline call
  `GET /projects/:id/pipelines/:pipeline_id/bridges`; each bridge carries
  `downstream_pipeline`. Walk recursively for deep hierarchies [documented].
- Child pipelines report `source: parent_pipeline` in pipelines listings; multi-project
  downstreams are linked from the upstream pipeline [documented value of
  `CI_PIPELINE_SOURCE`; enumeration via bridges is the reliable API path].
- `target_branch` filtering: pipelines list has no `target_branch` param — filter by
  `ref`/`sha` and join with the MR API's `target_branch` [documented params + inference
  on join strategy].

**Test reports (pipeline-level, no artifact parsing needed)**

- Requires `artifacts:reports:junit` in the test job [documented]. All tiers.
- `GET /projects/:id/pipelines/:pipeline_id/test_report` →
  `{total_time, total_count, success_count, failed_count, skipped_count, error_count,
  test_suites: [{name, total_time, total_count, success_count, failed_count,
  skipped_count, error_count, test_cases: [{status, name, classname, execution_time,
  system_output, stack_trace}]}]}` [documented, quoted].
- `GET .../test_report_summary` → `{total: {time, count, success, failed, skipped, error,
  suite_error}, test_suites: [{name, ..., build_ids, suite_error}]}` [documented,
  quoted]. `GET .../test_report_builds` lists per-build reports [documented endpoint].
- MR test view compares head vs base: newly failed / newly errored / existing / resolved,
  plus "Failed N time(s) in <default_branch> in the last 14 days" [documented].

**Sources:** docs.gitlab.com/api/pipelines/, docs.gitlab.com/api/jobs/,
docs.gitlab.com/api/job_artifacts/, docs.gitlab.com/ci/pipelines/merge_request_pipelines/,
docs.gitlab.com/ci/pipelines/merge_trains/, docs.gitlab.com/ci/pipelines/downstream_pipelines/,
docs.gitlab.com/ci/testing/unit_test_reports/, docs.gitlab.com/api/merge_requests/

### 1.2 GitHub

**Check runs (the native "job result + inline diagnostics" primitive)**

- `POST/PATCH /repos/{owner}/{repo}/check-runs[/{check_run_id}]` — create/update.
  **Write is only available to GitHub Apps** (`checks:write`); PATs can read
  [documented]. Statuses: `queued, in_progress, completed` (+ `waiting, pending,
  requested` — Actions-only) [documented]. Conclusions: `action_required, cancelled,
  failure, neutral, success, skipped, stale, timed_out` (only GitHub sets `stale`)
  [documented].
- `output` object: `{title, summary, text, annotations_count, annotations_url}`
  [documented].
- Annotations: `GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations`;
  annotation object: `{path, start_line, end_line, start_column, end_column,
  annotation_level (notice|warning|failure), message, title, raw_details}` [documented].
  **Limits: max 50 annotations per request; update calls append to existing
  annotations.** GitHub Actions steps are capped at 10 warning + 10 error annotations
  per step [documented].
- `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` — all check runs on a SHA (the
  PR-head lookup forge needs); check suites via
  `GET /repos/{owner}/{repo}/check-suites/{check_suite_id}/check-runs` [documented].

**Workflow runs / jobs / steps**

- `GET /repos/{owner}/{repo}/actions/runs` filters: `head_sha`, `branch`, `event`,
  `status`, `actor`, `created`, `check_suite_id`, `exclude_pull_requests`; **capped at
  1,000 results per search** [documented]. Run object includes `id, name, path
  (.github/workflows/x.yml@ref), event, status, conclusion, head_branch, head_sha,
  run_number, run_attempt, triggering_actor, pull_requests[], jobs_url, logs_url,
  check_suite_id, run_started_at` [documented, quoted].
- `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs?filter=latest|all` — job object
  includes `steps: [{number, name, status, conclusion, started_at, completed_at}]`,
  `conclusion`, `runner_name`, `labels` [documented, quoted]. `filter=latest` hides
  re-run attempts — pass `all` when forensics [documented].
- Re-run: `POST /repos/{owner}/{repo}/actions/jobs/{job_id}/rerun` with optional
  `enable_debug_logging`; `actions:write` [documented]. Re-run failed jobs:
  `POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs` [documented].
- Correlate run→PR via `pull_requests[]` in the run object or via head SHA [documented].
- Code scanning alerts API (pipeline-produced findings) → §3; merge-group semantics → §6.

**Sources:** docs.github.com/en/rest/checks/runs, docs.github.com/en/rest/actions/workflow-runs,
docs.github.com/en/rest/actions/workflow-jobs (all apiVersion=2022-11-28)

---

## 2. Failure diagnostics ingestion

### 2.1 GitLab

- **Job log:** `GET /projects/:id/jobs/:job_id/trace` — returns the raw log (plain text
  with ANSI escapes) with 200 [documented]. There is no server-side JSON log format; the
  agent must strip ANSI [inference].
- **Structured failure metadata:** job object carries `status` and a machine
  `failure_reason` (e.g. `script_failure`, `runner_system_failure`, `stuck_or_timeout_failure`)
  [documented jobs API].
- **Test failures:** prefer the test-report endpoints (§1.1) — `system_output` +
  `stack_trace` per test case are the structured failure messages, no log parsing needed
  [documented].
- **Artifacts:** single file out of a job archive:
  `GET /projects/:id/jobs/:job_id/artifacts/*artifact_path`
  (e.g. `.../artifacts/gl-sast-report.json` — the docs' own example). By ref+job name:
  `GET /projects/:id/jobs/artifacts/:ref_name/raw/*artifact_path?job=name` (optional
  `search_recent_successful_pipelines`). Archive: `GET .../jobs/:job_id/artifacts`.
  File listing: `GET .../jobs/:job_id/artifacts/tree?path=&recursive=`. Keep artifacts
  from expiring: `POST .../jobs/:job_id/artifacts/keep` [documented]. Note: report
  artifacts are not shown in the UI artifacts browser unless their filenames are also in
  `artifacts:paths` — the path-based API still works for files in the archive
  [documented]. forge's generated templates should emit `artifacts:paths` alongside
  `artifacts:reports:*` so any tool can fetch them [inference].
- **RCA inputs checklist [inference]:** pipeline `detailed_status` + `yaml_errors`;
  failed jobs with `failure_reason` + `allow_failure`; trace tails; JUnit
  `system_output`/`stack_trace`; selected artifacts; child pipelines via `bridges`; job
  `runner` info for infra-caused failures (retry classification).

### 2.2 GitHub

- **Check-run annotations are the inline diagnostics channel:** each annotation is a
  path/line-anchored failure message; a debugging agent reads `annotations_url` per
  check run, plus `output.summary/text` [documented]. 50/request pagination, appended
  via repeated PATCHes [documented].
- **Job logs:** `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs` → **302 redirect**
  to a plain-text log URL; the `Location` URL **expires after 1 minute** — follow
  immediately [documented]. Whole-run logs zip: `GET /repos/{owner}/{repo}/actions/runs/
  {run_id}/logs` (`logs_url` on the run object) [documented]. Requires `actions:read`
  [documented].
- **Step-level localization:** the failing `step.conclusion` in the job object narrows
  the log slice before ingestion [documented fields; slicing is forge logic
  (inference)].
- **forge writes RCA back** by updating its own check run (`PATCH .../check-runs/{id}`
  with `output.summary`/`output.text` + annotations) — this is the natural place to
  publish a root-cause analysis [inference].

**Sources:** docs.gitlab.com/api/jobs/, docs.gitlab.com/api/job_artifacts/,
docs.github.com/en/rest/actions/workflow-jobs, docs.github.com/en/rest/checks/runs

---

## 3. Security findings ingestion

### 3.1 GitLab CE — exactly what is free

**Report JSON format (the artifact contract)** [documented, security scanner integration
docs]:

- Top level: `version` (schema semver), `scan` `{analyzer{id,vendor,name,version},
  scanner{id,vendor,name,version}, type, start_time, end_time, status,
  messages?}`, `vulnerabilities[]`, `remediations[]`.
- Vulnerability fields: `id` (UUID, scanner-assigned), `category` (`sast`,
  `dependency_scanning`, `container_scanning`, `secret_detection`, `dast`, ...), `name`,
  `message`, `description`, `severity` — enum **`Info, Unknown, Low, Medium, High,
  Critical`** — `solution`, `location` (category-specific: e.g. SAST →
  `{file, start_line, end_line, ...}`; dependency scanning → `{file, dependency{package
  {name}, version}}`), `identifiers[] {type, name, value, url}`, `links[]`, `cve`
  (deprecated alias of name).
- `remediations[] {summary, fixes[], diff}` (diff-based auto-remediation).
- Schemas: `gitlab-org/security-products/security-report-schemas` (JSON Schema per
  report type; GitLab validates ingested reports against vendored schema versions)
  [documented].

**Tier matrix (current docs, 2026)** [documented per page]:

| Scanner | Job/template runs in Free/CE? | Report artifact | UI (Security tab / MR widget / Vulnerability Report) |
|---|---|---|---|
| SAST (Semgrep analyzer) | Yes | `gl-sast-report.json` | Ultimate only |
| Secret Detection | Yes (moved to Free in 13.3) | `gl-secret-detection-report.json` | Ultimate only ("run scans and view ... JSON report artifacts in any GitLab tier") |
| Container Scanning (Trivy) | Yes (moved to Free in 15.0) | `gl-container-scanning-report.json` | Ultimate only |
| Dependency Scanning | **No — Ultimate** (docs page: "Tier: Ultimate"; analyzer replaced by Trivy-based one in 18.x, tier unchanged) | `gl-dependency-scanning-report.json` | Ultimate only |
| DAST / IaC / API+coverage fuzzing | No — Ultimate | `gl-dast-report.json`, ... | Ultimate only |

- **The pipeline Security tab, MR security widget, Vulnerability Report, Security
  Dashboard and all vulnerability state/dismiss APIs are Ultimate.** In CE the artifacts
  exist as ordinary job artifacts and must be fetched and parsed by the integrator
  [documented tier lines; the "forge parses artifacts itself" architecture is inference].
- Fetch recipe [documented endpoints]: `GET /projects/:id/pipelines/:pipeline_id/jobs` →
  find job by name (`sast`, `secret_detection`, `container_scanning`) →
  `GET /projects/:id/jobs/:job_id/artifacts/gl-*-report.json`.
- Vulnerability REST APIs (`GET /vulnerabilities/:id`,
  `POST /vulnerabilities/:id/dismiss|confirm|resolve|revert`) are **Ultimate**
  [documented]. Do not code against them for CE targets.
- Secret push protection and client-side secret detection exist but are tier-gated
  separately; pipeline secret detection coverage notes (branch vs MR scans,
  `SECRET_DETECTION_HISTORIC_SCAN`) are documented [documented].

### 3.2 GitHub

**Code scanning alerts** [documented, REST page quoted]:

- `GET /repos/{owner}/{repo}/code-scanning/alerts` (filters `state`
  `open|closed|dismissed|fixed`, `severity` `critical|high|medium|low|warning|note|error`,
  `tool_name`/`tool_guid`, `ref` (`refs/pull/<n>/merge` for PR results), `pr`);
  `GET /repos/{owner}/{repo}/code-scanning/alerts/{alert_number}`;
  `GET .../alerts/{alert_number}/instances` (`instances_url`).
- Alert fields: `number`, `state`, `fixed_at`, `created_at`, `updated_at`,
  `rule {id, name, severity, security_severity_level, description, full_description,
  tags, help, help_uri}`, `tool {name, guid, version}`,
  `most_recent_instance {ref, analysis_key, category, environment, commit_sha, state,
  message{text}, location{path, start_line, end_line, start_column, end_column},
  classifications}`, `dismissed_by/at/reason/comment`, `url`, `html_url`,
  `instances_url`, `repository`.
- **403 "GitHub Advanced Security is not enabled for this repository"** is the documented
  failure when the repo lacks the feature (public repos: free; private repos need GitHub
  Code Security, formerly GHAS) [documented].
- Permissions: classic `security_events` scope; fine-grained/App **"Code scanning
  alerts" (read/write)** [documented].
- Upload path for forge-generated findings: `POST /repos/{owner}/{repo}/code-scanning/
  sarifs` (SARIF analysis upload; creates alerts) [documented].

**Secret scanning alerts** [documented]:

- `GET /repos/{owner}/{repo}/secret-scanning/alerts[/{alert_number}]`;
  `GET .../alerts/{alert_number}/locations` (`locations_url`).
- Fields: `number`, `secret_type`, `secret_type_display_name`, `secret`, `state`
  (`open|resolved`), `resolution`, `resolved_by/at`, `validity`
  (`active|inactive|unknown`), `publicly_leaked`, `multi_repo`, `push_protection_bypassed`,
  `html_url`, timestamps.
- Tier: public repos free; private repos require **GitHub Secret Protection** [documented].

**Dependabot alerts** [documented]:

- `GET /repos/{owner}/{repo}/dependabot/alerts[/{alert_number}]`.
- Fields: `number`, `state` (`open|dismissed|fixed`), `dependency {package {ecosystem,
  name}, scope, manifest_path}`, `security_advisory {ghsa_id, cve_id, summary,
  description, severity, cvss, ...}`, `security_vulnerability {package,
  vulnerable_version_range, first_patched_vulnerability_identifier}`, `url`, `html_url`,
  `dismissed_by/at/reason/comment`, `auto_dismissed_at`, timestamps.
- **Free for all repositories** (not GHAS-gated; dependency graph required) [documented —
  GitHub security features overview + 2023 GA-of-alerts-for-all changelog].

**GitHub App integration recipe** [documented permission names; App wiring is inference]:

- Repo permissions: `Checks: rw`, `Actions: r` (`w` to rerun), `Code scanning alerts:
  rw`, `Secret scanning alerts: rw`, `Dependabot alerts: rw`, `Pull requests: rw`,
  `Contents: r`, `Commit statuses: r` (`statuses:write` only if forge supplies
  required-check sources). Webhooks: `check_run`, `check_suite`, `workflow_job`,
  `code_scanning_alert`, `secret_scanning_alert`, `dependabot_alert`, `merge_group`,
  `pull_request`.

**Sources:** docs.gitlab.com/development/integrations/secure/,
docs.gitlab.com/user/application_security/{sast,secret_detection,dependency_scanning,
container_scanning}/, docs.gitlab.com/api/vulnerabilities/,
docs.github.com/en/rest/code-scanning/code-scanning,
docs.github.com/en/rest/secret-scanning/secret-scanning,
docs.github.com/en/rest/dependabot/alerts,
docs.github.com/en/code-security/getting-started/github-security-features

---

## 4. Security triage flows

### 4.1 GitLab CE: what triage is even possible

- **Native triage state does not exist in CE.** Vulnerability report, per-vulnerability
  status changes (dismiss/confirm/resolve), dismissal reasons, and the MR security
  widget are Ultimate [documented]. There is no "comment on a finding" API at any tier —
  even Ultimate triage is state+comment fields, not discussions [documented API shape].
- **forge-side triage model for CE [inference, engineering]:**
  1. Key findings by a stable fingerprint forge computes itself: `category` +
     primary `identifiers[].value` + `location` hash (the schema's `id` is
     scanner-assigned per scan and not stable across runs for all scanners
     [documented schema; instability is inference]). GitLab Ultimate computes its own
     location/track fingerprints internally — CE users get none of that [documented].
  2. Record triage verdicts (false positive, accepted risk, fixed-in) in forge's DB;
     the finding JSON's `severity`, `identifiers`, `location`, `solution` are the
     display inputs.
  3. Surface triage in the forge UI and on the MR as comments/notes referencing the
     fingerprint; link fix MRs by branch/commit reference.
  4. Optionally re-emit a filtered "post-triage" report artifact from a forge job so
     humans can download a cleaned `gl-*-report.json` [inference].
- Fix linking: security report `remediations[].diff` (analyzer-proposed patches) can seed
  a fix MR; MR linkage then lives in forge's store [documented schema + inference].

### 4.2 GitHub: dismissal + comments via one PATCH each

- **Code scanning alert triage:** `PATCH /repos/{owner}/{repo}/code-scanning/alerts/
  {alert_number}` with `{state: "dismissed", dismissed_reason: "false positive" |
  "won't fix" | "used in tests", dismissed_comment: "<text>", create_request?}` —
  `dismissed_reason` is **required** when dismissing; `create_request: true` routes
  through alert-dismissal approval where configured; reopen with `state: "open"`;
  `assignees` supported on the same call. Requires "Code scanning alerts" write /
  `security_events` [documented]. There is **no free-form discussion endpoint** on the
  alert itself — the only text field is `dismissed_comment`; richer rationale belongs in
  a linked issue/PR that forge creates [documented field set + inference on UX].
- **Secret scanning alert triage:** `PATCH .../secret-scanning/alerts/{alert_number}`
  with `{state: "resolved", resolution: "false_positive" | "wont_fix" | "revoked" |
  "used_in_tests", resolution_comment}` [documented]. Note underscore separators
  (vs. spaces on code scanning) [documented].
- **Dependabot alert triage:** `PATCH .../dependabot/alerts/{alert_number}` with
  `{state: "dismissed", dismissed_reason: "fix_started" | "inaccurate" |
  "no_bandwidth" | "not_used" | "tolerable_risk", dismissed_comment}` [documented].
- Alert visibility permission: users/teams need write access to the repo (or be granted
  security-alert access; org-level listing needs owner/security manager)
  [documented].
- **Recommended forge flow [inference]:** triage agent reasons over
  `most_recent_instance` + `rule.help`, then either (a) opens a fix branch/PR (contents
  write) and comments the PR link via `dismissed_comment`, or (b) dismisses with an
  explicit reason + machine-written rationale. Never dismiss without a comment —
  `dismissed_comment` is the audit trail humans see in the UI.

**Sources:** docs.github.com/en/rest/code-scanning/code-scanning,
docs.github.com/en/rest/secret-scanning/secret-scanning,
docs.github.com/en/rest/dependabot/alerts, docs.gitlab.com/api/vulnerabilities/,
docs.gitlab.com/development/integrations/secure/

---

## 5. Inline review comments (GitHub side; GitLab skipped)

forge already posts GitLab MR notes/discussions; the only version-relevant note: MR list
filter `wip` was deprecated in GitLab 19.0 in favor of `draft` [documented].

**GitHub review + inline comments (REST)** [documented]:

- `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` — body `{commit_id, event:
  "APPROVE" | "REQUEST_CHANGES" | "COMMENT", body, comments: [{path, position?} |
  {path, side: "LEFT"|"RIGHT", start_side?, line, start_line?, body}]}`. Omitting
  `event` creates a **pending review**; submit later with
  `PUT /repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}` (update) or
  `POST .../reviews/{review_id}/events`; delete pending with `DELETE .../reviews/
  {review_id}`. `pull_requests: write` [documented].
- Standalone inline comment: `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments`;
  reply: `POST .../pulls/{pull_number}/comments/{comment_id}/replies` [documented].
- Dismiss a review: `PUT /repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/
  dismissals` `{message}` [documented].
- List review comments: `GET .../pulls/{pull_number}/reviews/{review_id}/comments`
  [documented].
- **Review-thread resolution is GraphQL-only** — no REST endpoint exists. Query
  `repository.pullRequest.reviewThreads {id, isResolved, isOutdated, path, line,
  comments{...}}`; mutate with `resolveReviewThread` / `unresolveReviewThread`
  (`threadId`, `PRRT_…` node id) [documented GraphQL schema; widely corroborated].
  forge's "readonly review" agent should return thread IDs it created so a later
  resolve-capable pass can close them [inference].
- **AI review summarization: no documented public REST endpoint for third-party apps.**
  Research found no official REST route (e.g. no documented `generate-ai-summary`) on
  the Pull requests REST reference; AI PR summaries and Copilot code review are
  first-party Copilot features (Copilot can be assigned as a reviewer —
  `copilot-pull-request-reviewer` — and rulesets can auto-request it), not available to
  external Apps. forge must generate summaries itself [research finding: absence;
  inference on the conclusion].

**Sources:** docs.github.com/en/rest/pulls/reviews, docs.github.com/en/rest/pulls/pulls,
docs.github.com/graphql (reviewThreads / resolveReviewThread)

---

## 6. Required checks / quality gates

### 6.1 GitHub: rulesets, required checks, merge queue

- **Rulesets** supersede classic branch protection; rule types relevant to forge
  [documented, rules reference]: *Require status checks to pass before merging* (with
  "require branches to be up to date", and **pinning the expected source App** —
  statuses from an integration with `statuses:write`), *Require merge queue*, *Require a
  pull request before merging* (approvals, dismiss-stale, **required review-thread
  resolution**, "require approval of the most recent reviewable push"), *Require code
  scanning results* (tool + severity threshold), *Require code quality results*,
  *Restrict code coverage* (minimum % / max drop), plus force-push/linear-history/file
  rules.
- **Plan availability:** branch protection / protected branches: free on **public**
  repos; private repos need Pro/Team/Enterprise [documented]. Merge queue on private
  repos is Enterprise-Cloud-gated (Team does not get it); public org-owned repos have it
  on any plan [documented on GHES/older docs + 2026 third-party corroboration; exact
  current plan matrix should be verified in the target org at provisioning time —
  flagged].
- **Merge queue semantics** [documented]: PR is added after required branch checks pass;
  GitHub creates a merge group on a temporary branch
  (`gh-readonly-queue/{base_branch}/...` prefix observed in practice) and fires the
  **`merge_group`** webhook / Actions event; **only required status checks run on the
  group** — workflows must add `merge_group:` to `on:` or required checks never report
  and the queue stalls (the classic misconfiguration). Groups can batch multiple PRs
  ("Only merge non-failing PRs" toggle controls grouping optimism); a failed group
  ejects its PR and remaining groups rebuild. Settings: merge method, build concurrency
  (1–100 merge_group dispatches), group min/max size, wait time, **status check
  timeout**.
- **forge as a gate [documented mechanism + inference wiring]:** forge creates check runs
  under a stable name from its App; admins add that check (optionally App-pinned) to
  required status checks — optionally only for `merge_group` contexts. forge's quality
  contract becomes the enforced gate without any custom status machinery.

### 6.2 GitLab CE: one gate only — "pipelines must succeed"

- Project setting **"Pipelines must succeed"**: API fields
  `only_allow_merge_if_pipeline_succeeds` and `allow_merge_on_skipped_pipeline` (skipped
  pipelines block merge by default when the setting is on) [documented, projects API +
  auto-merge docs]. Works for external-CI pipelines too [documented]. Auto-merge
  (`merge_when_pipeline_succeeds` on the MR) then merges on green [documented].
- **No per-check required statuses in CE.** Status checks (external status checks) are
  **Ultimate** on current docs (historically Premium) — even there, third-party services
  register project-side and report via
  `POST /projects/:id/merge_requests/:merge_request_iid/status_checks`
  `{external_status_check_id, status}`, with a "Status checks must succeed" merge option
  [documented]. CE users: none of this. Approval rules beyond basics, merge trains,
  merged-results pipelines, and security/pipeline execution policies are also paid-tier
  [documented].
- **forge-side workaround (forge's own quality contract) [inference, engineering]:**
  inject a forge gate job into every project pipeline (include template via project CI
  config that forge writes; there are no org-level enforcement pipelines in CE) whose
  exit code encodes forge's verdict (tests pass, secrets absent, contract checks green).
  Combine with `only_allow_merge_if_pipeline_succeeds=true` so a red gate blocks merge.
  MR-level signals beyond that (approvals, external checks) cannot be enforced on CE —
  forge must present them advisorially and record the decision itself.

**Sources:** docs.github.com/en/repositories/configuring-branches-and-merges-in-your-
repository/managing-rulesets/available-rules-for-rulesets, docs.github.com
(merge queue management + GHES 3.13 pages), docs.github.com protected-branches docs,
docs.gitlab.com/user/project/merge_requests/auto_merge, docs.gitlab.com/api/projects/,
docs.gitlab.com/user/project/merge_requests/status_checks/

---

## 7. What an "everything" CI integration still can't do

**GitLab CE**

- No security findings store: pipeline Security tab, MR security widget, Vulnerability
  Report/Dashboard, and `/vulnerabilities` APIs (dismiss/confirm/resolve) are Ultimate —
  forge must parse raw `gl-*-report.json` artifacts and own all triage state externally
  [documented].
- Dependency Scanning, DAST, IaC scanning, fuzzing templates are Ultimate; only SAST,
  secret detection, container scanning run for free [documented].
- No merged-results pipelines / merge trains (Premium) — a green MR pipeline does not
  test the merge commit [documented].
- No external status checks (Ultimate) and no required-check granularity — the only CE
  merge gate is the pipeline [documented].
- No enforcement policies (scan execution / pipeline execution = Ultimate), no approval
  rules, no compliance framework gates [documented].
- GitLab Duo agentic features (SAST auto-fix MRs, CI expert agent; 18.8–19.x) are
  first-party Ultimate — not forge's integration surface [documented].

**GitHub**

- Code scanning and secret scanning alert APIs return **403 when the feature isn't
  enabled**; private repos need paid Code Security / Secret Protection SKUs (public
  repos free) [documented]. forge must degrade gracefully per repo.
- Check-run **write is App-only** — forge must ship as a GitHub App, not a PAT
  [documented].
- Branch protection/rulesets/merge queue are plan-gated on private repos (merge queue:
  Enterprise Cloud for private) [documented + flagged corroboration].
- Annotations are capped (50/request, 10 warn + 10 error per Actions step); required
  checks stall if the workflow lacks `merge_group:` triggers [documented].
- Review-thread resolution needs GraphQL; no REST [documented]. AI PR summaries / Copilot
  review are not exposed to third-party Apps [research finding: absence].
- Rate limiting + pagination differ per platform (GitLab offset/keyset params; GitHub
  Link-header cursors and 1,000-result caps on filtered run searches) [documented
  mechanisms; forge should treat both as first-class].

**Bottom line [inference]:** a "complete" CI integration can ingest everything on both
forges (logs, test reports, artifacts, annotations, alert APIs) but cannot outsource
*enforcement* or *triage state* to the CE/free tiers. forge must own: (1) fingerprinted
triage storage, (2) the gate job/check that turns its verdicts into enforced merge
conditions, and (3) merged-state verification where the forge lacks merge trains/queues.
