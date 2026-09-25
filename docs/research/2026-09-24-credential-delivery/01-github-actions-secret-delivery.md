# GitHub Actions: Secret Delivery to workflow_dispatch Runs (2026-09-24)

How a forge-dispatched GitHub Actions run (via `POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches`)
can receive a MODEL credential (e.g. `ANTHROPIC_AUTH_TOKEN`) without the value passing
through the dispatch payload. Facts from official docs.github.com pages fetched
2026-09-24; tagged [documented] / [observed] / [inference].

---

## 1. workflow_dispatch inputs are NOT a secret channel

- Input schema lives in the workflow file itself: `on.workflow_dispatch.inputs` with
  types `boolean`, `choice`, `number`, `string`, `environment`; limits: **max 25
  top-level input properties, max payload 65,535 characters** [documented —
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows].
- Inputs arrive in the `inputs` context (Booleans preserved) and in
  `github.event.inputs` (all strings); the two are identical except for Boolean typing
  [documented, same page].
- There is **no secret input type** and inputs are carried verbatim inside the
  `workflow_dispatch` event payload — the same payload the run exposes via the event
  contexts, webhook deliveries, and the run's UI "inputs" display [documented for
  payload delivery: events/webhook docs; UI display is [observed] — every dispatch run
  page shows its inputs]. Practical consequence: **any value forge spreads into a
  dispatch input is readable by anyone who can view the run** (read access to the repo
  for private repos; the world for public repos) [inference from documented payload
  visibility + observed UI].
