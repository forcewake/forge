# Azure DevOps setup (runbook): service account + PAT + project + harness

End-to-end guide for connecting an Azure DevOps project to forge. Order
matters: forge first, then the identity + PAT, then service hooks, the
lane pipeline and the branch policy. Verification gates are the "verify
live" checklist at the bottom and the first live run.

**Status (honest beta):** the AzDO adapter is contract-tested end to end
against the recorded payload shapes
([ADR-0024](../adr/0024-azure-devops-adapter.md),
[research](../research/azure-devops.md)) — the lane, the gate machinery, the
reactive lanes and the joins all run in the unit suite. **Live
verification against a real organization is still pending** a
user-provided PAT + org (the checklist at the bottom lists exactly what
to confirm and what stays flagged).

## 0. What you need

- A forge deployment reachable by Azure DevOps for **service hooks** — a
  public HTTPS endpoint. Azure webhooks have NO HMAC signature; the
  delivery authenticates with Basic credentials over **HTTPS only** (a
  `cloudflared` tunnel works, exactly as for GitHub).
- An **Azure DevOps organization** where you can create a user, create
  pipelines and edit branch policies on the target repo (Project
  Collection Administrator or a cooperative admin for the policy step).
- forge's model key (whatever LiteLLM/the harness needs).

## 1. Service account + PAT

Create a **dedicated service account** (e.g. `forge-bot`) in the org — a
PAT carries its owner's permissions and every forge action is attributed
to it. Do not run forge on a human's PAT.

Create the PAT (User settings → Personal access tokens) with the
least-privilege scopes:

| Scope | forge uses it for |
|---|---|
| `vso.code_write` | factory branch + CAS push (publisher), Draft PRs, PR reads, project/repo listing |
| `vso.build_execute` | lane dispatch (Runs API), run/build polling, timeline + task logs, artifacts, build cancel |
| `vso.work_write` | work-item plan comments (the plan/gate surface) |
| `vso.threads_full` | PR review/debug comment threads |

Never grant more: **no "Approve and vote" permission, no PR Complete** —
the no-merge guarantee is enforced by this identity shape, not by prompts
([ADR-0003](../adr/0003-no-merge-is-enforceable.md),
[ADR-0024](../adr/0024-azure-devops-adapter.md) §5). Forge authenticates as
`Authorization: Basic base64(":" + PAT)` — empty username, colon prefix.
Entra ID / service-principal auth is the documented production upgrade
path (the client already takes a token-provider callable).

## 2. Configure forge

Add to forge's environment (`.env` / container env):

| Variable | Meaning |
|---|---|
| `FORGE_AZDO_ENABLED` | `true` — the ingress is fail-closed without it (503) |
| `FORGE_AZDO_ORG_URL` | `https://dev.azure.com/{org}` (Services) or `https://{instance}/{collection}` (Server 2022 = API 7.1) |
| `FORGE_AZDO_PAT` | the service account's PAT from step 1 |
| `FORGE_AZDO_WEBHOOK_USERNAME` / `FORGE_AZDO_WEBHOOK_PASSWORD` | the Basic pair service hooks must present — THE authenticator (no HMAC exists); generate with `openssl rand -hex 16` each |
| `FORGE_AZDO_APPROVERS` | comma-separated AzDO identities (`uniqueName` form, e.g. `dev@fabrikam.example`) allowed to `/implement` and `/go`; empty falls back to `FORGE_APPROVERS` (the lists never merge — a GitLab username can never approve an AzDO run) |
| `FORGE_AZDO_BOT_NAME` | forge's own AzDO identity (`forge-bot`) — its plan comments re-trigger `workitem.commented`, so the bot-loop guard must recognize it |
| `FORGE_AZDO_LANE_PIPELINE_ID` | the numeric id of the lane pipeline (step 4); unset = builtin in-worker proposer instead of the Pipelines lane |

Restart forge. Deliveries that fail the Basic check answer **401**;
unconfigured ingress answers **503**.

## 3. Service hooks (the 6 events)

