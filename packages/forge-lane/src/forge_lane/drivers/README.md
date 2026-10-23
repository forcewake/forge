# First-class lane drivers

`claude_code` uses the pinned `claude-agent-sdk` wheel and its bundled native
CLI. Install `forge-lane[claude]`. It requires the attempt's gateway grant;
ambient Anthropic, GitHub and GitLab credentials are not forwarded. There
is no direct-provider or alternate-model fallback.

The current fixed policy provides Read, Write and Edit inside the checkout.
It refuses shell execution, repository APIs, MCP, skills, authority files,
repository metadata and credential files. A safe path inventory supplies
navigation. Permission callbacks and a PreToolUse hook both enforce the
policy. This is a deliberately restricted execution capability, not a claim
that a regular expression isolates arbitrary shell programs. CI must still
provide an isolated checkout and read-only repository credentials.

Configuration and the SDK home live outside the checkout and are removed
on every exit. The CLI has its own process group; interrupt waits for writes
to stop. Capabilities are interrupt and steer. The driver does not advertise
cross-generation conversation resume because its private transcript is
removed; filesystem resume remains the runner's guarantee.

Usage token counts are accumulated once per result. The SDK cost value is
the latest cumulative estimate, not a billing receipt. Tests use a fake SDK;
no model calls are needed to verify this adapter.

Verified API references:

- <https://code.claude.com/docs/en/agent-sdk/python>
- <https://code.claude.com/docs/en/agent-sdk/permissions>
- <https://code.claude.com/docs/en/agent-sdk/cost-tracking>

## Copilot CLI

`copilot_cli` talks ACP v1 to the pinned native Copilot CLI 1.0.86. Install
`@github/copilot@1.0.86` and the matching explicit platform package, such as
`@github/copilot-linux-x64@1.0.86`, at the same version. The driver verifies
that pin before starting. Set `COPILOT_GITHUB_TOKEN` from the lane's CI secret;
`GH_TOKEN`, `GITHUB_TOKEN`, stored login and BYOK are not authentication
fallbacks. The frozen model is passed explicitly and auto-update is off.

The CLI exposes view/edit/create/apply_patch only. Shell, network tools,
built-in GitHub MCP and custom instructions are disabled. Permission requests
must carry a recognized file input inside the workspace; informational
locations alone grant nothing. Patch headers and all target paths are
validated. Safe operations receive allow_once, never allow_always. A rejected
permission remains a typed refusal even when the CLI returns end_turn with
zero output. Native path restrictions and CI isolation remain structural
requirements; this adapter does not claim arbitrary shell isolation.

Capabilities are interrupt only. Cancellation is recorded before the ACP
notification because an interrupted CLI can return end_turn. No interrupted
turn becomes completion. Startup, execution and disconnect own a process
group and private configuration. stderr is bounded and redacted. Usage is
unmetered: ACP supplies no usage receipt, so the driver returns none.

References:

- <https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server>
- <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>
- <https://agentclientprotocol.com/protocol/v1/tool-calls>
