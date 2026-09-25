# Azure Pipelines: Secret Delivery to API-Triggered YAML Runs (2026-09-24)

How a forge-dispatched Azure Pipelines run (via `POST {org}/{project}/_apis/pipelines/{id}/runs`,
the "run_pipeline" surface) can receive a MODEL credential without spreading the value
into template parameters. Facts from learn.microsoft.com fetched 2026-09-24; tagged
[documented] / [observed] / [inference].

---

## 1. Runtime/template parameters are explicitly NOT secret-safe

The parameters-vs-variables table in the official runtime parameters page states, for
`Parameters`: **"Secret values — No support for secret values"** [documented —
https://learn.microsoft.com/en-us/azure/devops/pipelines/process/runtime-parameters?view=azure-devops].
And the same page's "Parameter security best practices":

> "When you use runtime parameters in Azure Pipelines, don't pass secrets or sensitive
> values as parameter inputs. Parameter values are expanded at template parsing time
> and might be exposed in pipeline logs or outputs. ... For credentials, tokens, or
> other confidential data, use pipeline variables marked as secrets and stored in Azure
> Key Vault, the Pipeline UI, or variable groups."

[documented, verbatim]

- Parameters expand at template-parsing (queue) time, are immutable after queue, and
  surface in the Run Pipeline UI [documented]. Template expressions `${{ }}` run before
  the pipeline and cannot see secrets at all (secret via `${{ variables.x }}` yields
  empty) [documented — template expressions page; observed in community guides].

**forge consequence [inference]:** whatever the broker resolves, it must NOT be spread
into `templateParameters` of the run_pipeline body. Same conclusion as GitHub inputs.

## 2. The secret variable type (the only secret-safe variable form)

- Secret variables are set: (a) in the pipeline UI (lock icon / "Keep this value
  secret"), (b) in a variable group (lock icon), (c) via a Key Vault-linked variable
  group; a logging command in YAML is possible but explicitly discouraged ("anyone who
  can access your pipeline can also see the secret") [documented —
  https://learn.microsoft.com/en-us/azure/devops/pipelines/process/set-secret-variables ,
  https://learn.microsoft.com/en-us/azure/devops/pipelines/security/secrets].
- Secret variables are masked in logs and are NOT injected as environment variables
  into scripts automatically — they must be explicitly mapped (`env: API_KEY:
  $(apiKey)`) [documented — set-secret-variables; echoed in the secrets best-practices
  page: "map your secrets into environment variables"].
- Best-practice page: "It's best to securely manage secret variables in Azure Key
  Vault. You can also set secret variables in the pipeline definition UI or in a
  variable group." And: prefer service connections / managed identities over secrets
  where possible [documented — secrets page].

## 3. Variable groups: the protected-resource carrier

- A variable group holds values + secret-flagged variables in Library; referenced from
  YAML with `variables: - group: my-variable-group` [documented —
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/variable-groups].
- **Secret variables in variable groups are protected resources**: "You can add
  combinations of approvals, checks, and pipeline permissions to limit access to secret
  variables in a variable group. Access to nonsecret variables isn't limited" — i.e.
  approvals/checks/permissions bind only to groups containing secrets [documented,
  verbatim].
- A YAML pipeline must be **authorized** to use a group ("anyone who could push code
  to your repository could extract the contents of secrets ... therefore you must
  authorize the pipeline") — first-run prompt or CLI/REST authorization [documented].
- Key Vault-linked groups map **names only**; "The pipeline runs that link to the
  variable group and fetches the latest secret values from the vault" — values never
  stored in DevOps [documented]. This is Azure's native rotation story.
- Group consumption: `$(name)` in task inputs; for scripts, map through `env:` since
  secret variables are not auto-exported [documented].

**forge binding/rotation semantics [inference]:** group (with ≥1 secret) = protected
resource → per-pipeline authorization + optional approvals/checks (branch control,
business hours, etc.). Rotation: overwrite the value in the group, or rotate in Key
Vault (vault-linked group fetches latest at run start — no DevOps-side update at all).

## 4. run_pipeline API: what the request body can carry

Run Pipeline REST body (`RunPipelineParameters`) [documented —
https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/run-pipeline?view=azure-devops-rest-7.1]:

| Field | Type | Notes |
|---|---|---|
| `templateParameters` | object | plain runtime/template parameter values |
| `variables` | `<string, Variable>` | `Variable = { isSecret: boolean, value: string }` |
| `resources`, `stagesToSkip`, `previewRun`, `yamlOverride` | — | run shaping |

So the API **can** pass queue-time variables, each markable `isSecret: true`
[documented field contract]. However:

- "The Azure Pipelines UI and the REST API that runs a pipeline provide ways for users
  to add new variables at queue time. This ability allows users to create variables
  that the pipeline author didn't define, to override system variables, and to set
  values for existing variables at queue time" — orgs are advised to limit queue-time
  variables [documented — "Securely use variables and parameters",
  https://learn.microsoft.com/en-us/azure/devops/pipelines/security/inputs].
- Passing a secret via `variables[...].isSecret=true` puts the raw value in forge's
  HTTPS request body and into the run's variable set — it works, it's masked in logs,
  but it makes forge (and every operator with run-view + queue rights) a delivery hop,
  and queue-time overrides are the documented abuse surface [documented + inference].

**forge conclusion [inference]:** `templateParameters` carries only the credential
**ref**. The value arrives either via (a) an authorized **variable group** the pipeline
references by name (`$(FORGE_MODEL_...)`), or (b) forge's runner-time redemption (doc
04). The `variables`/`isSecret` API path is a fallback for orgs that won't create
groups, not the default.

## 5. YAML consumption shape for the lane

```yaml
parameters:
- name: credentialRef     # ref only
  type: string
  default: anthropic-main
variables:
- group: forge-lane-credentials   # authorized per pipeline; contains secret vars
jobs:
- job: lane
  steps:
  - script: forge-lane run
    env:
      ANTHROPIC_AUTH_TOKEN: $(FORGE_MODEL_ANTHROPIC_MAIN)   # mapped from secret var
```

- Unmapped secret in a script without `env:` is simply absent [documented behavior].
- Branch binding: pair the group's pipeline permissions with a `condition` on
  `variables['Build.SourceBranch']` — the documented pattern for restricting secret
  availability to a branch [documented — secrets page "Use YAML templates" example].

## 6. Answer to the key question

The supported way to get a secret into an API-triggered YAML run is **not** template
parameters (documented: no secret support) and not naked queue-time overrides. It is a
**secret-typed variable** delivered through a **variable group** (protected resource,
per-pipeline authorization, optional approvals/checks, Key Vault link for rotation), or
a pipeline-UI secret variable — consumed via `$(name)` and mapped into the process env
inside the lane step [documented].

## Sources

- Runtime parameters (incl. verbatim "No support for secret values" and security best
  practices): https://learn.microsoft.com/en-us/azure/devops/pipelines/process/runtime-parameters?view=azure-devops
- Set secret variables (UI / group / Key Vault / env mapping):
  https://learn.microsoft.com/en-us/azure/devops/pipelines/process/set-secret-variables
- Secrets in pipelines (protect, audit, rotate; YAML-template + branch condition
  pattern): https://learn.microsoft.com/en-us/azure/devops/pipelines/security/secrets
- Variable groups (protected resources, authorization, Key Vault link):
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/variable-groups
- Securely use variables and parameters (queue-time variable limits):
  https://learn.microsoft.com/en-us/azure/devops/pipelines/security/inputs
- Run Pipeline REST API (templateParameters + variables {isSecret,value}):
  https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/run-pipeline?view=azure-devops-rest-7.1
- Queue-time-via-API community confirmation (6.0/7.x bodies): StackOverflow threads
  2020–2021 [observed].
