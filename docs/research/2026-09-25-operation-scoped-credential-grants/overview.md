# Operation-scoped credential grants — research overview (2026-09-25)

> Follow-up to [2026-09-24 credential delivery](../2026-09-24-credential-delivery/overview.md),
> which established *how* a lane obtains a model credential (runner-time
> redemption over the lane-control channel). The new review demand: the
> redemption (`GET /lane/credentials/redeem`) must be authorized against
> the **exact dispatched operation** via a persisted operation grant —
> not project membership. Topic files:
>
> 1. [01-operation-scoped-issuance.md](01-operation-scoped-issuance.md) —
>    Vault AppRole/response-wrapping, AWS STS session tags, GCP WIF
>    attribute conditions, SPIFFE/SPIRE: what the persisted grant looks
>    like in each, deadline semantics, single-use vs windowed retry, and
>    "acknowledged-but-lost response" handling.
> 2. [02-gitlab-ce-free-credential-reality.md](02-gitlab-ce-free-credential-reality.md) —
>    `id_tokens` confirmed Free/Self-Managed (with the full claim set),
>    `CI_JOB_TOKEN` limits, Premium-only `secrets:` keyword, Agent for
>    Kubernetes tiers.
> 3. [03-sender-constrained-credentials.md](03-sender-constrained-credentials.md) —
>    RFC 8471 Token Binding (dead), RFC 8705 mTLS, RFC 9449 DPoP (the
>    model), and the DPoP-shaped NEXT adapter for the redemption
>    handshake.
> 4. [04-absolute-deadlines-vs-sliding-ttls.md](04-absolute-deadlines-vs-sliding-ttls.md) —
>    absolute vs sliding expiry across Vault/STS/GCP/SPIRE, the
>    family/absolute-expiry pattern, and the CVE gallery of relative-TTL
>    failures.
>
> Confidence marks: **[documented]** authoritative source (linked) ·
> **[observed]** concrete secondary source · **[inference]** this pass's
> synthesis for forge.

## A. Recommended grant document for forge

Synthesis [inference] of the four systems' grant shapes: Vault's
SecretID record (accessor + metadata + cidrs + ttl + num_uses), STS's
trust-policy/claim binding with tamper-proof transitive session tags,
GCP's CEL predicate re-evaluated at every exchange, SPIRE's
selector→identity registration entry — all under the absolute-deadline
discipline of topic 4.

```jsonc
// persisted BEFORE dispatch; the attempt HMAC token only *points* at it
{
  "grant_id":     "g_9f41…",                  // unique envelope id (like wrapping_accessor)
  "subject":      { "lane": "…", "actor": "…" },   // who dispatched
  "work":         "work-123",                  // the work item
  "attempt":      "attempt-7",                 // the attempt
  "generation":   7,                           // monotonic operation revision — the fence
  "route":        "gitlab-ce",                 // github-actions | azure-pipelines | gitlab-ce
  "ref":          "anthropic-main",            // WHICH credential may be redeemed
  "mode":         "interactive",               // interactive | batch
  "operation":    "op_2026…",                  // the exact dispatched operation id
  "absolute_deadline": "2026-09-25T14:00:00Z", // hard ceiling; NEVER extended (topic 4)
  "retry_window":      "2026-09-25T10:40:00Z", // lost-response retrieval window (§A.2)
  "redemption_version": 0,                     // CAS counter; 0 = unredeemed
  "redeemed_at":  null,
  "binding":      { "cnf": { "jkt": "…" } }    // OPTIONAL sender binding — §C, reserved now
}
```

**Validation at redemption** (all must pass; any mismatch = 410/403 +
audit, never a partial accept) [inference]:

1. HMAC token resolves to (subject, work, attempt) — then the persisted
   grant is loaded and **every field is compared against live attempt
   state**: `generation`, `route`, `ref`, `mode`, `operation`. This is
   GCP's attribute-condition discipline (reject, don't repair) and STS's
   claim-matched trust policy — project membership is nowhere in the
   chain.
2. `now ≤ absolute_deadline` (server clock; deadline lives in the grant
   record, not in any token). Credential TTL = `min(now →
   absolute_deadline, provider max)`.
3. CAS-increment `redemption_version` under a conditional update; a
   presentation carrying an older version than stored = replay ⇒ revoke
   grant + alert (RFC 9700 §4.14 family semantics; the better-auth race
   lesson).

### A.1 Authority fence across awaited broker resolution

The broker resolves the `ref` asynchronously. Between "grant created" and
"credential materialized" the operation may legitimately change (retry,
steer, re-dispatch) [inference, on the documented patterns]:

- **Re-check `generation` after every `await` resumes** — mirror of GCP's
  "attribute-condition update takes effect immediately" (in-flight
  exchanges fail, by design) and SPIRE's per-connection re-attestation.
  If `attempt.generation != grant.generation` when the broker returns,
  the materialized credential is discarded and the grant is voided — a
  stale controller can never complete a fenced redemption.
- The grant id + version act as a **fencing token** for the credential
  write: materialization is `INSERT … WHERE generation = $g`, so a
  superseded generation cannot overwrite a newer grant's state.
- Broker-resolution wait is itself bounded by the absolute deadline; a
  resolution arriving after it is dropped without materialization.

### A.2 Retry-window semantics for lost responses ("acknowledged-but-lost")

The two documented poles (topic 1 §5): Vault wrapping is at-most-once and
tamper-evident but **has no idempotent retrieval** (a lost response = a
dead grant + fresh issuance); STS/GCP are freely re-callable but allow
parallel redemptions. Recommended blend [inference]:

- **First successful materialization** stores the deliverable
  server-side (sealed; or its ciphertext for the retry window only),
  sets `redeemed_at`, and delivers it.