Org settings → Web hooks → **+ Web hook** (or provision via the API,
`POST /_apis/hooks/subscriptions`). One subscription per event, all with:

- **URL** `https://<forge-host>/webhook/azure_devops`
- **Basic authentication**: the `FORGE_AZDO_WEBHOOK_USERNAME`/`...PASSWORD`
  pair (HTTPS is required — Azure refuses Basic on plain HTTP)
- **Resource details to send**: `All`
- Route on the event only — forge ignores `publisherId` (it is unstable
  across API versions: `tfs` AND `azure-devops` both appear in samples).

| Event | Notes |
|---|---|
| `workitem.commented` | run commands on work items (`/implement`, `/go`, `/cancel`, `/security`). Optional `commentPattern` filter; leave unset and let forge parse. |
| `git.pullrequest.commented-on` | run commands on PRs |
| `git.pullrequest.created` | reactive review trigger |
| `git.pullrequest.updated` | incremental reactive review (new pushes) |
| `build.complete` | CI failure debug lane; a `buildStatus=Failed` filter reduces noise |
| `git.push` | inbox-only today (reconciliation hooks arrive later) |

Forge's own lane runs and bot-authored comments/PRs are skipped inside
forge (lane-pipeline id and bot-identity guards), so the subscriptions
can stay unfiltered.

## 4. The lane pipeline + its variables

The coding agent runs in YOUR project's ephemeral pipeline agent — the
lane has no write credential and no forge secret; its only output is the
candidate artifact forge validates and publishes.

