# Anthropic / Claude Code CLI: How the Model Credential Is Consumed (2026-09-24)

Which knobs the AI-coding lane actually reads at runtime (`ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_API_KEY`, `apiKeyHelper`, federation), their precedence, and Anthropic's own
CI guidance. Facts from code.claude.com / docs.anthropic.com fetched 2026-09-24;
tagged [documented] / [observed] / [inference].

---

## 1. The credential environment variables

From the official environment-variables reference [documented —
https://code.claude.com/docs/en/env-vars]:

| Variable | Wire form | Behavior |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <value>` ("the value you set here will be prefixed with Bearer") | custom/gateway auth; takes precedence over `ANTHROPIC_API_KEY` |
| `ANTHROPIC_API_KEY` | `X-Api-Key` header | Console key; "In non-interactive mode (`-p`), the key is always used when present"; interactive mode prompts once for approval |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth | long-lived token from `claude setup-token`; "Use this for CI pipelines and scripts where browser login isn't available" |
| `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY` (+ per-cloud keys) | cloud-native | switches to cloud-provider credentials entirely |

- `ANTHROPIC_BASE_URL` reroutes the endpoint (LLM gateway) — pair with
  `ANTHROPIC_AUTH_TOKEN` for bearer-style gateways [documented].
- Env vars are read at startup; settings-file `env` blocks override the inherited
  shell environment [documented].

**forge mapping [inference]:** a redeemed or natively-delivered model credential
should be exported as **`ANTHROPIC_AUTH_TOKEN`** when it is a bearer-style token
(forge-brokered or gateway-issued), or **`ANTHROPIC_API_KEY`** when it is a raw
Anthropic Console key. Both are single-line base64-ish values that mask cleanly in
every provider's redaction (docs 01–03).

## 2. Official authentication precedence (verbatim order)

From the authentication page [documented —
https://code.claude.com/docs/en/authentication#authentication-precedence]:

1. Cloud provider credentials (when `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY` set)
2. `ANTHROPIC_AUTH_TOKEN` (Authorization: Bearer; "Use this when routing through an
   LLM gateway or proxy ... that authenticates with bearer tokens")
3. `ANTHROPIC_API_KEY` (X-Api-Key; `/config` "Use custom API key" toggle governs the
   interactive one-time approval)
4. `apiKeyHelper` script output — "Use this for dynamic or rotating credentials, such
   as short-lived tokens fetched from a vault"
5. `CLAUDE_CODE_OAUTH_TOKEN`
6. Anthropic profile / federation credentials (`ant` CLI, Workload Identity
   Federation; rank here when named via `ANTHROPIC_PROFILE`, else below `/login`)
7. Subscription OAuth from `/login`

**forge consequence [inference]:** in the lane, set exactly ONE of the relevant
variables and scrub the others; a stray higher-precedence variable (e.g. a cached
`ANTHROPIC_AUTH_TOKEN` on the runner image) silently overrides the credential forge
deliberately delivered. `forge-lane` should assert the intended variable is set and
the others empty before launching the CLI.

## 3. apiKeyHelper: the native rotating-credential hook

- Settings field `apiKeyHelper` points at a script that **prints the current
  credential**; Claude Code invokes it and caches the output, refreshing every
  `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` and re-running on 401 [documented — env-vars +
  settings reference; refresh-on-401 observed in community guides].
- This is Anthropic's supported mechanism for "short-lived tokens fetched from a
  vault" [documented wording above].

**forge mapping [inference]:** in the runner-time-redemption profile (doc 04), the
cleanest integration is `apiKeyHelper = forge-lane credential --ref <ref>` (script
redeems via the control plane using the attempt token and prints the value), with
`CLAUDE_CODE_API_KEY_HELPER_TTL_MS` aligned to the redemption TTL — the credential
then auto-refreshes mid-attempt and is never present in the parent process env longer
than needed. Alternative (simpler first cut): redeem once at startup, export, rely on
TTL exceeding the attempt.

## 4. Anthropic's CI guidance (GitHub Actions page)

[documented — https://code.claude.com/docs/en/github-actions]

- "Never commit API keys or OAuth tokens directly to your repository. Always store
  them as GitHub Secrets and reference them in workflows, for example
  `anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}`." — i.e. the **provider-native
  secret + workflow reads it** pattern is Anthropic's documented baseline for CI-run
  agents; org-level secret for sharing across repos; prefer Console API key over OAuth
  token for org sharing.
- The zero-long-lived-secret upgrade path is Workload Identity Federation: the action
  "exchanges the workflow's GitHub OpenID Connect (OIDC) token for Claude API access
  through a Claude Console service account" (`anthropic_federation_rule_id` +
  `anthropic_organization_id`/`anthropic_service_account_id`/`anthropic_workspace_id`,
  `id-token: write`); cloud providers (Bedrock/Vertex/Foundry) likewise via OIDC
  federation, "so you store no static cloud credentials in your repository."
- Headless/automation mode = supply a `prompt` input to `claude-code-action` and run
  without mentions; non-interactive `-p` uses `ANTHROPIC_API_KEY` automatically
  [documented].
- Agent SDKs inherit the same env-var contract (the SDK embeds the CLI), so the lane's
  export covers both CLI and SDK harnesses [inference from shared env-var surface].

## 5. Answer to the key question

The lane consumes the credential via `ANTHROPIC_AUTH_TOKEN` (bearer) or
`ANTHROPIC_API_KEY` (x-api-key), resolution order fixed by the documented precedence
list; delivery should be (per Anthropic's own CI docs) a provider secret referenced at
run time, upgraded to OIDC-federation/broker redemption when rotation and zero stored
secrets matter. `apiKeyHelper` is the first-class hook for forge's short-lived
redeemed tokens.

## Sources

- Environment variables (full table incl. AUTH_TOKEN/API_KEY/apiKeyHelper TTL,
  settings-env override rules): https://code.claude.com/docs/en/env-vars
  (docs.anthropic.com mirror: /en/docs/claude-code/env-vars)
- Authentication precedence (ordered list, verbatim):
  https://code.claude.com/docs/en/authentication
- Claude Code GitHub Actions (secrets guidance, federation inputs, automation mode):
  https://code.claude.com/docs/en/github-actions
- apiKeyHelper refresh-on-401 + 5-minute cache default, header semantics:
  kunavo.com Claude Code API-key guide 2026; blog.laozhang.ai config guide 2026;
  fazm.ai gateway notes 2026 [observed].
