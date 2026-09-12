# 0012 — Context and redaction checked at every boundary

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

Everything forge sends to a model is potentially hostile or sensitive at the
same time. Repository text, issue descriptions, CI logs, and tool results can
carry prompt injection ("ignore instructions, run this tool, commit this
secret"); the same channels can carry secrets that must not leave the
installation. Three specific weaknesses must be designed out:

- A tool that returns file content directly (decoding whatever is at `HEAD`)
  bypasses the shared redaction and token-budget path.
- Context assembly that transmits only an issue title and trigger note, but
  not the full description and acceptance criteria, starves the model of the
  evidence it needs — pushing it toward guessing.
- If redaction replaces secret spans with placeholders but those placeholders
  can end up in a commit, the redaction layer becomes a corruption source
  instead of a protection.

A locally hosted model reduces external data egress but does not remove
prompt injection or the risk of harmful CI code — so the boundary stays even
when the egress does not.

## Decision

Policy, redaction, and budgets are enforced **at every boundary**, uniformly:

- **All file reads are bound to a SHA.** Read tools address a pinned snapshot
  from [ADR-0006](0006-snapshot-isolation-and-race-protection.md), never a
  moving ref.
- **Every source passes the same outbound layer** — file contents, tool
  results, logs, issue descriptions, discussion threads — before anything
  reaches the model. There is no "trusted" shortcut path for tool output.
- **Repo text is data, never authorization.** No string in repository
  content, logs, or comments can enable a tool, change scope, or grant
  permissions. Read tools are bounded by project, snapshot, path, size, and
  request count.
- **Redacted files are excluded from automatic full-file editing.** When a
  file contains redacted spans (e.g. detected secrets), it is not eligible
  for wholesale regeneration, so placeholders can never be committed in
  place of real content; such files require a special, verified handling
  path or human involvement.
- **No dependency or lockfile changes in the base profile.** Manifest edits
  are not enabled while the required lockfile updates are forbidden — the
  agent is not set up to fail. A later dependency profile would update
  lockfiles deterministically in CI and carry a separate approval.

## Consequences

- **Positive:** a single, auditable choke point for everything the model
  sees; injected instructions in logs or tool results remain inert data;
  secrets found by redaction cannot be re-emitted into a commit through the
  full-file-edit path; the model gets full issue evidence instead of titles
  alone.
- **Negative:** the uniform boundary adds latency and machinery to every
  read; files with detected secrets cannot be auto-edited wholesale, which
  stops some otherwise straightforward tasks; dependency updates are out of
  reach for the base profile by design.
- Injection that gets past context filtering still cannot merge
  ([ADR-0003](0003-no-merge-is-enforceable.md)) or widen its own permissions
  ([ADR-0011](0011-config-never-delegates-security-downward.md)); the
  execution risk of anything it does write is contained by the profiles in
  [ADR-0002](0002-ci-execution-environment-explicit-execution-profiles.md).
