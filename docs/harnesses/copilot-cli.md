# Copilot CLI

The supported driver is `copilot_cli`. It speaks ACP to the pinned native
Copilot CLI; the [current workflows](onboarding.md) install the matching
platform binary and the driver checks its version before execution.

Supply the explicit `COPILOT_GITHUB_TOKEN` CI credential. The GitHub wrapper
maps the target's `FORGE_COPILOT_TOKEN` secret to it. Stored login,
`GH_TOKEN`, `GITHUB_TOKEN`, BYOK and the publisher credential are not fallbacks.
The frozen policy must explicitly select `copilot-scoped-unmetered` metering.
Missing native usage is not a zero-cost receipt.

The driver allows confined file operations under the approved write scope.
Shell, repository APIs and MCP are unavailable. Repository driver configuration
that cannot be isolated is refused. These tool restrictions do not claim host
or network isolation.

Interrupt is supported; steer and cross-generation conversation resume are not.
A paused run resumes through exact filesystem checkpoint restoration in a new
generation. Interrupted `end_turn` is not successful completion.
