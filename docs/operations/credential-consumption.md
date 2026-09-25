# Credential consumption and rotation at the runner boundary (R38-04)

How a bound model credential is *consumed* by a dispatched lane, and how
rotation behaves around an in-flight attempt. The delivery half (which
transport carries the credential to the runner) is
[#303 / R38-02](https://github.com/forcewake/forge/issues/303); this note
is the consumption half ([#305 / R38-04](https://github.com/forcewake/forge/issues/305)).

## The consumer receipt

The trusted runner bootstrap emits a **consumer receipt** (schema
`forge.credential.consumer-receipt/1`) after secret staging — under
`runner-redemption`, right after `redeem_lane_credential()` applied the
value to exactly one env slot; under the native modes, at lane start
(the shipped template's resolution snippet staged the secret before the
lane process ran). It joins, value-free:

| Field | Meaning |
|---|---|
| `redemption_id` / `broker_receipt_id` | Correlate with the control plane's durable `credential_redemptions` audit row and the broker's own receipt |
| `binding_revision` | Which binding revision paid for the attempt (unknown runner-side under native transports — recorded `null`, never guessed) |
| `attempt_generation` / `work_id` | The attempt the credential was staged for |
| `consumer_identity` | The runner job (`CI_JOB_ID` / `GITHUB_RUN_ID` / `BUILD_BUILDID`, else `host#pid`) |
| `env_var` | The delivered env-slot NAME — never the value |
| `credential_policy`, `resolved_version_kind` | The policy in force and the version's kind (below) |

The receipt rides BOTH durable lane journals — the candidate meta
(`credential_consumption`) and `.forge/steering.json` — on every
post-staging exit path, with an honest `consumer_status`: `consumed`
only when the driven turn completed; `staged-unresolved` otherwise. A
failed consumer bootstrap (the model endpoint down after staging)
preserves the unresolved delivery record and claims no model usage.

## Resolved-version honesty

An EnvBroker "version" (`env:<VAR>:present`) is a **presence stamp**, not
a secret-version fingerprint. Every receipt, dispatch proof, delivery
plan and consumer receipt shows the version's `kind` beside it:
`presence`, `fixture` (test double), `binding-revision` (the native
transports' attribution axis — no CI secret facility exposes a version
id), or `secret-version` (a real provider version id; the only kind
treated as a unique-secret-version proof).

## Rotation

- **Between approval and dispatch**: typed refusal (`rotated` /
  `delivery_route_unsupported` family) — never a silent substitution.
- **After dispatch**: the launched lane's staged snapshot is that
  attempt's generation; a rotation never mutates it. The next
  authorized attempt resolves the new revision.

## The JSON registry's multi-worker contract

`ConcurrentCredentialRegistry` (a drop-in for `ProjectCredentialRegistry`
over the same JSON document) makes the prototype's semantics explicit:

- **Writes** take an exclusive `flock` on `<document>.lock`, re-load
  under the lock, and land via atomic rename — no torn documents, no
  lost read-modify-writes between writers.
- **Reads** observe another worker process's change within the stat TTL
  `FORGE_CREDENTIAL_REGISTRY_TTL_SECONDS` (default 5s). Within the TTL
  the cached view stands — that is the documented propagation bound.
- The JSON store remains a **prototype**: audit-grade rotation history
  and cross-node propagation stay the production Postgres swap's.

## The credential policy

`FORGE_CREDENTIAL_POLICY` ∈ {`compat`, `strict-broker`} (default
`compat`):

| Route | `compat` | `strict-broker` |
|---|---|---|
| Bound subject, declared route | delivered (receipted) | delivered (receipted) |
| Unbound subject (registry has bindings) | `None` — ambient-legacy, labeled | typed refusal `strict_unbound_route` |
| Unknown route / profile (registry has bindings) | `None` — ambient-legacy, labeled | typed refusal `strict_unbound_route` |
| Any route, empty registry | `None` — ambient-legacy | `None` — ambient-legacy |

Unknown route, missing connection and absent binding never claim
project-bound operation in either policy. A malformed policy value is a
typed failure, never a silent re-default. The policy is stamped in every
receipt.

## Secret hygiene

Resolved values are wrapped in `SecretValue`: `repr`/`str` carry the
length class only (`<secret len=short>`), so dataclass reprs, exception
messages and log lines cannot leak the material. The deliberate unwrap
is `reveal_secret()` at the env-application and redemption-response
seams only. Values containing newlines, quotes or marker-like text never
appear in logs, receipts, proofs or audit exports (tested adversarially).
