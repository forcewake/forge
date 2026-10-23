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

This is an unreleased source tree being simplified toward 1.0 under
[ADR-0035](docs/adr/0035-simplification-program.md). The server,
`forge-protocol` wire contracts and independent `forge-lane` runner now
share a uv workspace. Current provider services still use the existing
lane while the new HTTP lane and shared lifecycle are integrated. See the
[implementation record](docs/roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md)
for completed source changes and outstanding verification. Source subtraction
is local; global prose/dependency targets and assembled, hosted and live
verification remain open.

Claude Code and Copilot CLI are the first-class driver targets. Additional
drivers are experimental. Azure DevOps remains on the
[0.42.x maintenance line](docs/operations/maintenance-0.42.md).
The builtin implementer, security triage, standalone CI-debug lane,
server-side MCP and legacy chat are removed or deferred.

Local tests, historical traces, hosted CI, release publication and external
acceptance are separate claims. This README declares no 1.0 release or
current live acceptance. [Historical records](docs/archive.md) remain
recoverable from their documented local snapshots.

BSD-3-Clause: [license](LICENSE) and [upstream provenance](UPSTREAM.md).
