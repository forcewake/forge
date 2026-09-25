# Credential Delivery for forge CI Lanes — Research Overview (2026-09-24)

Research question: forge dispatches AI-coding lanes on GitHub Actions
(workflow_dispatch), Azure Pipelines (run_pipeline) and GitLab CE (trigger API). The
broker resolves WHICH model credential (a ref); today the resolved VALUE is spread
into ordinary dispatch inputs / template parameters — undeclared in the shipped
workflow schema and not secret-safe. What is the provider-native, secret-safe delivery
per provider, and what does the runner-time redemption alternative look like?

Topic docs (fetched from official docs 2026-09-24, house tags
[documented]/[observed]/[inference]):

- `01-github-actions-secret-delivery.md` — inputs are not secrets; `secrets.X` is.
- `02-azure-pipelines-secret-delivery.md` — "No support for secret values" for
  parameters; variable groups are the carrier.
- `03-gitlab-cicd-secret-delivery.md` — trigger variables are visible; masked +
  protected variables are the carrier.
- `04-runner-time-redemption.md` — OIDC/Vault/WIF/id_tokens exchange patterns and the
  guarantee checklist.
- `05-anthropic-credential-consumption.md` — ANTHROPIC_AUTH_TOKEN vs API_KEY vs
  apiKeyHelper, documented precedence, Anthropic's CI guidance.

---

## 1. Cross-cutting findings (all three providers agree)

1. **Dispatch payloads are public-by-default run metadata.** [documented ×3]
   - GitHub: inputs live in the event payload/run UI; no secret input type exists.
   - Azure: runtime/template parameters have documented "No support for secret
     values" and are expanded at parse time where they can hit logs.
   - GitLab: trigger variables "display on each job's page" (values visible to
     Owner/Maintainer) and **outrank** project variables by precedence — an override
     and exfiltration vector, not just a display risk.
   → forge rule: **the dispatch payload carries the credential REF, never the VALUE.**
   The current spread-into-inputs design must be removed, not just undeclared.
2. **Every provider has a native secret facility with the same shape** [documented ×3]:
   store value out-of-band, bind to repo/project (+ optionally ref/branch/environment),
   value flows store → runner at job start, masked in logs, write-only via API.
   - GitHub: repo / environment / org secrets (`secrets.NAME`; environment adds
     approval + branch gates). Azure: secret variables in variable groups (protected
     resources with approvals/checks/pipeline permissions) or pipeline-UI secrets.
     GitLab CE: project/group variables with Protected + Masked (+ File type).
3. **Masking is redaction, not access control** [documented ×3]: transformed/split
     output defeats it on every platform. It protects logs, not the credential from
     the job itself — the lane sandbox remains the real boundary.
