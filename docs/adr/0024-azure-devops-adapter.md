# ADR-0024: Azure DevOps as the third source + execution adapter

Status: accepted (2026-09-15)
Context: forge runs at full parity on GitLab CE and GitHub
([ADR-0019](0019-source-execution-adapters.md)). Azure DevOps (Services and
Server) is the third enterprise platform where the factory loop — authorized
work item → plan → human gate → coding harness in ephemeral CI → trusted
publisher → Draft PR → review → ready_for_human — must work. Research base:
[specs/azure-devops-brief.md](../specs/azure-devops-brief.md) (API ground
truths verified against learn.microsoft.com REST references, 2026-09).

## Decision

1. **Azure DevOps is a peer source + execution adapter, not a fork.** Same
   durable core, same gate machinery, same trusted publisher, same quality
   contract, same harness drivers — only the boundary modules differ:
   `forge.integrations.azure.AzureDevOpsClient` (typed httpx client, the
   ADR-0014 pattern — the official `azure-devops` Python package is stale
   at 7.1.0b and sync-only; we do not adopt it), an
   `AzureRepositoryReader` (AuthoritativeReader semantics), the
   `forge.gateway.azure_webhook` ingress, `AzureRunService` (the
   GitHubRunService shape: start_run / go / cancel), and a
   `AzurePipelinesExecutor` (the Actions adapter shape: launch / poll /
   cancel / reconcile over a typed handle). Provider id: `azure_devops`.
2. **Auth = PAT via Basic (`":" + PAT`, empty username), least-privilege
   scopes.** Forge's PAT: `vso.code_write` (branches/PRs/threads),
   `vso.build_execute` (dispatch/poll pipeline runs), `vso.work_write`
   (work-item plan comments), `vso.threads_full` (PR threads). The
   identity is a dedicated service account (PATs carry their owner's
   permissions and every action is attributed to them). Entra ID /
   service-principal auth is the documented production upgrade path —
   out of scope for the beta, the client takes a token-provider
   callable so Bearer can be added without surface changes (the GitHub
   App pattern).
3. **Webhook ingress = service hooks, fail-closed.** Azure DevOps webhooks
   have NO HMAC signature; the webHooks consumer offers Basic
   authentication credentials (HTTPS required). Forge validates those
   credentials in constant time and 503s/401s everything else — the same
   fail-closed posture as the GitLab token check and the GitHub HMAC
   check. Subscriptions are provisioned via the REST API
   (`POST /_apis/hooks/subscriptions`, publisherId `tfs`, consumerId
   `webHooks`) for exactly the events forge consumes: `git.push`,
   `git.pullrequest.created`, `git.pullrequest.updated`,
   `git.pullrequest.commented-on` (run commands + reactive review),
   `build.complete` (CI reconcile + debug lane),
   `workitem.commented` (run commands). Payload normalization follows the
   same contract as the other ingresses: inbox identity is
   content-stable per event, commands land as durable steps in ONE
   transaction, raw payloads are captured under `FORGE_CAPTURE_DIR`.
4. **The trusted publisher maps to the native CAS.** Azure's push API
   requires `refUpdates[].oldObjectId` — the expected parent commit —
   and rejects mismatches (`staleObjectId`, `forcePushRequired`, …).
   That is the same expected-head CAS as GitHub's `expectedHeadOid`
   (ADR-0006 race protection): publish = create branch (`oldObjectId`
   = 40×`0`) then one push with multi-file commits
   (`commits[].changes[]`, add/edit/delete, raw text or base64) from
   the frozen attempt base, with typed drift errors mapped to
   `BranchDriftError`.
5. **No-merge stays enforced by identity + policy, not prompts.** The bot
   identity's PAT simply has no "complete PR" workflow granted
   (Contribute-to-PR only, no Approve/Complete votes — enforced at the
   PAT scope/permission layer and verified by `forge doctor`); Draft PRs
   (`isDraft: true` at creation, supported since 2020) are the only thing
   forge creates. Completion strategies, vote policies and branch
   policies remain the humans' configuration.
6. **Execution adapter = Azure Pipelines, the Actions adapter's contract.**
   Launch primitive: the Pipelines Runs API
   (`POST /{project}/_apis/pipelines/{pipelineId}/runs`) with
   `templateParameters` (typed queue-time parameters: run_id,
   attempt_base, driver, model, work item id) — correlation by the
   returned run id (the 2026 dispatch-response shape; discovery by
   `sourceVersion`/branch only as Azure DevOps Server fallback). The lane
   is the same proposal-only contract as everywhere: check out the frozen
   attempt base detached (`checkout: self` + explicit
   `git checkout --detach` — pipeline YAML cannot pin a commit SHA in a
   resource ref), `persistCredentials: false`, no write credential in the
   lane, candidate `.forge/candidate.diff` + meta published as pipeline
   artifacts, publisher applies via CAS. Ground truth honored: for Azure
   Repos the YAML `pr:` trigger is IGNORED — PR CI is governed by branch
   policies ("Build validation"), which is exactly the ADR-0008 quality
   contract's enforcement point; the reconciler correlates builds to the
   candidate SHA via the builds API (`sourceVersion`).
7. **Triggers and reactive lanes mirror the other providers.** `/implement`
   (work-item comment or `git.pullrequest.commented-on` on a PR — the
   mention parser is provider-agnostic), plan posted as a work-item
   comment (markdown format) + RunSpec freeze + pending decision, `/go`
   consumes it, `/cancel` = revoke. Reactive review on
   `git.pullrequest.updated` (incremental via
   `lastMergeCommit.sourceCommit` before/after SHAs) posts findings as PR
   threads (status `active`, inline positions via `threadContext` file +
   line ranges, change-tracking via `pullRequestThreadContext` so comments
   follow iterations). Build failure → durable `debug_ci` step fed by the
   timeline + per-task log APIs.
8. **Config is connection-scoped like the other adapters.**
   `FORGE_AZDO_ENABLED` + `FORGE_AZDO_ORG_URL` (Services
   `https://dev.azure.com/{org}` or Server
   `https://{instance}/{collection}` — both supported by the same client)
   + `FORGE_AZDO_PAT` (forge identity) + `FORGE_AZDO_WEBHOOK_USERNAME` /
   `FORGE_AZDO_WEBHOOK_PASSWORD` (ingress validation) +
   `FORGE_AZDO_APPROVERS` (connection-scoped, ADR-0020 §4 lesson) +
   `FORGE_AZDO_BOT_NAME` (bot-loop guard). Harness credentials stay in
   the target project's pipeline variables/variable groups — never
   forge's.

## Migration / rollout

- AZ-1 client + reader + contract tests → AZ-2 ingress + run service →
  AZ-3 executor + lane template + reactive/debug lanes → AZ-4 docs,
  doctor, live verification (needs a user-provided PAT and organization —
  runbook in `docs/azure-setup.md`). Live verification gates the release
  exactly like the GitHub slice did.
- Rejected: adopting the stale `azure-devops` Python SDK; webhook
  validation by payload shape only (no authenticator); auto-approving or
  completing PRs (never); per-project fork of the lane semantics
  (one contract, three adapters).
