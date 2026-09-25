# Runner-Time Redemption: Short-Lived Credential Exchange Patterns (2026-09-24)

The pattern where the runner authenticates to a broker and exchanges a short-lived,
claims-bound token for the secret at job start — instead of baking the secret into the
pipeline definition or the dispatch payload. Facts from official docs fetched
2026-09-24; tagged [documented] / [observed] / [inference]. This is the reference for
forge's option (b): redeeming the model credential over the existing lane-control
channel with the attempt-scoped HMAC token.

---

## 1. The generic pattern (as realized by GitHub Actions OIDC)

Flow [documented — GitHub "About security hardening with OpenID Connect",
https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect]:

1. Trust is preconfigured on the broker (cloud role / Vault role / federated
   credential) against the CI's OIDC issuer, pinned to claims.
2. Every job gets an auto-generated JWT from the CI's OIDC provider (GitHub issuer:
   `https://token.actions.githubusercontent.com`).
3. A step requests the token (`permissions: id-token: write` is required) and presents
   it to the broker.
4. The broker validates subject + claims against the trust definition and returns a
   short-lived credential "available only for the duration of the job".

**Binding claims in the GitHub JWT** [documented, example token]: `sub`
(`repo:owner/repo:environment:prod`, `repo:owner/repo:ref:refs/heads/main`, ...),
`repository`, `ref` / `ref_type`, `sha`, `run_id`, `run_attempt`, `actor`,
`event_name` (e.g. `workflow_dispatch`), `environment`, `workflow`, `job_workflow_ref`
(reusable-workflow pinning), plus `iss`/`aud`/`exp`/`nbf`/`iat`/`jti`. The documented
example token's `exp - iat` is 300 s — token lifetime on the order of minutes
[documented example; TTL is minutes, not hours].

Benefits stated by GitHub: "No cloud secrets"; granular authN/authZ on the provider;
"your cloud provider issues a short-lived access token that is only valid for a single
job, and then automatically expires" [documented, verbatim].

## 2. HashiCorp Vault's CI auth (JWT/OIDC auth method)