- **Retries within `retry_window`** (same grant, same version, HMAC +
  proof identity unchanged) return the **same sealed response** —
  idempotent retrieval, so a dropped HTTPS response does not burn the
  grant. This is the retrieval window the majors implicitly get from
  stateless re-issuance, grafted onto Vault-style single-logical-use.
- **After the window**: grant is terminal (`expired|delivered|voided`);
  recovery is a **new grant** (new dispatch), matching Vault's
  "destroy-by-accessor + fresh SecretID" runbook. A failed
  second-unwrap-equivalent (wrong version) is an **alert, not a retry**
  — Vault's core operational lesson.
- Version mismatch inside the window ⇒ replay detection ⇒ revoke grant,
  never serve.

## B. GitLab CE (Free): the native short-lived job-bound credential

Confirmed by docs [documented —
https://docs.gitlab.com/ci/secrets/id_token_authentication.html]:
**`id_tokens` is Tier Free and offered on GitLab Self-Managed** — a CE
lane can mint an RS256 JWT (`aud` = forge) whose claims spell the
operation tuple: `job_id`, `pipeline_id`, `pipeline_source`, `sha`,
`ref`/`ref_type`/`ref_path`/`ref_protected`, `project_id`,
`runner_id`, `runner_environment`, `environment_*`. `exp` = job timeout
or **5 minutes** by default.

CE-compatibility rules for the endpoint design [documented facts;
inference design]:

- Accept **manual OIDC redemption**: job declares `id_tokens`, calls
  forge with the JWT; forge validates `iss`/JWKS/`aud` and binds the
  grant to `job_id`+`pipeline_id`+`sha`+`ref`. Do **not** assume the
  `secrets:` keyword (Vault/GCP/Azure integrations) — **Premium/
  Ultimate on Self-Managed** [documented — /ci/secrets/].
- `CI_JOB_TOKEN` is job-bound ("valid only while the job is running")
  but only GitLab (+KAS) accepts it — it cannot authenticate to forge;
  and Agent-for-K8s **impersonation** (`access_as: ci_job`) is also
  Premium, so no per-job K8s identity on CE either
  [documented — /ci/jobs/ci_job_token/; /user/clusters/agent/].
- Caveat: id_tokens are **exp-bounded, not job-bounded** (no documented
  revocation at job end) — forge's own grant deadline is the fence; and
  GitLab blocks issuance on `project_path` reuse ("prevents a new
  project from inheriting external trust policies") — bind to immutable
  ids, the same discipline [documented].

## C. Sender binding — the NEXT adapter step

The redeemed model key is a bearer at the provider; binding applies to
what forge controls. NEXT step [inference, modeled on RFC 9449 —
documented mechanics in topic 3]:

1. Runner keypair at lane start; grant reserves `binding.cnf.jkt`
   (thumbprint pinned `dpop_jkt`-style at dispatch/first contact).
2. `DPoP:`-style proof (typ `dpop+jwt`, asymmetric alg, `jti`≥96 bits,
   `htm`/`htu`, server-issued **timestamped nonce** — skew-proof) on
   every redemption/refresh call; server rejects bound grants presented
   bare ("MUST reject a DPoP-bound access token received as a bearer
   token").
3. Effect: a stolen lane HMAC token or copied grant cannot redeem or
   refresh elsewhere; only the key holder can. Leaked *responses* remain
   mitigated by short TTL + single grant + absolute deadline (topics 4, 1).

## Top actionable findings (one per topic, 5 lines)

1. **Operation-scoped issuance** (doc 01): every mature platform persists
   an operation-shaped grant — Vault SecretID (metadata/cidrs/num_uses=1,
   accessor-revocable), STS claim-matched trust policy + tamper-proof
   transitive session tags, GCP CEL attribute condition re-evaluated at
   every exchange, SPIRE selector→SVID entry. None natively solves
   "acknowledged-but-lost": Vault is at-most-once with alert-not-retry;
   STS/GCP are stateless re-callable. Forge should persist the grant with
   a version counter + bounded retrieval window to get both properties.
2. **GitLab CE Free** (doc 02): `id_tokens` **confirmed Free on
   Self-Managed** (5-min default exp; `job_id`/`pipeline_id`/`sha`/
   `runner_id` claims) — the CE-native assertion for redemption.
   `CI_JOB_TOKEN` (job-lifetime, inbound-allowlisted) cannot reach third
   parties; the `secrets:` keyword and Agent impersonation are Premium —
   the endpoint must work with manual OIDC only and rely on forge's own
   grant deadline because id_tokens are not revoked at job end.
3. **Sender-constrained credentials** (doc 03): RFC 8471 TLS token
   binding is dead (no browser consensus); DPoP (RFC 9449) is the live
   application-layer standard — per-request proof (`jti`/`htm`/`htu`/
   `ath`), `cnf.jkt` binding at issuance, server-timestamped nonces,
   MUST-refuse-bound-tokens-as-Bearer. NEXT adapter: runner keypair +
   `binding.cnf.jkt` in the grant + proofs on redemption/refresh.
4. **Absolute deadlines vs sliding TTLs** (doc 04): all majors use
   absolute windows set at issuance (Vault max-TTL from creation; STS
   fixed Expiration, re-assume to continue; SPIRE half-life rotation
   never extends; GCP fresh absolute tokens per exchange). The CVE
   gallery (WSO2 CVE-2025-12627, hexpm CVE-2026-75554, better-auth
   CVE-2026-53517, data.all CVE-2024-52311) is one bug in four costumes:
   credential lifetime outran authorization lifetime. Rule: absolute
   deadline in the grant record, refresh re-presents the grant, atomic
   CAS redemption, inactivity may only shrink, job-end revokes.
