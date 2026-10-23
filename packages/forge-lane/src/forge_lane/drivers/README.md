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
