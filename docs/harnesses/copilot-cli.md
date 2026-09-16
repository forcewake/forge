# Harness: GitHub Copilot CLI

Template: `ci/templates/copilot.gitlab-ci.yml` · driver id `copilot`
· interface notes: [research/harness-interfaces.md §7](../research/harness-interfaces.md)
· research addendum: [harness-config-best-practices.md §8](../research/harness-config-best-practices.md)

GitHub's terminal agent (npm `@github/copilot`), run headless via `-p`.
The forge posture: scoped grants so nothing can prompt, with the
mechanical `--deny-tool` on commit/push — documented Copilot rule: **deny
always wins** over every allow, including `--allow-all`.

## CI variables

| Variable | Required | Purpose |
|---|---|---|
| `COPILOT_GITHUB_TOKEN` | ✅ | fine-grained PAT with the **"Copilot Requests"** permission. Precedence: `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN`. **Classic `ghp_` tokens are NOT supported**. Model spend rides on the Copilot subscription behind the PAT |
| `COPILOT_MODEL` | — | optional model override (`--model`); unset = vendor default. Copilot model names do NOT follow forge's RunSpec model routes — that is why this driver has its own variable |
| `FORGE_BOT_READ_TOKEN` | — | optional READ-ONLY repo PAT |

## Headless invocation (what the lane runs)

```
copilot -p "<task pointer>" \
  --allow-tool 'read,write' \
  --allow-tool 'shell(git:*)' \
  --deny-tool 'shell(git commit)' --deny-tool 'shell(git push)' \
  [--allow-tool <mcp-server> ...]
```

- **Scoped grants**: reads, file writes, and read-only git — everything
  else is auto-denied in `-p` mode (no prompt, no hang).
- **Mechanical deny**: `--deny-tool 'shell(git commit)'` /
  `'shell(git push)'` beat every allow and any saved approvals
  (`permissions-config.json`); the lane's push URL is FORBIDDEN anyway.
- **Session-scoped flags**: grants are never persisted — the ephemeral
  lane starts clean every time.

## MCP

`mcpServers` (canonical JSON) written to `~/.copilot/mcp-config.json` —
the documented Copilot CLI location — plus an explicit
`--allow-tool <server>` grant per configured server. `${VAR}` references
inside the config expand at runtime.

## Usage receipts

None parseable on stdout (upstream issue #52; the JSON-RPC SDK is not a
lane fit) — the candidate meta records `usage: null` (unknown ≠ zero).

## Live notes / gotchas

- Installed via the retrying npm preamble (`@github/copilot`);
  `copilot --version` lands in the trace. Node 22 image — no extra
  runtime needed.
- Billing/multipliers are subscription-side: budget classes (ADR-0023)
  feed the F22 ledger, but Copilot's own metering is opaque to forge —
  treat this lane's cost estimates as the least precise of the four.
- Enterprise managed-settings policies (VS Code "AI settings") also
  govern Copilot CLI — check them if grants behave unexpectedly on
  managed machines.

## Triage

| Symptom | Look at |
|---|---|
| `Tool not allowed` denials on file writes | the grants — `read,write` must be present; MCP servers need their own `--allow-tool` |
| auth 401 at startup | the PAT type — classic `ghp_` is rejected; fine-grained with Copilot Requests only |
| no commits in the candidate | expected: the driver never commits; the candidate is the working-tree diff |
