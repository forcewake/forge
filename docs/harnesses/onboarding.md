# Configure the current CI lane

Current GitHub and GitLab services delegate implementation to the existing
CI lane. The builtin implementer is removed. The separate `forge-lane`
package and `/lane/v1` API are a migration target; installing them does not
switch current dispatch automatically. See [lane v1](lane-v1.md).

## 1. Approve the execution environment

Use disposable unprivileged containers, never a shell runner, Docker socket,
production secrets or broad internal-network access. Permit only required
provider, package-registry and model traffic. The lane receives no publisher
token or control-plane database credentials. Its checkout cannot push.
Bound job wall-clock and the server's harness deadline.

## 2. Install the matching template

For GitHub, copy the reviewed
[Actions harness](../../ci/templates/forge-harness.github.yml) to the target
repository and configure `FORGE_GITHUB_HARNESS_WORKFLOW`. It must expose
`workflow_dispatch` on the selected source ref. For GitLab, include the
matching reviewed [template](../../ci/templates/) on the default branch;
the compiled configuration must contain the selected lane job.
Pin the server wheel/ref and template from one compatible composition.
Avoid moving `main` includes in operational deployments.

Admission reads the actual CI configuration before paid planning. Missing,
unreadable or incompatible setup returns `lane_not_onboarded`. An auth error
is not permission to fall back to a builtin implementation.

## 3. Provision only the selected lane credentials

Use masked CI secrets for the selected driver; optional
`FORGE_BOT_READ_TOKEN` is read-only. See [Claude](claude-code.md),
[Copilot](copilot-cli.md), [Grok](grok-build.md) and [OpenCode](opencode.md).
Provision native carriers or runner-redemption bindings through the
[credential runbooks](../operations/credential-rotation.md). A protected
secret unavailable on factory refs must be corrected before dispatch,
without an ambient duplicate. `FORGE_HARNESS_HTTPS_PROXY` is optional for
throttled provider streams; do not expose proxy credentials in traces.

## 2b. MCP servers in the lane (FORGE_HARNESS_MCP, ADR-0022)

`FORGE_HARNESS_MCP` carries canonical `mcpServers` JSON. Put secret values in
separate masked variables and reference them as `${NAME}`. The renderer
translates the configured servers to each driver's dialect; unknown or
invalid configuration refuses. MCP access is an explicit lane capability,
not authority to widen repository writes. The removed forge MCP server is
unrelated to this lane-side client configuration.

## 4. Dispatch and result

The server freezes the brief, model, driver and attempt base. The lane
checks out that base, executes the brief and returns only `candidate.diff`
and `candidate.meta.json` in its candidate artifact. Logs remain diagnostic.
The publisher checks the approved digest, generation, grant, current head,
patch applicability and path policy before its journaled commit/Draft write.
The project's own required CI then verifies that exact candidate SHA.

An empty initial diff is `harness_no_changes`; an empty repair is
`repair_no_effect`. Wrong/missing base, malformed/binary patch, missing
artifact or path violation refuses publication. A completed vendor turn is
not proof of a useful or independently verified candidate.

## 5. Pause and resume

Existing dispatches include resume mode, exact checkpoint, generation and
continuation decision. Required resume must restore verified WIP before any
model turn; failure is not a fresh-start fallback. SDK lane templates
support this path; batch templates refuse required resume. Steering needs
its explicit control configuration and cannot change approval or scope.
See [runtime recovery](../operations/adaptive-runbook.md).

Run `forge doctor --project <id>` and complete an authorized disposable
first issue → `/go` → candidate → independent CI → Draft workflow. Local
rendering alone is not live acceptance. Historical examples remain at
`docs/harnesses/onboarding.md` in the [archive](../archive.md).

## Writing the task: what the agent actually reads

Name the desired behavior, acceptance checks and permitted repository/path
scope in the issue. The agent executes the frozen approved brief, not an
unapproved later comment. Put material changes through the existing human
gate. A neighboring repository is read-only and needs explicit authorization;
mentioning it does not grant access or widen publication scope.
