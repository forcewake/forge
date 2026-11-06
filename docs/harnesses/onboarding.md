# Configure the Forge 1.0 lane

Forge uses one `forge-lane` protocol and two supported drivers:
[`claude_code`](claude-code.md) and [`copilot_cli`](copilot-cli.md).
The lane runs in the target's CI; the server owns approval, budget and
publication. The lane receives no publisher credential and cannot push.

## GitHub

Use the verified `forge-lane.github.yml` asset from the selected release
manifest. Check its `github_wrapper_sha256`, then copy those exact bytes to
`.github/workflows/forge-lane.yml` in the target. Its reusable workflow reference
must equal `github_workflow_ref`, including the full `workflow_commit` SHA.
Tags such as `@v1`, branches and unresolved source-template markers are refused
by the release artifact builder. The source
[wrapper template](../../templates/github/forge-lane.yml) requires rendering;
it is not a ready-to-copy configuration.

Configure that target path as `lane_workflow`. Preserve `contents: read`,
`id-token: write` and checkout without persisted credentials. For Copilot,
provide the wrapper's `FORGE_COPILOT_TOKEN` CI secret.

## GitLab

Use the verified `forge-lane.gitlab-include.yml` asset, checking its
`gitlab_include_sha256`. Store it as a target-owned local include and reference
it from the target CI configuration. It pins the reviewed
[lane job](../../ci/lane.gitlab-ci.yml) by full source commit. The dispatched job
uses the configured OIDC audience and disables checkout push access.
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

## Build a local candidate

From the source checkout, build all three distributions and verify the isolated
lane installation without contacting a provider or starting a model session:

```sh
uv run --no-project --python 3.13 python -m tools.release.bundle --repo OWNER/REPOSITORY \
  --rc v1.0.0-rc.LOCAL_ID --output /private/path/forge-candidate
```

The output contains hashes, exact wrapper/include/reporter assets, an install
receipt and a manifest that explicitly records unpublished assets and dirty
source. A local install proof does not make its future HTTPS asset URLs live.
Use `--offline` only when build tools and locked dependency wheels are already
cached. Normal builds can download packages; installation still checks every
dependency hash.
Configure the server with the published manifest's `lane_requirements_url` and
`lane_requirements_sha256`; do not substitute server runtime requirements.

For independent native test failure evidence and bounded repair, use the
[current CI repair setup](../operations/ci-repair.md) and its verification
workflow templates. The coding lane remains read-only toward the repository.