- The dispatch REST endpoint requires `ref` plus an `inputs` object ("Input keys and
  values configured in the workflow file. The maximum number of properties is 25.");
  fine-grained tokens need "Actions" repository permissions (write) [documented —
  https://docs.github.com/en/rest/actions/workflows].
- The workflow file must exist on the default branch for the trigger to be usable;
  after the workflow has run at least once it can be dispatched against any branch/tag
  via API/CLI [documented, events page].

**forge conclusion [inference]:** dispatch inputs may carry the credential **ref**
(e.g. `model_credential: anthropic-main`) but never the **value**. The workflow maps
ref → secret at run time (section 3).

## 2. Actions secrets: storage model, scopes, permissions

Three scopes, encrypted with libsodium sealed boxes before reaching GitHub
[documented —
https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions
and the secure-use reference]:

| Scope | Who can create | Available to | Notable gate |
|---|---|---|---|
| Repository | write access (org repos) / collaborator (personal) | all workflows in the repo | none |
| Environment | repo owner (personal) / **admin** (org) | only jobs declaring `environment: NAME` | protection rules run first |
| Organization | org owners (+ `admin:org` for gh CLI) | policy: all / private / selected repos | access policy per secret |

- **Values are write-only**: after creation the value can be overwritten or deleted but
  never read back through UI or API (the Actions secrets REST surface has
  list/create/update/delete but no value-read endpoint) [observed; stated plainly in
  GitHub's docs ecosystem, e.g. envmanager/kodivio guides 2026; consistent with
  documented API surface].
- **Secret names vs values**: anyone with write access to the repo can *see and use*
  every repo secret name/value pair in a workflow — "Any user with write access to your
  repository has read access to all secrets configured in your repository" [documented —
  secure use reference]. They still cannot read the value out of the API; but they can
  run a workflow that uses it [documented implication + observed].
- Limits: 100 repo secrets, 100 per environment, 1,000 per org; 48 KB per secret
  [documented for size on the secrets page ("larger than 48 KB" workaround); counts
  observed via secrets reference].
- Rotation = overwrite (`gh secret set NAME` / REST PUT). **No versioning, no expiry,
  no read-back**; GitHub's own guidance is delete-and-rotate on exposure and periodic
  review [documented — secure use reference "Delete and rotate exposed secrets";
  "Audit and rotate secrets" section of secrets-in-pipelines guidance].
- Secrets are withheld from fork-triggered runs (except `GITHUB_TOKEN`) and from
  Dependabot-triggered workflows; not auto-forwarded into reusable workflows
  (`secrets: inherit` or explicit pass required) [documented — using-secrets notes].

## 3. The provider-native pattern: dispatch carries ref, workflow reads `secrets.X`

A workflow_dispatch run CAN read repo/org/environment secrets directly — the secret
flows from GitHub's store to the runner at job start and **never traverses the dispatch
payload** [documented — secrets context usage]:

```yaml
on:
  workflow_dispatch:
    inputs:
      credential_ref:      # REF ONLY — value never in payload
        description: Which model credential to use
        type: string
        required: true
jobs:
  lane:
    runs-on: ubuntu-latest
    steps:
      - name: Run AI lane
        env:
          ANTHROPIC_AUTH_TOKEN: ${{ secrets[format('FORGE_MODEL_{0}', inputs.credential_ref)] }}
        run: forge-lane run
```

- Secrets are consumed via `env:` or `with:` (never on the command line; GitHub warns
  command-line processes are visible via `ps` and audit events) [documented — using
  secrets page].
- Unset secret → empty string (silent), so fail fast on emptiness [documented].
- Masking is best-effort exact-match redaction (plus some encodings); structured data
  (JSON blobs) as secret values defeats redaction — keep the token a bare string
  [documented — secure use reference].

### Environment secrets give forge a binding + approval story

- Environment secrets "are only available to workflow jobs that use the environment"
  and a job "cannot access environment secrets until one of the required reviewers
  approves it" [documented — environments reference / deployments-and-environments].
- Protection rules: required reviewers (up to 6, one approval suffices, optional
  prevent-self-review), wait timer, deployment branch/tag policies [documented].
- The `environment` *input type* on workflow_dispatch lets the dispatcher select an
  environment in the UI; in YAML forge can hardcode `environment: forge-lane` on the
  job [documented input type; wiring is inference].

**forge binding/rotation semantics (native path) [inference]:**
- Binding: repo secret → any workflow in the repo; environment secret → only the lane
  job + admin-gated creation + optional reviewer approval.
- Rotation: manual overwrite per repo/env/org; no TTL. forge's broker can *name* a
  generation (e.g. `FORGE_MODEL_ANTHROPIC_V2`) but cannot expire old values.

## 4. Dispatch API specifics forge will use

- `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches` with
  `{"ref": "...", "inputs": {...}}`; the response now returns the workflow run id + URLs
  [documented — REST workflows page, 2026 API version samples].
- Triggering requires the workflow on the default branch; the dispatch `ref` selects
  what the run checks out [documented].
- forge's app token needs Actions write (fine-grained "Actions: write" or classic
  `repo` scope) [documented].

## 5. Answer to the key question

**Yes** — a workflow_dispatch event triggers a workflow that reads repo/org/environment
secrets; the secret value is delivered runner-side by GitHub and does not pass through
the dispatch payload, the inputs context, or the run's event payload [documented
behavior of the `secrets` context; the inputs context only ever carries what the
dispatcher sent]. The only things forge must guarantee: (a) inputs carry refs only,
(b) the shipped workflow schema declares no credential-value input, (c) the lane job
maps ref → `secrets.NAME` and fails closed on empty.

## Sources

- Events that trigger workflows (`workflow_dispatch` section):
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Using secrets in GitHub Actions:
  https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions
- Secure use reference (write access ⇒ secret read access; redaction limits; rotation):
  https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
- Environments / protection rules:
  https://docs.github.com/en/actions/reference/environments and
  https://docs.github.com/actions/reference/workflows-and-actions/deployments-and-environments
- REST: workflows (dispatch) and workflow runs (run metadata shape, no inputs-redaction
  guarantee): https://docs.github.com/en/rest/actions/workflows ,
  https://docs.github.com/en/rest/actions/workflow-runs
- Secondary (limits, write-only behavior, rotation practice): envmanager.com and
  kodivio.org GitHub Actions secrets guides, 2026 [observed].
