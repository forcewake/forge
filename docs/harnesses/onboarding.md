# Configure the Forge 1.0 lane

Forge uses one `forge-lane` protocol and two supported drivers:
[`claude_code`](claude-code.md) and [`copilot_cli`](copilot-cli.md).
The lane runs in the target's CI; the server owns approval, budget and
publication. The lane receives no publisher credential and cannot push.

## GitHub

Copy the [target dispatch wrapper](../../templates/github/forge-lane.yml)
to `.github/workflows/forge-lane.yml` in the target repository. It accepts
`workflow_dispatch` and calls the sole
[reusable lane](../../.github/workflows/lane.yml). Review the workflow reference
and immutable lane requirements before enabling the repository.

Configure that target path as `lane_workflow`. Preserve `contents: read`,
`id-token: write` and checkout without persisted credentials. For Copilot,
provide the wrapper's `FORGE_COPILOT_TOKEN` CI secret.

## GitLab

Include the reviewed [lane job](../../ci/lane.gitlab-ci.yml) in the target's
configured CI file. It consumes the server's dispatch and verified requirements,
requests the configured OIDC audience and disables checkout push access.
Provide `COPILOT_GITHUB_TOKEN` only when selecting Copilot.

## Execution and verification

The workflows install the hash-verified lane distribution without the server
wheel, then invoke:

```sh
forge-lane run --dispatch /path/to/dispatch.json --workspace /path/to/checkout
```

The server supplies the exact frozen dispatch. Do not construct replacement
approval bytes or inject driver/model overrides. Configure repository identity,
OIDC trust, the approved driver/model, budgets and required independent native
checks through the current server configuration.

Use an isolated CI runner with only the necessary credentials. File-tool policy
is not a host sandbox or network firewall. A pause must persist its checkpoint
before acknowledgment; resume names that exact checkpoint in a new generation.
Successful lane completion remains separate from independent verification and
human review of the Draft PR/MR.
