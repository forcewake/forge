# Existing Claude lane

The current batch recipe is
[claude-code.gitlab-ci.yml](../../ci/templates/claude-code.gitlab-ci.yml);
the SDK recipe is
[claude-sdk-lane.gitlab-ci.yml](../../ci/templates/claude-sdk-lane.gitlab-ci.yml).
GitHub uses the [Actions harness](../../ci/templates/forge-harness.github.yml).
These are separate from the new `forge_lane` driver.

Provision `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` for the selected
route; never give the lane forge's publisher credential. Keep the reviewed
CLI/version pin and record `claude --version`. The gateway's model name is
significant: a required `[1m]` suffix cannot be dropped silently.

The renderer owns headless flags, settings isolation, strict MCP config,
turn limits and commit/push denials. Do not bypass a denial by editing a
running job. Optional `FORGE_HARNESS_MCP` is translated from the canonical
configuration; secret references use separate masked variables.

Usage result events feed the lane's receipt; incomplete metering remains
explicit. Authentication errors, permission denials and network resets
need their own diagnosis. A fast-path `FORGE_HARNESS_HTTPS_PROXY` may help
throttled streams but is deployment configuration, not an assumed fix.
Start with [onboarding](onboarding.md) and inspect the exact pinned
rendered script. Historical live observations apply only to their named
version and remain in the [archive](../archive.md).
