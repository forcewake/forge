# Harness: Claude Code

Template: `ci/templates/claude-code.gitlab-ci.yml` · driver id `claude-code`
· Actions lane: `forge.harness_entry` · interface notes:
[research/harness-interfaces.md §2](../research/harness-interfaces.md)

Anthropic's coding agent, run headless (`-p` print mode). The forge
posture: settings isolation + scoped shell + mechanical commit/push deny.

## CI variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | ✅ | API key for the model endpoint — a gateway (z.ai) works; never forge's own key |
| `ANTHROPIC_BASE_URL` | ✅ | Anthropic-compatible endpoint |
| `ANTHROPIC_MODEL` | — | defaults to `glm-5.3-flash[1m]` in the template; the `[1m]` suffix selects the 1M-token window — without it Claude assumes 200k and warns |
| `FORGE_CLAUDE_VERSION` | — | npm pin (default `latest`); pin a tag for reproducible lanes |
| `FORGE_BOT_READ_TOKEN` | — | optional READ-ONLY repo PAT (ADR-0016: the lane never gets a write token) |

## Headless invocation (what the lane runs)

```
claude -p "<task pointer>" \
  --allowedTools "Bash(git status:*),Bash(git diff:*),Bash(git log:*)" \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode acceptEdits \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose
```

- **Mechanical deny**: commit/push are `--disallowedTools` — they hold even
  if a permission escalation is attempted; the lane's push URL is `FORBIDDEN`
  regardless.
- **No prompts**: `--permission-prompts none` + `-p` semantics — un-allowed
  tools are denied, never awaited.
- **Settings isolation**: `--setting-sources ''` — repo/user settings,
  hooks and skills from outside the brief are not loaded.
- **Timeout budgets**: `API_TIMEOUT_MS=3000000`,
  `BASH_DEFAULT_TIMEOUT_MS=300000`, `BASH_MAX_TIMEOUT_MS=600000` — long
  tool calls must not die at client defaults.
- **Install**: retrying npm preamble with the `FORGE_CLAUDE_VERSION` pin;
  `claude --version` lands in the trace.

## MCP

`--strict-mcp-config` is ALWAYS on: servers come only from
`FORGE_HARNESS_MCP` (canonical JSON → `/tmp/forge-mcp.json`); per server,
`mcp__<name>__*` + `mcp__<name>` tool grants are appended to the
allowlist. `${VAR}` references inside the config expand at runtime —
server keys stay in separate masked variables. See
[harness-onboarding §2b](../harness-onboarding.md#2b-mcp-servers-in-the-lane-forge_harness_mcp-adr-0022).

## Usage receipts

Per-turn `result` events carry usage; the lane's event filter aggregates
them into `.forge/usage.json` → candidate meta (completeness: aggregate).

## Live notes / gotchas

- The `[1m]` model suffix matters (z.ai catalog).
- Long streaming turns can hit connection resets on some networks — the
  stream-json events land in the job trace for diagnosis; the
  `FORGE_HARNESS_HTTPS_PROXY` lane variable fixed exactly this on the lab
  (identical prompt: 226 s direct vs 2 s via a fast-path proxy).
- Live-verified on all three providers (GitLab CI, GitHub Actions, Azure
  Pipelines — the AzDO lane pins Python 3.13 via `UsePythonVersion`).

## Triage

| Symptom | Look at |
|---|---|
| empty candidate, exit failed | the driver step log: `API-ERROR retry` lines = provider trouble; `permission_denials` = a needed tool was not allow-listed |
| hang at startup | npm flake — the preamble retries 3×; check the registry reachability |
| MCP "not valid JSON" | the `FORGE_HARNESS_MCP` variable is unset in that CI system (undefined variables arrive as literal `$(NAME)` text on Azure DevOps — harness_entry treats that as unset) |