1. Commit
   [ci/templates/forge-lane.azure-pipelines.yml](../../ci/templates/forge-lane.azure-pipelines.yml)
   into the target repo (path of your choice, e.g. `/ci/forge-lane.yml`).
   Replace the `<PINNED_REF>` in its `pip install` with a ref you trust
   (a tag or full SHA — this is the lane's supply chain).
2. Pipelines → New pipeline → Azure Repos Git → Existing YAML → point at
   that file. **Do not add triggers** — the template has none on purpose:
   it is dispatch-only (forge queues runs via the Runs API), and Azure
   Repos ignores YAML `pr:` triggers anyway (see step 5).
3. Note the pipeline's numeric id → `FORGE_AZDO_LANE_PIPELINE_ID`.
4. Pipeline (or better: a **variable group** linked to the pipeline) →
   Variables — the harness provider keys as **secret** variables, per
   driver: `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` (or
   `ANTHROPIC_AUTH_TOKEN` for gateway providers), `ZAI_API_KEY`,
   `XAI_API_KEY`, `COPILOT_GITHUB_TOKEN`, and `FORGE_HARNESS_MCP` as a
   plain variable (copy-paste examples:
   [harness-onboarding §2b](../harnesses/onboarding.md#2b-mcp-servers-in-the-lane-forge_harness_mcp-adr-0022)). Optional brief transport: `FORGE_AZDO_READ_TOKEN`
   (your OWN read-only work-item PAT — never forge's PAT) plus
   `FORGE_AZDO_ORG_URL` / `FORGE_AZDO_BOT_NAME`; without them the lane
   uses a pre-provisioned `.forge/brief.md`.
5. The agent pool is yours — the template defaults to
   `ubuntu-latest`; a self-hosted pool with a container job mirrors the
   ADR-0002 execution profile.

## 5. Branch policy: Build validation (the quality contract)

PR CI is governed by branch policies on Azure Repos — the YAML `pr:`
trigger is ignored. The ADR-0008 quality contract's enforcement point is
a **Build validation** policy on the TARGET branch:

Repos → Branches → target branch (e.g. `main`) → Branch policies →
Build validation → point it at your project's own CI pipeline (NOT the
forge lane — the lane stays dispatch-only), `Trigger: automatic`,
blocking. Minimum-reviewer / vote policies stay your configuration —
forge never votes and never completes PRs (display-only findings as PR
threads).

## 6. forge.yml (optional repo scoping)

For repo-less work-item commands forge resolves the target repo in this
order: explicit mapping → the project's repository list (single repo, or
AzDO's project-named default) → the project name:

```yaml
forge:
  azure_devops:
    default_repos:          # project → repo
      Fabrikam: core
    # default_repo: core    # single default for all projects
```

## 7. First run

```text
comment /implement on a work item
  → forge replies with a plan comment + "Approve this exact plan by
    commenting /go <run-id>"
comment /go <run-id>          (as an identity in FORGE_AZDO_APPROVERS)
  → forge cuts the factory branch at the frozen attempt base and
    dispatches the lane pipeline (Runs API; the run id is the build id)
  → the agent runs in your pipeline agent (no write credentials); its
    diff lands as the forge-candidate-<run> pipeline artifact
  → forge validates and publishes it via the Push API CAS as a DRAFT PR
  → your Build validation policy runs the PR CI
  → reactive review posts PR threads → ready_for_human
```

Cancel: `/cancel <run-id>` (or bare `/cancel`) — the publication grant
is revoked first, then the lane build is cancelled.

## Verify live (the AZ-4 runbook checklist)

Everything below was verified live on 2026-09-16 (org
`PavelNasovich0958`, full-access PAT, claude-code lane); re-run the list
once on YOUR organization and record what you see:

- [x] **Webhooks arrive and authenticate** — live: comments produce 202s;
      wrong-password deliveries answer 401.
- [x] **`/implement` end-to-end** — live: work item → plan (with the
      Implementation block) → `/go` → lane run (claude-code, ~150 s on a
      Microsoft-hosted agent) → candidate artifact → Draft PR → review →
      `ready_for_human`. The candidate (a `farewell()` function) merged
      cleanly on human review.
- [x] **Branch creation semantics** — live: `POST /refs` creates the
      factory branch on every `/go`. (Note the documented API quirk the
      research caught: the *pushes* API body must be the BARE refUpdates
      array — an object wrapper 400s with `refUpdates: null`.)
- [x] **Thread status numerics** — live: active threads render correctly
      with `status: 1`. Closing suggestion threads uses `status: 5` per
      the documented enum order (not pinned by a sample — check one
      closed thread in the PR UI on first run).
- [x] **runId == buildId** — live: dispatch response ids matched the
      Pipelines UI urls; the AzDO cancel path keys off the build id.
- [ ] **`build.complete` payload shape for PR builds** — still open: the
      recorded fixture's `sourceBranch: refs/pull/{id}/merge`, `reason:
      pullRequest` and `triggerInfo: {pr.number}` are inferred. Capture
      one real PR-build failure payload (`FORGE_CAPTURE_DIR`) and confirm
      the debug lane correlates it to the PR.
- [x] **Work-item link (ArtifactLink)** — live: after publish the work
      item carries the ArtifactLink relations to the PR (visible under
      Development/Links).
- [x] **WIT comments stripe** — live: plan/evidence comments land via
      `7.1-preview.4` on Azure DevOps Services.
- [x] **`forge doctor`** — green: PAT identity probe, webhook
      credentials, lane pipeline id (values never printed).

## FAQ

**Why doesn't forge vote or complete the PR?**
It can't: the service account is shaped so Approve/Complete is not
granted ([ADR-0003](../adr/0003-no-merge-is-enforceable.md)). Review
findings are Active/Closed PR threads; merging is a human decision.

**The webhook returns 401 for every delivery.**
The Basic pair in the subscription must equal
`FORGE_AZDO_WEBHOOK_USERNAME`/`FORGE_AZDO_WEBHOOK_PASSWORD`, and the URL
must be HTTPS. Azure DevOps sends the Basic header on every delivery —
there is no signature to rotate.

**The lane queue-time parameters arrive but the pipeline ignores them.**
The templateParameters cross the REST boundary as strings; the template
must declare them as `type: string` queue-time parameters (the shipped
template does). A hand-copied template with `boolean`/`number` types is
the usual culprit.

**Can forge merge?**
No. Same contract as every provider — see
[ADR-0003](../adr/0003-no-merge-is-enforceable.md) and the scope table in
step 1.
