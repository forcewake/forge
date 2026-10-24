# Claude Code

The supported driver is `claude_code` in the separate `forge-lane` package.
It uses the pinned Claude Agent SDK and its bundled CLI. The release lane
requirements must include `forge-lane[claude]`; the server wheel does not run
inside the lane. Follow [onboarding](onboarding.md) for either provider.

The approved RunSpec binds the driver, model, write scope, execution policy
and limits. Claude receives this attempt's scoped gateway grant. Ambient
Anthropic credentials and a different model are not authentication or
selection fallbacks.

The driver permits confined file operations under the frozen policy. It
rejects shell and repository-write tools, credential files and authority
configuration. It uses a private SDK home and does not inherit ambient
settings. These controls do not establish host isolation or an egress firewall;
the CI runner must provide its execution boundary.

Interrupt and steer are supported. Pause stops writes before checkpointing.
Resume restores exact filesystem WIP in a new generation; it does not promise
conversation continuation. SDK usage estimates are not billing receipts;
unknown usage never becomes zero spend.
