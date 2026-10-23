# Experimental contrib drivers

Codex app-server, OpenCode v2 `serve`, and the Grok headless CLI are
community integrations. They have deterministic fake-protocol smoke tests;
they have no first-class nightly compatibility guarantee or live acceptance
claim. Vendor protocol drift can make a driver unavailable. The maintained
first-class CI inventory remains Claude Code and Copilot CLI.

Install `forge-lane[contrib]` and explicitly set
`FORGE_LANE_ENABLE_CONTRIB=1` in the lane job to activate `codex`, `opencode`
or `grok`. The extra currently reserves optional dependency declarations;
its code is included in the normal wheel. Installed extras are not a
reliable runtime flag. The environment opt-in controls loader activation;
it does not create an operating-system security boundary.

Install and pin the matching vendor CLI separately, including its platform
binary where the npm wrapper uses optional dependencies. The lane never
installs an executable, updates it automatically, attaches to an existing
server, or falls back to a different protocol. Use an ephemeral Linux CI
workspace with only the approved checkout and its explicit credentials.
The host's process isolation remains part of the CI deployment contract.

| Driver | Capabilities | Model authentication | Completion authority |
| --- | --- | --- | --- |
| `codex` | interrupt, steer | Exact attempt gateway grant | Matching `turn/completed` status |
| `opencode` | interrupt | Exact attempt gateway grant | New assistant transcript `stop` or `error` |
| `grok` | interrupt | Explicit masked `FORGE_GROK_AUTH` subscription bundle | Successful terminal event and zero process exit |

None advertises native session resume. Checkpoint resume supplies the saved
workspace and a resume note to a new vendor session. Missing usage remains
unknown (`usage=None`); these drivers do not invent zero-cost accounting.
The lane supervisor still owns deadlines, cancellation and outcome policy.
Every terminal result follows cleanup of the owned process group. Interrupt
also closes readers and the private configuration directory.

Each child receives a new private home and a minimal environment, without
GitHub, GitLab, CI OIDC or ambient model-provider credentials. Codex and
OpenCode receive only the attempt-scoped model grant at the selected gateway;
stored vendor accounts and ambient API keys cannot override it. The current
server model gateway must support the selected vendor wire protocol before
BYOK can run; an unavailable gateway is an error, not an account fallback.

Codex requires experimental dynamic file tools and effective configuration
that confirms shell tools, web search, apps and MCP are disabled. It uses a
read-only native sandbox; the lane mediates only `read_file`, `write_file`
and `edit_file`. Paths outside the checkout, authority files, symlink escapes,
unknown arguments and command tools are refused. Steering carries only text
and the expected active turn ID. A turn acceptance response is not completion.

OpenCode starts a new password-protected server on loopback. It disables
project configuration, external skills and plugins, selects the gateway
provider/model before sending a prompt, and requires a confirmed session
`ask` permission ruleset. It permits each individual read/write/edit request
only for a concrete permitted checkout file. Shell, wildcard, authority-file
and other unknown requests are rejected. The required v2 routes are probed;
there is no v1 downgrade. Transcript polling deliberately avoids depending
on non-replayable SSE events. Tool-call rounds and empty transcripts cannot
complete a run. This is a strict subset of the vendor tool surface.

Grok requires the CI OAuth JSON bundle explicitly; `XAI_API_KEY` and stored
accounts are not used. The bundle is written to a private mode-0600 file and
removed at shutdown. The CLI receives only Read/Write/Edit, a strict sandbox
profile with credential and authority-path deny rules, and explicit denials
for shell, MCP, web and agent tools. There is no always-approve or project
trust flag. Grok's child-network sandbox is Linux-specific; its sandbox and
native permission behavior need a version-pinned deployment smoke before
operational use. The fake tests verify invocation and event handling, not
vendor sandbox enforcement. Model communication uses its subscription path,
not the BYOK grant path.

Primary protocol references:

- [Codex app-server](https://developers.openai.com/codex/app-server)
  and [configuration reference](https://developers.openai.com/codex/config-reference/).
- [OpenCode v2 API](https://dev.opencode.ai/v2/docs/api) and
  [configuration isolation flags](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/plugin/skill/customize-opencode.md).
- [Grok CLI](https://docs.x.ai/build/cli/reference),
  [permissions](https://docs.x.ai/build/features/permissions), and
  [sandbox](https://docs.x.ai/build/features/sandbox).

The repository's Codex and OpenCode research notes retain observed vendor
version corrections. These ports use those corrections and fail closed on
missing safety capabilities; fake smoke success alone does not qualify a
new vendor release.
