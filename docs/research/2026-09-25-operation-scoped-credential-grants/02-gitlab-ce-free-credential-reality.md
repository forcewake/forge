# GitLab CE (Free) credential reality (2026-09-25)

What self-managed GitLab Community Edition — the Free tier — natively
offers for a short-lived, job-bound credential, with documented limits.
This decides what forge's `GET /lane/credentials/redeem` contract can ask
a GitLab CE lane to present. Facts fetched from docs.gitlab.com and
self-managed doc mirrors on 2026-09-24/25. Tags: **[documented]** /
**[observed]** / **[inference]**.

---

## 1. `id_tokens` (OIDC JWT) — CONFIRMED available on Free / Self-Managed

The decisive fact, verbatim from the docs header
[documented — https://docs.gitlab.com/ci/secrets/id_token_authentication.html]:

> "Tier: **Free**, Premium, Ultimate · Offering: GitLab.com, **GitLab
> Self-Managed**, GitLab Dedicated" (introduced in GitLab 15.7).

So on CE: yes, a job can declare

```yaml
job:
  id_tokens:
    FORGE_ID_TOKEN:
      aud: https://forge-control.example
  script:
    - forge-lane redeem --token $FORGE_ID_TOKEN
```

and receive an RS256 JWT "signed with a dedicated private key" whose
signature forge verifies via the instance's OIDC discovery/JWKS
(`https://<gitlab>/.well-known/openid-configuration`).

### 1.1 The claim set forge can bind a grant to [documented]

Standard: `iss` (instance domain), `sub`, `aud` (per-token), `exp`, `nbf`,
`iat`, `jti`. Custom: `project_id`/`project_path`,
`namespace_id`/`namespace_path`, `job_project_*` (18.4),
`user_id`/`user_login`/`user_email`/`user_access_level`, `user_identities`
(pref), `pipeline_id`, `pipeline_source`, **`job_id`**, `ref`, `ref_type`,
`ref_path`, `ref_protected`, `groups_direct` (feature flag, ≤200 groups),
`environment`, `environment_protected`, `deployment_tier`,
`environment_action`, **`runner_id`**, **`runner_environment`**
(`gitlab-hosted`|`self-hosted`), `sha`, `ci_config_ref_uri`,
`ci_config_sha`, `project_visibility`, `job_source`/`job_config` (18.9).

This is a near-complete operation tuple: `job_id`+`pipeline_id` identify
the dispatched operation, `sha`+`ref*` bind the work, `runner_id` names
the executor. `sub` defaults to
`project_path:{group}/{project}:ref_type:{type}:ref:{branch_name}` and
**is configurable per project via the projects API**
(`ci_id_token_sub_claim_components`; GitLab 18.7 added `ref_protected`
and `environment_protected`/`deployment_tier` into composable `sub`)
[documented].

### 1.2 Lifetime semantics and their documented limit

> "The expiry time for the token is set to the **job's timeout** if
> specified, or **5 minutes** if no timeout is specified." [documented]

Two consequences [inference from documented facts]:

- The 5-minute default is right for a redemption handshake: mint, redeem,
  done.
- If the job sets a long timeout, the JWT's `exp` is equally long, and
  the docs describe **no revocation of an id_token when its job ends**
  (unlike `CI_JOB_TOKEN`, below). Validation is offline signature+claims
  checking. Treat id_tokens as **exp-bounded, not job-bounded**: forge's
  grant deadline, not the JWT's exp, must be the real fence.

Precedent worth copying: "GitLab **blocks ID token issuance** when the
configured `sub` claim contains a `project_path` with a path that another
project previously used. This restriction prevents a new project from
inheriting external trust policies" [documented] — i.e. GitLab itself
refuses to let trust bind to a mutable name. forge's grant should key on
immutable ids for the same reason.

### 1.3 The `secrets:` keyword is NOT the CE path

The runner-side `secrets:vault:` keyword (automatic Vault/GCP/Azure
fetch, value delivered as a file variable) is
[documented — https://docs.gitlab.com/ci/secrets/]:

> "Use Vault secrets in a CI job · Tier: **Premium, Ultimate** ·
> Offering: GitLab.com, GitLab Self-Managed, GitLab Dedicated"

The "Using external secrets in CI" *landing page* is Free, and older doc
mirrors note the tutorial variant "is available to all subscription
levels" — that refers to doing the exchange **manually in the job script**
with `id_tokens` + `curl`/`vault`, which is exactly the Free flow
[documented + observed — doc mirrors; see also
../2026-09-24-credential-delivery/03-gitlab-cicd-secret-delivery.md].
Design consequence: forge's endpoint must be reachable by a plain HTTPS
call presenting the JWT — anything that assumes the Premium `secrets:`
machinery would be pretending a feature CE does not ship.

## 2. `CI_JOB_TOKEN` — job-bound, but only inside GitLab (+KAS)

[documented — https://docs.gitlab.com/ee/ci/jobs/ci_job_token.html]

- "The token is valid **only while the job is running**. After the job
  finishes, the token access is revoked." Also invalid when the job is
  erased or the project is being deleted. Tier: Free (all offerings).
- "The token receives the same access level as the user that triggered
  the pipeline, but has access to fewer resources than a personal access
  token" — the endpoint set is a fixed allowlist (registry, packages,
  releases, GET-shaped repo/artifact endpoints, …). It is **not** a
  general bearer for third-party brokers.
- **Inbound scope** ("Job token permissions", formerly "Limit access to
  this project"): "By default, the allowlist of any project only includes
  itself"; cross-project use requires the target project to allowlist the
  source group/project AND the triggering user to be a member with the
  needed role. Self-managed admins can enforce the allowlist instance-wide.
  Limit: ≤200 groups/projects per allowlist.
- No TTL knob exists — the lifetime *is* the job's runtime. There is no
  documented per-read audit of what a job token fetched (only that it is
  masked in job logs).

**Verdict for forge [inference]:** `CI_JOB_TOKEN` cannot authenticate to
forge's redemption endpoint (it is not a JWT and only GitLab/KAS accept
it); it is irrelevant to the broker leg except as the KAS bearer below.

## 3. GitLab Agent for Kubernetes (KAS) — Free CI/CD tunnel, Premium impersonation

[documented — https://docs.gitlab.com/18.5/user/clusters/agent/ci_cd_workflow]

- The CI/CD workflow is **Free** ("Moved to GitLab Free in 14.5");
  jobs get a pre-configured `KUBECONFIG`, or manually:
  `kubectl config set-credentials agent:$AGENT_ID --token="ci:${AGENT_ID}:${CI_JOB_TOKEN}"`
  against the KAS proxy — i.e. the bearer is `ci:<agent-id>:<job token>`,
  and the agent's `ci_access:` config is an explicit project/group
  allowlist (≤500 projects; same top-level namespace unless instance-level
  authorization is admin-enabled).
- **But**: "Restrict project and group access by using impersonation —
  Tier: **Premium, Ultimate**." On CE, a job using the tunnel "inherits
  all the permissions from the service account used to install the agent"
  — coarse on Free; the per-job identity (`gitlab:ci_job:<id>` +
  `agent.gitlab.com/ci_job_id` etc.) is the Premium gate.

Relevance to forge is indirect (it is the pattern of a *platform-scoped
job credential* with an explicit allowlist + job-id-annotated identity),
not a credential forge can consume.

## 4. CI/CD variables (the fallback carrier) — documented limits

Carried over from the 2026-09-24 pass; the CE-relevant facts
[documented — GitLab CI/CD variables docs; see
../2026-09-24-credential-delivery/03-gitlab-cicd-secret-delivery.md]:

- **Protected** variables flow only to pipelines on protected
  branches/tags; **masked** values must be single-line, ≥8 chars, from a
  base64-ish charset (Anthropic tokens qualify); masking is log redaction,
  not access control.
- Environment scoping selects *which* variable a job sees; **per-run
  audit of variable reads does not exist** — the read happens at job
  start, unlogged as an event forge could correlate.
- Trigger variables display on job pages and **outrank** project
  variables — value-carrying trigger variables are an override/exfiltration
  vector; only the ref should ever ride the dispatch.

## 5. The CE-compatible redemption contract [inference, grounded above]

1. The lane job declares `id_tokens: FORGE_ID_TOKEN: aud: <forge
   control plane>`; TTL defaults to 5 min if the job sets no timeout —
   forge should document "no long job timeouts on redemption jobs, or
   rely on forge's own grant deadline".
2. Forge validates: `iss` == configured instance (JWKS), `aud` == forge,
   `exp`/`nbf` window, then binds the persisted grant to `job_id` +
   `pipeline_id` + `sha` + `ref`/`ref_protected` (+ `project_id`) — the
   GitLab-native spelling of the operation tuple.
3. Do **not** rely on: the `secrets:` keyword (Premium), Agent
   impersonation (Premium), `CI_JOB_TOKEN` reaching forge, or trigger-
   variable carriage of values. All four are documented off-limits or
   unsafe on CE.
4. `runner_id`/`runner_environment` claims give coarse sender attribution
   now; cryptographic sender-binding (DPoP-style) is the NEXT step — see
   [03-sender-constrained-credentials.md](03-sender-constrained-credentials.md).

## Sources

- ID token authentication (tier header Free/Self-Managed; claim table;
  exp = job timeout / 5 min; sub configurability; path-reuse block):
  https://docs.gitlab.com/ci/secrets/id_token_authentication.html
- Secrets keyword tier (Premium/Ultimate) vs Free manual flow:
  https://docs.gitlab.com/ci/secrets/hashicorp_vault and
  https://docs.gitlab.com/18.0/ci/secrets
- CI/CD job token (valid-while-running, endpoint allowlist, inbound scope
  defaults, admin enforcement):
  https://docs.gitlab.com/ee/ci/jobs/ci_job_token.html
- GitLab Agent for Kubernetes CI/CD workflow (Free tier, ci:AGENT:JOB_TOKEN
  bearer, ci_access allowlist, Premium impersonation):
  https://docs.gitlab.com/18.5/user/clusters/agent/ci_cd_workflow
- Cloud services / OIDC conditional-role pattern (sub/aud filters):
  https://docs.gitlab.com/ee/ci/cloud_services
- Prior pass (variables, trigger-variable precedence, masked charset):
  ../2026-09-24-credential-delivery/03-gitlab-cicd-secret-delivery.md
