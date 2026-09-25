# Operation-scoped / claim-bound credential issuance (2026-09-25)

How mature platforms bind short-lived credential issuance to a *specific*
operation/job/attempt — the persisted GRANT each one keeps, its deadline
semantics, single-use vs windowed-retry behavior, and what each does about
"acknowledged-but-lost response" (the server consumed the request but the
client never received the credential). Companion to
[2026-09-24 credential delivery](../2026-09-24-credential-delivery/overview.md)
which fixed the *channel*; this pass fixes the *authorization object*.
Tags: **[documented]** verbatim/authoritative source (linked) ·
**[observed]** demonstrated in secondary but concrete sources ·
**[inference]** synthesis for forge.

---

## 1. HashiCorp Vault AppRole — the closest analogue to forge's grant

### 1.1 The persisted record

Vault's AppRole splits identity into `role_id` (a selector, "not a
credential that proves anything") and `secret_id` (the credential). What
the server persists per SecretID is, functionally, a grant document
[documented — https://developer.hashicorp.com/vault/api-docs/auth/approle]:

| Grant field (Vault name) | Meaning | Default |
|---|---|---|
| `secret_id_accessor` | handle for lookup/destroy **without revealing the secret_id** | generated |
| `metadata` (JSON k-v) | attribution: "which SecretID belonged to that build?"; set on tokens issued with it, **logged in audit logs in plaintext** | empty |
| `cidr_list` / `secret_id_bound_cidrs` | source-IP binding at login time; per-issuance list must be a subset of role-level | `[]` (none) |
| `ttl` / `secret_id_ttl` | hard expiry **from mint time** (not from use) | 0 = never |
| `num_uses` / `secret_id_num_uses` | use counter; HashiCorp: "For best security, set `secret_id_num_uses` to 1" | 0 = unlimited |
| `token_bound_cidrs` | binds the *resulting token* to IP blocks too | `[]` |
| `token_ttl` / `token_max_ttl` | result-token incremental TTL / absolute ceiling from creation | 0 |

Lookup-by-accessor returns exactly this shape
(`creation_time`, `expiration_time`, `cidr_list`, `metadata`,
`secret_id_num_uses`, `secret_id_ttl`) — i.e. the grant is inspectable and
revocable through a handle that is not itself the credential
[documented — AppRole API "Generate new secret ID"/"lookup" sample responses].

### 1.2 Response wrapping — single-use delivery with tamper evidence

`vault write -wrap-ttl=60s -force auth/approle/role/<r>/secret-id` does not
return the SecretID; it returns a **wrapping token** whose private cubbyhole
holds the response, redeemable **exactly once**, with a hard clock
[documented — https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern]:
"To guarantee confidentiality, integrity, and non-replication of SecretID,
you can use the `-wrap-ttl` flag … it puts it into a new token's Cubbyhole
with a token use count of 1."

Two controls make the delivery auditable and attack-resistant
[documented + observed — https://secopslog.com/courses/vault-prod/vp-approle]:

- **creation_path check**: every wrapping token records the API path that
  minted it; `sys/wrapping/lookup` (unauthenticated — "a runner holding
  nothing but the wrapping token can still make the check") exposes it.
  Runbook: "Compare `creation_path` against the exact string you expect,
  character for character, and stop if it differs. An attacker's re-wrap
  reads `sys/wrapping/wrap`." A re-wrapped token cannot lie about origin.
- **forced wrapping via policy**: `min_wrapping_ttl` / `max_wrapping_ttl`
  on the path — "a minimum of one second effectively makes response
  wrapping mandatory for that path". HashiCorp's orchestrator example:
  `path "auth/approle/role/+/secret*" { capabilities=["create","read","update"] min_wrapping_ttl="100s" max_wrapping_ttl="300s" }`.

### 1.3 "Acknowledged-but-lost response" — Vault's honest answer

Vault does **not** offer idempotent re-retrieval of a consumed response.
Single-use redemption is treated as a *tamper-evidence signal*, not a
retryable state [observed — secopslog course]:

> "A failed unwrap on a runner that should have succeeded is an incident,
> not a retry." … "a failed unwrap tells you the delivery went wrong, not
> why it went wrong" (theft-vs-expiry is indistinguishable from the error
> alone).

The prescribed runbook: "Destroy the SecretID by accessor, rotate the
delivery channel, then find out who opened the envelope" — destroy-by-
accessor means "your revocation runbook never has to hold the credential it
revokes." HashiCorp's own best-practice page says to alert when "Vault
throws a use-limit error when an application tries to read the SecretID"
and when the unwrap is "refused as the token has already been used" — in
both cases "the trusted-broker workflow has likely been compromised"
[documented — approle-pattern]. Delivery is thus **at-most-once with
detection**, and recovery is a *fresh grant* (new SecretID), never replay
of the old response.

Also documented for the orchestrator split: "RoleID and SecretID are only
ever together on the end-user system that needs to consume the secret" —
two channels, so no intermediary (including the broker) ever holds the
full credential.

## 2. AWS STS — grant = trust policy evaluated per call; stateless issuance

### 2.1 The persisted authorization object

For `AssumeRole`/`AssumeRoleWithWebIdentity` the persisted "grant" is the
role's **trust policy** + role settings; the *session* is the ephemeral
result [documented —
https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html]:

- **Trust policy conditions** pin the operation: OIDC `aud`/`sub`
  StringEquals/StringLike against the incoming JWT (the classic GitHub OIDC
  trust policy filters `token.actions.githubusercontent.com:sub` to
  `repo:org/repo:ref:refs/heads/main`).
- **`sts:TagSession` + condition keys** gate what session tags the caller
  may attach: `aws:RequestTag` (required k-v), `aws:TagKeys` (max key set),
  `aws:PrincipalTag`, `sts:TransitiveTagKeys`
  [documented — https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html].
- **Session policies** (`Policy` inline ≤2048 chars, up to 10 managed
  ARNs) scope the result: "the resulting session's permissions are the
  **intersection** of the role's identity-based policy and the session
  policies … cannot grant more permissions."
- **`ExternalId`** (confused-deputy fence), **`SourceIdentity`** (survives
  chains, lands in CloudTrail), **`RoleSessionName`** (visible in the
  assumed-role ARN → per-operation attribution).
- **Duration**: `DurationSeconds` 900 s–43 200 s, clamped by the role's
  `MaxSessionDuration` (default 3600 s); role chaining is hard-capped at
  **1 hour**; `AssumeRoot` is fixed 15 min with a **mandatory** task-scoped
  policy (you cannot mint "root, anything goes")
  [documented + observed — AWS docs; dev.to STS deep dive].

### 2.2 Claim → session-tag binding (the per-operation attribute surface)

OIDC web identity maps JWT claims into session tags under the
`https://aws.amazon.com/tags` namespace — nested
(`principal_tags` / `transitive_tag_keys`) or flattened
(`.../principal_tags/<k>`) for IdPs that cannot nest
[documented — id_session-tags]. The tags then ride **every** request the
session makes as `aws:PrincipalTag`, so downstream policies can demand
`aws:PrincipalTag/Team == ${s3:ResourceTag/Team}`. **Transitive** tags
survive role chaining and are tamper-proof downstream: "an attribute
stamped upstream cannot be overwritten downstream. Even if a downstream
call sets `--tags Team=admin` with the same key, the upstream Transitive
Tag wins" [observed — dev.to STS deep dive]. This is the closest industry
analogue to forge's "grant fields must match the dispatched operation": the
operation's claims are stamped into the credential and re-checked at every
use, not just at issuance.

### 2.3 Lost response

STS issuance is **stateless and freely re-callable**: a lost response is
recovered by calling again and receiving *fresh* credentials (new
expiration from now). There is no single-use coupling on the issuance
call, so "acknowledged-but-lost" costs one extra call, not one lost grant.
The price: nothing prevents N parallel valid sessions from N calls —
detection (CloudTrail `AssumeRole` events with `roleSessionName`,
`principalTags`, `durationSeconds`) replaces prevention
[documented — CloudTrail sample in id_session-tags].

## 3. GCP Workload Identity Federation — the grant is a CEL predicate over claims

The persisted object is the **workload identity pool provider**: attribute
mappings (`google.subject` ≤127 chars, required; up to 50 `attribute.*`)
plus an **attribute condition** — a CEL expression over the raw assertion,
evaluated at **every token exchange** [documented —
https://cloud.google.com/iam/docs/workload-identity-federation]:

> "An attribute condition is a CEL expression that can check assertion
> attributes … If the attribute condition evaluates to `true` for a given
> credential, the credential is accepted. Otherwise, the credential is
> rejected."

Production conditions bind the *operation*, e.g.
`assertion.repository_owner_id == '<org-id>' && assertion.ref == 'refs/heads/main'`
or `assertion.repository == 'my-org/my-repo' && assertion.ref == 'refs/heads/main'`;
pin on numeric ids not names (name reuse → squatting)
[observed — mevijay.com WIF end-to-end; lensix.com missing-condition
postmortem pattern]. IAM then grants **per-resource** to
`principalSet://…/attribute.repository/<owner>/<repo>` — including to a
single secret (`roles/secretmanager.secretAccessor` on one secret, not the
project) [observed — emrecavunt.com single-secret scoping]. Impersonation
of a service account requires `roles/iam.workloadIdentityUser` bound to
that principal(Set) [documented].

Lifetimes: the federated (direct-WIF) token is short (≤10 min, inherits
the upstream token's exp); SA access tokens default to ~1 h
[observed — mevijay.com; GCP default]. Two operational facts matter for
forge's fence design [observed — lensix.com]:

- **Attribute-condition updates take effect immediately** — "in-flight CI
  jobs and running workloads will start failing their token exchanges
  right away." There is no grace window: authorization is re-evaluated
  per exchange against *current* policy.
- An empty condition accepts **any** token from the issuer — "treat an
  empty condition as a misconfiguration, not a default."

Lost response: like STS, the exchange endpoint is re-callable; a retry
mints a fresh short-lived token. No single-use semantics on the exchange.

## 4. SPIFFE/SPIRE — the grant is a registration entry; attestation per connection

The persisted object is the **registration entry**: parentID (attested
node) + SPIFFE ID + selectors (`k8s:ns:prod`, `k8s:sa:payments`,
`unix:uid:1001`) + per-entry TTL overrides (`-x509SVIDTTL 900`,
`-jwtSVIDTTL 120` — seconds, not duration strings)
[documented — SPIRE docs; observed — secopslog zt-svid course].

- **Issuance**: the workload calls the local Workload API socket; the
  agent attests the *calling process* from kernel facts, matches selectors
  against entries, and only then obtains an SVID. "No token to present…
  the socket plus the kernel does the authentication." The private key is
  generated **in the workload** and never leaves it — X.509 SVIDs are
  proof-of-possession by construction ("Replay risk: Low"; JWT-SVIDs are
  bearers, "Present (reusable within lifetime if stolen)", audience-bound,
  ~5 min default) [documented + observed].
- **Deadline semantics**: each SVID has absolute `notBefore`/`notAfter`;
  the agent rotates at **half-life** (≈30 min into a 1 h SVID) over a
  held-open stream. Rotation is a **new absolute window**, not an
  extension — renewal never pushes `notAfter` past what a fresh issuance
  would allow. Hard ceiling above the grant: "SPIRE will not sign a
  certificate that outlives the authority signing it… the lifetime is
  truncated to that key's own expiry, with no error anywhere near the
  workload" — working rule `ca_ttl ≥ 6× SVID TTL` [observed — secopslog].
- **Revocation model**: expiry-as-revocation ("The credential cancels
  itself"); for immediate kill, SPIRE 1.9 `localauthority x509 taint`
  forces re-rotation, `revoke` drops the CA from the bundle
  [observed].
- Lost response: not applicable — there is no deliverable response to
  lose; identity is fetched from a local socket on demand and re-fetched
  at rotation. The failure mode is client-side: "a process that reads its
  SVID once is a scheduled outage."

## 5. Cross-system comparison (the question the review asks)

| | Vault AppRole + wrapping | AWS STS | GCP WIF | SPIFFE/SPIRE |
|---|---|---|---|---|
| Persisted grant | SecretID record (accessor, metadata, cidrs, ttl, num_uses) + role | Role trust policy + session-tag rules + role max duration | Pool provider: attribute mapping + CEL condition + IAM principal(Set) bindings | Registration entry: selectors → SPIFFE ID + per-entry TTLs |
| Bound to the operation via | metadata k-v + bound CIDRs + num_uses=1 | trust-policy claim match + session tags (transitive, tamper-proof downstream) + ExternalId/SourceIdentity | CEL over `assertion.*` at **every** exchange; per-resource principalSet | kernel-attested selectors matched per Workload-API connection |
| Deadline | absolute from mint (`secret_id_ttl`, wrapping TTL; token `max_ttl` from creation) | absolute from issuance (`Expiration`); 900 s–12 h, chained ≤1 h | federated token ≤10 min / SA token ~1 h, absolute | absolute notBefore/notAfter, rotated at half-life, silently capped by CA life |
| Single-use? | **yes** — wrapping token & SecretID redeem once | no (re-callable, parallel sessions allowed) | no (re-callable exchange) | n/a (socket fetch; each rotation is fresh) |
| Acknowledged-but-lost | **no idempotent retrieval** — failed unwrap = alert + destroy-by-accessor + fresh grant | retry the call → fresh creds | retry the call → fresh token | re-fetch from socket |
| Replay of a *stolen response* | detected (second unwrap fails) — "wrapping guarantees … that you find out" | undetected at issuance; detectable via CloudTrail | undetected at exchange; auditable | X.509 SVID: infeasible (key never leaves workload); JWT-SVID: bearer within TTL |

**The two poles forge must blend** [inference]: Vault-style *at-most-once,
tamper-evident delivery* (strong against response theft, brittle against
lost responses — the exact "acknowledged-but-lost" hole) and
STS/GCP-style *stateless re-callable issuance* (robust against loss, but
allows unbounded parallel redemptions unless the server itself persists a
redemption decision). No studied system offers both natively; the forge
design in [overview.md](overview.md) §A does it by persisting the grant
with a version counter and a bounded retrieval window.

## Sources

- Vault AppRole API (role params, secret-id generation/lookup responses):
  https://developer.hashicorp.com/vault/api-docs/auth/approle
- Vault AppRole best practices (two-channel delivery, wrapping, policies,
  alerting on use-limit errors):
  https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern
- Vault response-wrapping mechanics + runbooks (creation_path check, failed
  unwrap handling, accessor destroy, forced wrapping):
  https://secopslog.com/courses/vault-prod/vp-approle
- AWS STS AssumeRole (session policies intersection, DurationSeconds,
  tags, role-chain 1 h): https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html
- AWS session tags (aws:PrincipalTag, sts:TagSession, condition keys,
  transitive tags, OIDC claim mapping, CloudTrail):
  https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html
- AWS STS deep dive (durations table, AssumeRoot TaskPolicy, transitive-tag
  immutability): https://dev.to/kanywst/aws-sts-deep-dive-19ha
- GCP Workload Identity Federation (attribute conditions, mappings,
  workloadIdentityUser):
  https://cloud.google.com/iam/docs/workload-identity-federation
- GCP WIF end-to-end (lifetimes 10 min/1 h, numeric-id pinning,
  per-resource bindings): https://mevijay.com/blog/gcp-workload-identity-federation-github-actions
- Attribute-condition immediate effect + empty-condition failure mode:
  https://lensix.com/blog/workload-identity-provider-has-no-conditions-locking-down-gcp-federation
- Single-secret principalSet scoping:
  https://emrecavunt.com/blog/gcp-workload-identity-github-actions-secrets
- SPIFFE/SPIRE mechanics (selectors, TTLs, half-life rotation, CA ceiling,
  taint/revoke): https://secopslog.com/courses/zerotrust/zt-svid and
  https://alatirok.com/spiffe-spire-ai-agents-svid-mtls
