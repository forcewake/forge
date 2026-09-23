# Token and secret rotation (runbook)

Four secrets rotate on different clocks. None of them lives in forge's
database, so rotation never touches run state.

| Secret | Where it lives | Rotation |
|--------|----------------|----------|
| Bot PAT (`FORGE_BOT_TOKEN` in forge env; optional read-only `FORGE_BOT_READ_TOKEN` in project CI variables) | GitLab user `forge` → Access Tokens; forge env; (read-only variant) project CI variables | 90 days recommended (current lab token expires 2026-10-31) |
| Webhook secret (`GITLAB_WEBHOOK_SECRET`) | GitLab project webhook + forge env | 180 days, or immediately on suspicion |
| Model API key (`ZAI_API_KEY`/`ANTHROPIC_AUTH_TOKEN`) | Provider console; LiteLLM config; project CI variables | Provider-driven; immediately on leak |
| Operator/admin token | Your secrets store; never in forge | Your policy |

## Bot PAT rotation (no lost runs)

The token is read at call time from the environment — restart is the only
propagation step:

1. Create the new PAT for the bot user (same scopes, same user — run
   identity and gate audit rows must stay stable).
2. Update it in forge's env (`FORGE_BOT_TOKEN`); if the project uses the
   optional read-only lane token, update `FORGE_BOT_READ_TOKEN` in the
   project CI variables. The lane must never hold a write token (ADR-0016).
3. Recreate `forge-app` and `forge-worker` (in-flight runs are durable:
   `waiting_harness`/`waiting_ci` runs are resumed by the reconciler; runs
   in worker-held states simply resume processing after restart).
4. Revoke the old PAT only after `forge doctor` exits 0.

## Webhook secret

1. Set the new value in forge's env, restart `forge-app`.
2. Update the webhook secret in every onboarded project (old deliveries
   between the two updates get 401 — GitLab retries them, forge accepts
   once the new secret is live; ordering above avoids most retries).
3. Never commit the secret; `data/captured/` payloads are not secret but
   honor the [retention policy](audit-retention.md).

## Model keys

Update LiteLLM's config/env and the project CI variables that the harness
jobs read, then restart `forge-litellm`. In-flight harness jobs already
running keep their env — a key revoked mid-run fails that job honestly
(classified `harness_infrastructure` when auth patterns appear in the
trace) and the run blocks or repairs per the standard rules.

## Legacy lane-token drain and rotation (Q35-06)

The lane credential has two spellings: the standing
**generation-scoped token** (`HMAC(secret, work_id:generation)`, minted
by each dispatch for the run's current attempt) and the **legacy
work-scoped token** (`HMAC(secret, work_id)`, no generation), kept
working only inside a bounded migration window. That window no longer
depends on process lifetimes — resolve it once, and every restart
agrees:

| Anchor | Configuration | Restart-stable? |
|--------|---------------|-----------------|
| Explicit deadline | `FORGE_LEGACY_CREDENTIAL_DEADLINE` (ISO date; the older `FORGE_LANE_LEGACY_TOKEN_DEADLINE` spelling is honored) | Yes — operator state |
| Recorded start | `FORGE_LANE_LEGACY_TOKEN_START` + 30 days | Yes — operator state |
| Persisted anchor file | written **once** on first resolution; default `lane-legacy-credential-anchor` under `FORGE_CHECKPOINT_STORE_DIR` (override with `FORGE_LEGACY_CREDENTIAL_ANCHOR_FILE`) | Yes — deployment state |
| Nothing persistable | — | Legacy acceptance **refused** (fail-closed); generation tokens unaffected |

Operator rules:

1. **Set an explicit deadline on promotion.** A newly promoted profile
   should record `FORGE_LEGACY_CREDENTIAL_DEADLINE` from day one; the
   value is a fixed instant both processes started on either side of it
   agree on. A date already in the past is valid (the window is closed);
   a malformed, empty, or out-of-sanity-bounds value is refused —
   `forge doctor` reports it as `credential.configuration_invalid` and
   the APIs refuse legacy tokens with the same diagnostic rather than
   silently opening a fresh window.
2. **Do not delete or hand-edit the anchor file.** It is write-once
   deployment state: once it exists, its instant is the migration start
   forever. Corrupt or missing-and-unwritable state refuses legacy
   tokens (fail-closed) — restore from backup, or pin the window with
   an explicit deadline instead.
3. **Drain before expiry.** `forge doctor`'s `credential.legacy_deadline`
   check shows the anchor source, the deadline, days remaining, and the
   count of grandfathered generation-less works still visible
   (`cancellation_generation = 0`). Warn while that count is non-zero.
   Drain means: let those attempts finish or retry them — the retry
   dispatch mints a generation-scoped token; run history is never
   rewritten.
4. **Rotate `FORGE_LANE_CONTROL_SECRET` with an overlap.** Both token
   spellings derive from the same secret, so a rotation pairs with the
   migration window, not against it: deploy the new secret (restart),
   then re-dispatch the live attempts so their lanes hold tokens under
   the new secret, then revoke nothing further — old attempts die with
   their tokens at the deadline. A secret rotated without re-dispatch
   strands in-flight lanes exactly as any expiry would; the recovery is
   the retry/re-dispatch path, never widening the window.
5. **After the deadline, legacy stays dead.** A restart cannot revive a
   legacy token: the deadline is derived from recorded state, not from
   the new process's start. Generation-scoped tokens are unaffected by
   the window in either direction.
