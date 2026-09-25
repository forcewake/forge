# Absolute deadlines vs sliding TTLs in credential systems (2026-09-25)

How mature credential systems treat expiry: absolute (fixed wall-clock
ceiling set at issuance) vs sliding/rolling (extended by use or renewal),
the "grant has an absolute deadline; refresh must re-present the grant"
pattern, and the documented CVE gallery of implementations that got
relative TTLs wrong. This grounds the `absolute_deadline` field and the
refresh/retry semantics of forge's operation grant. Tags:
**[documented]** / **[observed]** / **[inference]**.

---

## 1. What each major system does

### Vault — renewal inside a hard ceiling

- `token_ttl` is the **incremental** lifetime ("This current value … will
  be referenced at renewal time"); `token_max_ttl` is an **absolute
  ceiling measured from token creation**. A token "is renewable up to
  half an hour [max] from creation and finished after that, and a job
  that hangs cannot sit on Vault access all weekend" — renewal **cannot
  outrun** max_ttl; hitting it forces full re-authentication (which, with
  a single-use SecretID, means a fresh grant)
  [documented — AppRole API; observed — secopslog].
- Escape hatch: `token_period` (periodic tokens) — no max, renewable
  forever *provided each renewal lands inside the period*; explicitly the
  daemon/long-lived-service shape, deliberately distinct from job-shaped
  access [observed — secopslog].
- Batch tokens: non-renewable by construction.
- Wrapping tokens: absolute `wrap_ttl` clock; "after two minutes the
  wrapping token expires and its cubbyhole goes with it" — no renewal at
  all [observed].

### AWS STS — absolute, no refresh, re-assume to continue

- The session carries a fixed `Expiration` (ISO-8601 UTC) computed at
  issuance; `DurationSeconds` 900–43 200 s is clamped by the role's
  `MaxSessionDuration`, and "Role chaining limits your … role session to
  a maximum of one hour." There is **no renewal API** — continuation is a
  *new* AssumeRole call (a fresh grant presentation) producing a fresh
  absolute window; the caller-side policy can additionally cap requests
  via `NumericLessThanEquals sts:DurationSeconds`
  [documented — AssumeRole API; observed — Terraform pattern].
- `AssumeRoot`: fixed 15 min, mandatory task policy — even the strongest
  grant is deadline-capped and scope-narrowed [observed].

### GCP — short absolute tokens; every exchange is fresh

- Direct-WIF federated tokens ≤ ~10 min (inheriting the upstream token's
  exp); service-account access tokens ~1 h by default. No sliding
  extension: a longer-lived need performs new exchanges, each re-evaluated
  against the *current* attribute condition ("Updating a provider's
  attribute condition takes effect immediately")
  [documented + observed — WIF docs; mevijay; lensix].

### SPIRE — absolute windows, rotated, never extended; silent CA ceiling

- Each SVID has fixed `notBefore`/`notAfter`; the agent rotates at
  half-life, and rotation issues a **new absolute window** — expiry is
  never pushed forward on the old document ("The credential cancels
  itself"; expiry *is* the revocation model).
- The trap to copy as a lesson: the ceiling above the grant. "SPIRE will
  not sign a certificate that outlives the authority signing it … the
  lifetime is truncated to that key's own expiry, **with no error
  anywhere near the workload**" — rule of thumb `ca_ttl ≥ 6×` SVID TTL;
  a too-long configured TTL fails *silently shorter*
  [observed — secopslog]. forge analog: the grant's absolute deadline
  must be ≤ the signing authority/credential's own validity, and a
  misconfiguration should be loud (warn at issuance), not silent.

## 2. The family/grant pattern: absolute ceiling + bounded inactivity

The concrete recommended shape (from OAuth refresh-token engineering,
aligned with RFC 9700) [observed —
https://skillaudit.dev/seo/mcp-server-oauth-refresh-token-security]:

> "Correct practice: set an *absolute* expiry at issuance and never
> extend it. The absolute expiry is stored in the token **family** record,
> not the individual token. Optionally add a shorter inactivity window,
> but **only to restrict access — never to extend it beyond the absolute
> limit**."

Fields: `family_id`, `current_version`, `issued_at`,
`absolute_expires_at` ("Hard ceiling — never updated"),
`active_expires_at` ("updated on each refresh, but capped at absolute").
Plus replay detection: presenting a token whose `version` is older than
the family's current ⇒ "the token has already been consumed — revoke
every token in the family immediately" (RFC 9700 §4.14 family
invalidation on reuse; rotation itself per RFC 6749 §10.4). And bind
tokens to the client: HMAC-embed `client_id` so the credential cannot be
separated from its presenter.

IETF framing [documented — RFC 9700 as cited]: "the Authorization Server
MUST NOT extend the lifetime of a rotated refresh token beyond the
lifetime of the initial refresh token if a maximum expiration time was
established."

## 3. The failure gallery (relative TTL / lifetime-extension CVEs)

| Incident | Class | What went wrong | Lesson for forge |
|---|---|---|---|
| **CVE-2025-12627** (WSO2 IS impersonation) | CWE-613 insufficient session expiration | Refresh grant not bound to the impersonation context: attacker with an impersonated session's tokens "mint[s] new access tokens, effectively extending their malicious authorization beyond the intended session lifetime"; the RT "is treated as a standalone bearer credential rather than a context-bound artifact" | Refresh must re-validate the *authorization context* (the grant), not just the token's own signature+exp |
| **CVE-2026-75554** (hexpm) | CWE-613 | Refresh "re-derives access tokens from `granted_scopes` without re-checking organization membership"; a removed org member kept reading private packages **30 days** (refresh lifetime) instead of the intended **30 minutes** (access-token lifetime) — "The stored `granted_scopes` list acts as a trust anchor rather than as a request that must be re-validated" | Never let the credential outlive the authorization decision; every re-issuance re-checks current grant state |
| **CVE-2026-53517** (better-auth) | rotation race | Non-atomic read/validate/revoke on refresh: two concurrent presentations of the same parent both pass the revocation check ⇒ "a forked refresh-token family from a single parent"; also "Rotation refreshes that [7-day] window each call" (sliding). Fix: atomic compare-and-swap (`UPDATE … WHERE id=? AND revoked IS NULL`) + unique constraint + fail-closed loser | Redemption/refresh must be atomic (CAS on a version/revoked flag); concurrency on the same grant is a replay signal, not a feature |
| **CVE-2024-52311** (AWS data.all) | post-logout validity | Cognito tokens "not invalidated on logout, allowing previously authenticated users … to continue making authorized API calls until natural expiration" | "Natural expiration" of a long-lived token is not a revocation story; job-end must kill the grant server-side |
| **AWS Cognito defaults** (CIRT technique T1098.A006) | long-lived refresh | Default 30-day refresh tokens (configurable to 10 years); a stolen RT "silently generate[s] fresh access and ID tokens continuously" while "the legitimate user's session [is] not affected" | Long silent-refresh capability is an adversary technique, not a convenience; forge refresh windows should be minutes and visible in audit |

Cross-cutting pattern [inference]: every incident is a variant of one
bug — the **credential's lifetime outran the authorization's lifetime**
(either by sliding extension, by refresh re-derivation from stale state,
or by revocation not propagating). The fix in every case is the same
shape: an absolute deadline held in the server-side authorization record
(the grant/family), re-checked atomically on every re-issuance.

## 4. Rules this yields for forge's grant [inference, grounded above]

1. **One absolute deadline per grant**, set at dispatch (persisted server-
   side, like `absolute_expires_at`/`token_max_ttl`), never modified by
   any redemption, refresh, or retry. Credential TTL =
   `min(now → absolute_deadline, provider max)`.
2. **Refresh = re-present the grant, not extend the token.** A refreshed
   credential is a new redemption against the same persisted grant, valid
   only while the grant is still live and the operation tuple still
   matches — the hexpm/WSO2 lesson: re-derive authorization from current
   state, never from the previous issuance.
3. **Atomic single-logical-redemption**: compare-and-set on a persisted
   version/`redeemed_at` under a row lock or conditional update; the
   losing concurrent presentation is a replay event (alert + revoke), the
   better-auth lesson.
4. **Inactivity windows may only shrink**: an optional
   `last_seen`-based stall check can end a grant *early*; nothing may
   push `absolute_deadline` out. (Vault's period tokens are the
   deliberate exception for daemons — forge lanes are jobs, not daemons.)
5. **The ceiling above the grant must be validated loudly**: if the
   provider credential's own max validity < the requested deadline, warn
   at dispatch (SPIRE's silent-truncation lesson).
6. **Job-end kills the grant** (the data.all/Cognito lesson): lane
   completion/erasure revokes the grant record immediately; expiry is the
   backstop, not the mechanism.

## Sources

- Vault token TTL/max-TTL/period semantics:
  https://developer.hashicorp.com/vault/api-docs/auth/approle ;
  https://developer.hashicorp.com/vault/docs/auth/approle/approle-pattern ;
  https://secopslog.com/courses/vault-prod/vp-approle
- AWS STS DurationSeconds / MaxSessionDuration / 1-h role chaining:
  https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html
- Duration-limit table incl. AssumeRoot 15 min:
  https://dev.to/kanywst/aws-sts-deep-dive-19ha
- GCP token lifetimes and immediate condition effect:
  https://mevijay.com/blog/gcp-workload-identity-federation-github-actions ;
  https://lensix.com/blog/workload-identity-provider-has-no-conditions-locking-down-gcp-federation
- SPIRE absolute windows + silent CA truncation:
  https://secopslog.com/courses/zerotrust/zt-svid
- Family/absolute-expiry pattern + code (RFC 9700 §4.14, RFC 6749 §10.4):
  https://skillaudit.dev/seo/mcp-server-oauth-refresh-token-security ;
  https://openillumi.com/en/en-refresh-token-absolute-expiry-best-practice
- CVE-2025-12627 (WSO2): https://valtersit.com/cve/CVE-2025-12627
- CVE-2026-75554 (hexpm):
  https://www.sentinelone.com/vulnerability-database/cve-2026-75554/
- CVE-2026-53517 (better-auth): https://kodemsecurity.com/cve-archive/cve-2026-53517
- CVE-2024-52311 (data.all) + Cognito refresh abuse / T1098.A006:
  https://medium.com/@dikhyantkrishnadalai/aws-cognito-refresh-tokens-the-hidden-security-risk-behind-long-lived-user-sessions-2d0583e9753f
