# GitLab CE: Secret Delivery to Trigger-API Pipelines (2026-09-24)

How a forge-dispatched GitLab pipeline (via `POST /projects/:id/trigger/pipeline` on
GitLab CE) can receive a MODEL credential without the value leaking through trigger
variables. Facts from docs.gitlab.com fetched 2026-09-24 (CE ≡ self-managed Free tier
feature set); tagged [documented] / [observed] / [inference].

---

## 1. Trigger variables are NOT secret-safe

- The trigger API takes `variables[KEY]=value` form (or JSON) alongside `ref`
  [documented — https://docs.gitlab.com/ci/triggers/].
- **Documented exposure:** "CI/CD variables in triggered pipelines display on each
  job's page, but only users with the Owner and Maintainer role can view the values."
  [documented, verbatim — triggers page]. So trigger-variable values are stored with
  the pipeline/job and rendered in the UI to Owner/Maintainer; they are ordinary
  pipeline variables, not masked/protected secrets.
- "It is a security risk to save tokens in plain text in public projects... A leaked
  trigger token could be used to force an unscheduled deployment, attempt to access
  CI/CD variables, or other malicious uses." [documented, verbatim].
- Precedence: **pipeline variables (manual run form, schedules, API/trigger variables)
  outrank project, group, and instance variables** [documented — CI/CD variables
  precedence list]. A trigger-supplied value can therefore shadow a same-named project
  variable — a classic override/exfil vector; GitLab 18.x added group-level controls to
  disable pipeline-supplied variables [observed — envmanager 2026 guide; version-gated].

**forge conclusion [inference]:** the trigger call carries the credential **ref** as a
plain variable (e.g. `FORGE_CREDENTIAL_REF=anthropic-main`) and nothing else
value-bearing. Values ride a different channel (section 3) or are redeemed at runtime
(doc 04).

## 2. CI/CD variables: masked / protected semantics and their limits

UI variables (Settings → CI/CD → Variables; project/group/instance scope) with flags
[documented — https://docs.gitlab.com/ci/variables/]:

- **Protected variable**: "only available in pipelines that run on protected branches
  or protected tags" [documented]. Fork pipelines don't get parent-project variables
  by default [documented].
- **Visibility: Visible / Masked / Masked and hidden**; Masked-and-hidden also
  prevents the value from ever being revealed again in the settings page (settable
  only at creation) [documented]. As of GitLab 18.3 new variables default to Masked
  [observed — 18.7 docs page wording "Masked (default)"].
- **Masking format limitations — values that CANNOT be masked.** The value must:
  - be a **single line with no spaces**,
  - be **8 characters or longer**,
  - not match the name of an existing predefined or custom CI/CD variable,
  and if variable expansion is enabled, only `_, :, @, -, +, ., ~, =, /` are allowed
  beyond the Base64 (RFC 4648) alphabet [documented]. Multi-line secrets (JSON blobs,
  private keys) cannot be masked — use the **File** type (value written to a temp file;
  the variable holds the path) [documented].
- "Masking a CI/CD variable is **not a guaranteed way to prevent malicious users from
  accessing variable values**." Transformed output (base64, split, escaped characters
  like `My\[value\]`) defeats the mask; `CI_DEBUG_SERVICES` can reveal values
  [documented].
- Sensitive values must live in the UI (or group/instance level), never in
  `.gitlab-ci.yml` (which is visible to everyone with repo access) [documented].

**forge consequence [inference]:** a bearer/API token like `sk-ant-...` (single line,
base64-ish charset, ≥8 chars) masks cleanly; anything structured must go through a
File-type variable or runtime redemption. Protected flag ties the credential to
protected refs — forge should require the lane branch (e.g. `main` or a dedicated
`forge/lanes/*` protected branch) so the model credential never reaches arbitrary
branch pipelines.

## 3. The recommended channel for a job needing an API key

Per GitLab's own docs, in order of preference for CE:

1. **Project/group CI/CD variable, protected + masked** — injected only into
   protected-ref pipelines; masked in logs; consumed as `$NAME` env var in the job
   [documented].
2. **File-type variable** for multi-line values [documented].
3. **External secrets management** — HashiCorp Vault / GCP / Azure Key Vault / AWS
   Secrets Manager integrations using **ID tokens** (OIDC) — but this is
   **Premium/Ultimate tier** on the docs page ("Tier: Premium, Ultimate"), so not
   available on CE as a built-in [documented — https://docs.gitlab.com/ci/secrets/].
   On CE the same pattern is achievable manually: `id_tokens` + `curl` to a
   Vault/broker JWT endpoint [documented mechanics; CE assembly is inference].
4. **`CI_JOB_TOKEN`** for GitLab-internal access only: unique per job, "valid only
   while the job is running", access level of the triggering user with a restricted
   API surface, inbound scope limited to the project by default, outbound allowlist
   required for cross-project [documented —
   https://docs.gitlab.com/ci/jobs/ci_job_token/]. It cannot authenticate to
   non-GitLab services — not a model-credential channel [documented scope +
   inference].

## 4. Lane job shape (CE)

```yaml
forge-lane:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "trigger"'      # forge dispatch
  script:
    - forge-lane run
  # ANTHROPIC_AUTH_TOKEN arrives as protected+masked project/group variable
```

- Ref-only trigger payload: `curl -X POST -F token=$TRIGGER_TOKEN -F ref=main
  -F "variables[FORGE_CREDENTIAL_REF]=anthropic-main"
  https://gitlab.example.com/api/v4/projects/$ID/trigger/pipeline` [documented API
  shape; ref-only payload is inference-by-design].
- Keep the lane on a protected branch so protected variables apply; keep the trigger
  token itself out of repos/clients that log it [documented risk + inference].

## 5. Answer to the key question

Trigger variables are **logged-and-visible pipeline data**: they display on the job
page (values visible to Owner/Maintainer), sit in the API-accessible pipeline variable
set, and outrank project variables by precedence [documented]. The recommended channel
for a job needing an API key on CE is a **protected + masked project/group CI/CD
variable** (File type for multi-line), with runtime ID-token redemption to a broker as
the zero-static-secret upgrade path (built-in integrations Premium-only).

## Sources

- Trigger pipelines with the API (visibility quote, token risk):
  https://docs.gitlab.com/ci/triggers/
- CI/CD variables (UI fields, masking requirements/charset, hidden, protected,
  precedence, file type, expansion charset):
  https://docs.gitlab.com/ci/variables/ (and versioned 17.4/18.7 snapshots)
- CI/CD job token (lifetime, scope, allowlist):
  https://docs.gitlab.com/ci/jobs/ci_job_token/
- External secrets + ID tokens (tier note): https://docs.gitlab.com/ci/secrets/ ,
  https://docs.gitlab.com/ci/secrets/id_token_authentication/
- Secondary (precedence pitfalls, 18.x pipeline-variable controls): envmanager.com
  GitLab secrets guide 2026, runxbuild.com, mironsoft.de [observed].
