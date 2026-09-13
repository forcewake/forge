# Token and secret rotation (runbook)

Four secrets rotate on different clocks. None of them lives in forge's
database, so rotation never touches run state.

| Secret | Where it lives | Rotation |
|--------|----------------|----------|
| Bot PAT (`FORGE_BOT_TOKEN`) | GitLab user `forge` → Access Tokens; project CI variables; forge env | 90 days recommended (current lab token expires 2026-10-31) |
| Webhook secret (`GITLAB_WEBHOOK_SECRET`) | GitLab project webhook + forge env | 180 days, or immediately on suspicion |
| Model API key (`ZAI_API_KEY`/`ANTHROPIC_AUTH_TOKEN`) | Provider console; LiteLLM config; project CI variables | Provider-driven; immediately on leak |
| Operator/admin token | Your secrets store; never in forge | Your policy |

## Bot PAT rotation (no lost runs)

The token is read at call time from the environment — restart is the only
propagation step:

1. Create the new PAT for the bot user (same scopes, same user — run
   identity and gate audit rows must stay stable).
2. Update it in the project CI variables (`FORGE_BOT_TOKEN`) and in forge's
   env (`FORGE_BOT_TOKEN`).
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
