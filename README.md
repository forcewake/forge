# forge

Forge turns an authorized issue into a plan, bounded implementation in your
CI, and a Draft pull request or merge request. A human approves the exact
plan before execution and decides whether to merge. GitHub is the primary
provider; GitLab CE is the second.

## Safety model

The coding lane proposes changes without repository write credentials.
One trusted publisher validates the candidate, approved scope and current
head before writing. External effects are journaled before dispatch;
unknown outcomes are probed before retry. Approval is consumed once and
bound to digests. Cancellation revokes publication authority first.
Independent verification must name the current candidate SHA. Unknown
spend remains unknown. The bot never merges.

These rules are architectural contracts, enforced by the
[Invariant Suite](tests/invariants/registry.py) and
[decision records](docs/adr/0000-record-architecture-decisions.md).
They do not imply that this working tree has passed hosted or live checks.

## Start here

- [GitHub setup](docs/getting-started/github.md).
- [GitLab setup](docs/getting-started/gitlab.md).
- [Operator commands](docs/operations/operator-commands.md).
- [Installation and development](docs/onboarding/cold-install-runbook.md).
- [Documentation index](docs/README.md).

## Status

The local source candidate identifies as 1.0.0 under
[ADR-0035](docs/adr/0035-simplification-program.md). The installed server uses
one SQL engine and the HTTP execution lane. It has no Redis routing or old-runtime dependency. `forge` starts that
runtime directly; `forge-schema` provides explicit installation checks and
offline history export. Configuration is closed TOML with named environment
secrets. Start with [the installation guide](docs/onboarding/cold-install-runbook.md)
and [forge.example.toml](forge.example.toml).

The old runtime and migration chain are removed from `src/forge`. The
[retirement manifest](docs/archive/runtime-0.42.json) identifies their exact
recoverable source. There are no routing flags, old-runtime fallbacks or
compatibility API endpoints.

Claude Code and Copilot CLI are the supported drivers. Azure DevOps remains
on the [0.42.x maintenance line](docs/operations/maintenance-0.42.md).
The builtin implementer, security triage, standalone CI-debug lane,
server-side MCP and legacy chat are removed or deferred.

Local tests, historical traces, hosted CI, release publication and external
acceptance are separate claims. This README declares no 1.0 release or
current live acceptance. [Historical records](docs/archive.md) remain
recoverable from their documented local snapshots.

BSD-3-Clause: [license](LICENSE) and [upstream provenance](UPSTREAM.md).