4. **The industry direction is runner-time redemption** [documented: GitHub OIDC,
   Vault JWT auth, Azure workload identity federation, GitLab id_tokens — and
   Anthropic's own OIDC-federation option for the Claude GitHub Action]: job presents
   a short-lived, claims-bound token; broker returns a TTL'd credential. Standard
   guarantees: binding (repo/ref/run), minutes-scale TTL, least privilege, audit at
   the broker, zero long-lived secrets in CI config.
5. **Claude consumption is env-var-shaped with a fixed precedence**
   [documented]: cloud creds → `ANTHROPIC_AUTH_TOKEN` (Bearer) → `ANTHROPIC_API_KEY`
   (x-api-key) → `apiKeyHelper` (rotating/vault hook) → OAuth → federation → `/login`.
   The lane must set exactly one and scrub the rest.

## 2. Recommended delivery design per provider

### GitHub Actions — native profile (a)

- Workflow (shipped by forge, on default branch) declares input `credential_ref`
  (string) ONLY. Lane step:
  `env: ANTHROPIC_AUTH_TOKEN: ${{ secrets[format('FORGE_MODEL_{0}', inputs.credential_ref)] }}`
  [documented mechanism; mapping is forge design].
- Storage: repo secret `FORGE_MODEL_<REF>`; upgrade path = GitHub **environment**
  (e.g. `forge-lane`) with environment secret + optional required-reviewer/branch
  rules for approval gating [documented].
- Binding: repo (any workflow) → environment (this job only, admin-managed).
  Rotation: overwrite; no TTL; no versioning [documented model]. Value never appears
  in dispatch payload, inputs context, or run metadata [documented flow].

### Azure Pipelines — native profile (a)

- run_pipeline body: `templateParameters: { credentialRef: "anthropic-main" }` — ref
  only. Pipeline YAML: `variables: - group: forge-lane-credentials`; lane step maps
  `env: ANTHROPIC_AUTH_TOKEN: $(FORGE_MODEL_ANTHROPIC_MAIN)` [documented mechanism].
- Storage: variable group with lock-icon secret variables = **protected resource**:
  per-pipeline authorization + optional approvals/checks; Key Vault-linked group
  fetches values at run start (native rotation) [documented].
- Fallback (documented but not default): run_pipeline `variables:
  {name: {value, isSecret: true}}` passes a queue-time secret directly — works, but
  makes forge a delivery hop and queue-time variables are the documented override
  surface [documented + inference]. Avoid as the standard.

### GitLab CE — native profile (a)

- Trigger call: `variables[FORGE_CREDENTIAL_REF]=anthropic-main` only. Value comes
  from a **protected + masked project/group CI/CD variable** (File type if the value
  is ever multi-line) consumed as `$ANTHROPIC_AUTH_TOKEN` in the lane job
  [documented mechanism].
- Binding: protected variable ⇒ only pipelines on protected branches/tags — forge
  must dispatch the lane on a protected ref [documented + inference].
- Caveats: trigger variables outrank project variables (never name-collide the ref
  variable with the secret) [documented]; masked values must be single-line, ≥8
  chars, base64-ish charset — Anthropic tokens qualify [documented].

### All providers — runner-time redemption profile (b)

[design mapped from documented patterns; see doc 04 §7]
- Lane already holds an attempt-scoped HMAC token on the authenticated lane-control
  channel. At startup: `POST /v1/attempts/{id}/model-credential {credential_ref}` →
  `{value, expires_at, redemption_id}`; TTL bound to the attempt; every redemption
  audited centrally in the forge control plane; runner exports it as
  `ANTHROPIC_AUTH_TOKEN` (or wires `apiKeyHelper = forge-lane credential --ref` for
  mid-attempt refresh) [documented apiKeyHelper hook + forge inference].
- One uniform mechanism across all three providers; forge becomes the secret holder
  (new trust surface) in exchange for TTL, single rotation point, and central audit.

## 3. Trade-off table

| Dimension | (a) Native secret facility (per provider) | (b) Runner-time redemption (forge control plane) |
|---|---|---|
| Value exposure surface | CI provider store + runner env only; never in dispatch/API responses [documented] | forge control plane + runner env; value crosses lane HTTPS once per attempt [inference] |
| Auditability | Provider audit log records secret *update*; **no per-run read audit** of secret use [documented gap] | Every redemption logged with attempt id, ref, TTL — per-run attribution [forge design] |
| Rotation / TTL | Manual overwrite; no TTL, no versioning (GitHub/Azure VG/GitLab) — except Key Vault-linked Azure VGs which fetch latest at run [documented] | Forge-issued, TTL bound to attempt, instant revoke [forge design] |
| Cold-install simplicity | One manual step per provider repo (create secret/group/variable + authorize); zero new forge infra [documented setup] | Zero per-repo secret setup, but forge must hold credentials, run the redemption endpoint, and the lane must reach it [inference] |
| Provider lock-in | Three different facilities/permission models to document and automate [documented divergence] | One forge-native flow; providers interchangeable [inference] |
| Trust boundary shift | CI provider is the secret holder (as users already trust it) | forge control plane becomes secret holder — new blast radius, must fail closed offline [inference] |
| Masking/log safety | Provider masks known values (best-effort) [documented] | Same best-effort masking once exported; identical caveat [documented ×3] |

## 4. Recommendation for forge's first supported profile

**Ship (a) on GitHub Actions first: repo/environment secret named
`FORGE_MODEL_<REF>` + workflow reads `secrets[format(...)]`, dispatch carries
`credential_ref` only.**

Rationale [inference, grounded in the above]:
1. It is the exact pattern Anthropic documents for CI-run Claude agents
   (`${{ secrets.ANTHROPIC_API_KEY }}`), so forge's first profile matches vendor
   guidance and user expectations [documented].
2. Cold install is one documented step (`gh secret set`), no new forge infrastructure,
   and the value never touches forge — forge stays out of the secret-holding business
   for profile one.
3. The escape hatch is built in: same ref, upgraded to an **environment secret** for
   approval/branch gating without workflow changes [documented].

Implement (b) as the second profile behind the same `credential_ref` dispatch
contract: profile (b) is the only option that gives per-run audit, TTL, and instant
revocation, and it is uniform across all three providers — but it makes forge a
credential custodian and should be opt-in per installation. Ship Azure (variable
group) and GitLab CE (protected+masked variable) native profiles next using the
identical ref-only dispatch rule; they reuse the broker unchanged — only the
ref→secret mapping step differs per provider.

Immediate corrective regardless of profile: **stop spreading resolved values into
dispatch inputs / template parameters** — on all three providers those channels are
documented as visible, precedence-trumping, or log-exposed run metadata.

## 5. Top actionable findings (one per topic, 5 lines)

1. **GitHub**: dispatch inputs have no secret type and ride in the run's event payload;
   a workflow_dispatch run can still read repo/org/env secrets via `secrets.NAME`
   runner-side — pass only the ref; env secrets add approval+branch binding; rotation
   is overwrite-only (no TTL/read-back). (doc 01)
2. **Azure**: parameters are documented "No support for secret values"; the supported
   API-run channel is a secret variable in a **variable group** (protected resource:
   pipeline authorization + approvals/checks; Key Vault link = native rotation);
   `variables {isSecret}` on run_pipeline exists but is the queue-time override
   surface. (doc 02)
3. **GitLab CE**: trigger variables display on job pages (values to Owner/Maintainer)
   and **outrank** project variables — never value-carry; use protected+masked
   project/group variables on protected refs (single-line, ≥8 chars, base64-ish
   charset to mask; File type otherwise); built-in secrets-manager integration is
   Premium-only on CE. (doc 03)
4. **Redemption**: the OIDC/Vault/WIF/id_tokens pattern (claims-bound job token →
   broker → TTL'd credential, audited at the broker) is the standard; forge already
   owns the equivalent primitive (attempt-scoped HMAC token + authenticated control
   channel), so profile (b) is one new endpoint + TTL/audit semantics. (doc 04)
5. **Anthropic**: precedence is cloud → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY`
   → `apiKeyHelper` → OAuth → federation → /login; use AUTH_TOKEN for bearer/redeemed
   tokens, API_KEY for Console keys, and `apiKeyHelper` (with TTL env) as the native
   hook for forge's short-lived redemptions; Anthropic itself offers OIDC federation
   to avoid stored CI secrets. (doc 05)