Vault is the canonical third-party broker; its JWT auth method is exactly the
redemption leg [documented — https://developer.hashicorp.com/vault/docs/auth/jwt]:

- Config trusts the CI issuer: `oidc_discovery_url` +
  `bound_issuer` (for GitHub Actions both `https://token.actions.githubusercontent.com`)
  [documented — GitHub's "Configuring OpenID Connect in HashiCorp Vault" page].
- A **role** carries the binding: `bound_subject` (must equal the JWT `sub`), 
  `bound_audiences` (must exactly match a JWT `aud` claim — enforced since Vault 1.17),
  and `bound_claims` — an arbitrary claim→required-values map, e.g.
  `{"repository": "user-or-org-name/repo-name"}`, glob-supported [documented].
- Policies attach to the resulting Vault token; `ttl` bounds it (GitHub's own example
  role uses `"ttl": "10m"`) [documented].
- Login is one HTTPS call: `POST /v1/auth/jwt/login {"jwt": ..., "role": ...}` →
  `auth.client_token`; then `GET /v1/<secret-path>` to read the secret [documented].
- `hashicorp/vault-action` wraps the exchange and "automatically masks the fetched
  secrets in logs" [observed — vault-action docs/community].

**Guarantees the broker side provides** [documented + observed]: cryptographic
signature verification (JWKS/discovery), claim-based binding (repo/ref/environment),
per-role least-privilege policy paths, explicit TTLs, and Vault audit devices logging
every login and secret read [observed — Vault audit device is core functionality;
treat audit logging as a standard guarantee of any real broker, including forge's].

## 3. Azure: workload identity federation (service connections)

- Azure DevOps ARM service connections with workload identity federation store **no
  secret**; at run time the pipeline presents a short-lived token and Entra ID
  exchanges it against a **federated credential** pinned to an issuer+subject
  (`sc://org/project/connection-name`; new-format subjects use immutable IDs;
  issuer migration to `https://login.microsoftonline.com/<tenant>/v2.0` runs
  2026–2027) [documented —
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/connect-to-azure ;
  migration timeline observed — MS announcements/community]. All authentication
  attempts land in the Azure DevOps audit log [documented — Entra service connection
  docs/blog].
- Pipeline side: `AzureCLI@2/3` with the service connection triggers the exchange;
  `SYSTEM_OIDCREQUESTURI` + `System.AccessToken` are the documented hooks [observed —
  MS tech-community walkthroughs].
- The new **Azure DevOps service connection** (Aug 2026) extends the same
  no-PAT/no-secret model to Azure DevOps APIs themselves ("No persistent secrets: Use
  Microsoft Entra federated credentials"; "Audit trail: All authentication attempts
  are logged") [documented —
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/add-devops-entra-service-connection].

## 4. GitLab: ID tokens (`id_tokens`) as the runner-side assertion

- A job declares `id_tokens: { VAR: { aud: https://broker } }` and receives a JWT for
  use in scripts [documented —
  https://docs.gitlab.com/ci/secrets/id_token_authentication/].
- Claims bind identity: `sub` defaults to
  `project_path:{group}/{project}:ref_type:{type}:ref:{branch}`, plus
  `project_id`, `ref`, `ref_path`, `ref_protected`, `environment_*` fields
  [documented].
- TTL: "The expiry time for the token is set to the job's timeout if specified, or
  **5 minutes** if no timeout is specified" [documented, verbatim].
- The documented flow is exactly broker redemption: runner authenticates to Vault with
  the token → Vault verifies + checks bounded claims → attaches policies → returns a
  token → runner reads secrets [documented sequence diagram]. Built-in provider
  integrations are Premium/Ultimate; manual OIDC to any broker is the documented
  generic path [documented — /ci/secrets/].

## 5. Anthropic's own first-party version of this pattern

Claude Code's GitHub integration supports Workload Identity Federation: the action
"exchanges the workflow's GitHub OpenID Connect (OIDC) token for Claude API access
through a Claude Console service account" (inputs `anthropic_federation_rule_id`,
`anthropic_organization_id`, `anthropic_service_account_id`, `anthropic_workspace_id`,
with `id-token: write`) — explicitly offered "to avoid storing a long-lived secret
entirely" [documented — https://code.claude.com/docs/en/github-actions]. Precedence:
federation credentials rank above `/login` [documented — authentication page].

**forge significance [inference]:** the runner-time-redemption design is not a
sideload trick — it is where Anthropic itself is heading for CI-run agents.

## 6. Standard guarantees checklist (what any redemption design must offer)

| Guarantee | GitHub OIDC | Vault role | Azure WIF | GitLab id_tokens | forge lane (proposed) |
|---|---|---|---|---|---|
| Authenticates the run, not a person | issuer-signed JWT | issuer-signed JWT | issuer-signed JWT | issuer-signed JWT | attempt-scoped HMAC token over authenticated lane channel |
| Bound to repo/ref/run | `sub`/`repository`/`ref`/`run_id` claims | `bound_subject`/`bound_claims` | federated credential subject | `sub` + `ref_protected` claims | HMAC token = f(attempt id, lane id, exp); validate ref/attempt server-side |
| Short TTL | minutes (300 s example) | role `ttl` (e.g. 10m) | job-scoped token, ~1 h access token | 5 min default / job timeout | TTL bound to attempt window |
| Least privilege at the target | cloud role | policy paths | RBAC on identity | Vault policy | per-ref credential grant |
| Audit | provider logs | Vault audit device (every login/read) | ADO + Entra audit logs | Vault/broker logs | forge control-plane log of every redemption |
| No value in pipeline config / dispatch | yes | yes | yes | yes | yes |

[documented for the four provider columns; forge column is design mapping — inference]

## 7. forge design notes (option b concretized)

[Inference — synthesis, since forge's channel is not in the studied docs]

1. At lane startup the runner already holds an attempt-scoped HMAC token issued by the
   control plane over the authenticated lane-control channel. Redemption is one HTTPS
   call: `POST {control}/v1/attempts/{id}/model-credential` with
   `{ "credential_ref": "anthropic-main" }`, auth = that HMAC token.
2. Control plane validates: attempt exists and is running; ref is bound to this lane;
   TTL = min(remaining attempt budget, credential max-TTL, e.g. 30m-24h); emits the
   value + `expires_at` + redemption id; writes an audit record (attempt, ref, sha of
   value for correlation without storing it in the log).
3. Runner exports it as `ANTHROPIC_AUTH_TOKEN` (doc 05) for the lane process only —
   never into workflow inputs, artifacts, or logs; `forge-lane` scrubs it from the
   child env after Claude CLI startup if the CLI supports apiKeyHelper-refresh
   instead (doc 05 §3).
4. Replay window: the HMAC token must be single-attempt and the redemption endpoint
   should be idempotent-per-attempt with rate limiting — the OIDC equivalents get this
   from `jti` + short `exp` [inference modeled on documented JWT guarantees].
5. Degradation: if the control plane is unreachable, the lane fails closed rather than
   falling back to dispatch-borne values [inference; consistent with every studied
   provider failing closed on trust mismatch].

## Sources

- GitHub OIDC overview + token claims + subject formats:
  https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/about-security-hardening-with-openid-connect
- GitHub OIDC with reusable workflows (job_workflow_ref pinning):
  https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/using-openid-connect-with-reusable-workflows
- GitHub OIDC ↔ Vault configuration (role example, ttl 10m, bound_claims):
  https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-hashicorp-vault
  (enterprise-cloud path also matched)
- Vault JWT/OIDC auth method (bound_audiences/bound_claims/bound_subject, login
  endpoint, TTL): https://developer.hashicorp.com/vault/docs/auth/jwt
- Azure RM service connection / workload identity federation:
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/connect-to-azure ;
  Azure DevOps Entra service connection:
  https://learn.microsoft.com/en-us/azure/devops/pipelines/library/add-devops-entra-service-connection
- GitLab ID tokens (aud, claims, 5-minute default TTL, Vault flow):
  https://docs.gitlab.com/ci/secrets/id_token_authentication/
- Claude Code Workload Identity Federation in Actions:
  https://code.claude.com/docs/en/github-actions
- Migration/issuer timeline for ADO federation [observed]: insights.nomadlab.cc 2026
  playbook; MS DevBlogs 2026-08-06.
