# ADR-0020: GitHub Actions execution adapter and agent UX

Status: accepted (2026-09-14)
Context: closes Stage E3 — the GitHub slice had no runner (builtin ran
inside the forge worker) and no human gate. Implements the execution side
of [ADR-0019](0019-source-execution-adapters.md); findings F04/F20/F21
parity with the GitLab lane.

## Decision

1. **GitHub Actions is the second execution adapter** with the standard
   contract: `launch / poll / cancel / reconcile_launch` over a typed
   handle (provider, repo, workflow, run_id, attempt, run_spec_digest).
   Launch primitive: `workflow_dispatch` on a forge-controlled workflow
   file in the target repo (`ci/templates/forge-harness.github.yml`,
   human-applied at onboarding). Reusable workflows were rejected for v1:
   cross-repo calls entangle permission models; `repository_dispatch` lacks
   first-class inputs and run correlation. Correlation: the dispatch
   response is empty (204), so forge discovers the run by
   `event=workflow_dispatch` + `head_branch` + created-window and verifies
   `head_sha == attempt_base_oid` — never trusts ordering alone.
2. **Proposal-only lane on Actions.** The workflow checks out the frozen
   attempt base detached with `persist-credentials: false`, push is
   disabled, and the agent's only output is the candidate artifact
   (`.forge/candidate.diff` + meta) via Actions artifacts v4. Forge
   downloads it with the publisher token and publishes through the same
   trusted publisher as GitLab (grant → policy → CAS write). No write
   token, no forge secret in the lane; harness provider keys are
   repo-scoped Actions secrets owned by the repo owner.
3. **Human gate on GitHub = same machinery as GitLab.** `/implement` →
   durable FlowRun (provider `github`) → plan comment on the issue →
   pending decision (RunSpec digest, TTL) → `/go <run-id>` consumes it →
   Actions harness → candidate → publisher → Draft PR → checks → readonly
   review → `ready_for_human`. One active run per (repo, issue) via the
   existing partial unique index; `/cancel` = revoke-then-stop.
4. **Agent UX is honest.** The "Assign to Agent" picker is a curated
   GitHub directory (Copilot, Claude, Codex partners) — a third-party App
   does not appear there by registering. Supported triggers for forge:
   issue comment commands (live) and an `issues.labeled` label trigger
   (e.g. `forge`) — normalized into the same durable command path.
   Marketplace/partnership listing is a product step, not a code step.

## Consequences

- The builtin fallback inside the forge worker stays for provider-less
  environments, but every repo WITH Actions runs harnesses in the lane —
  same guarantees as the GitLab path (F04 parity).
- Actions minutes are consumed from the target repo's quota — documented
  at onboarding; the harness workflow carries a 60-minute timeout and a
  run-level durable deadline.
- Workflow file is versioned in the target repo (human-applied); forge
  records its digest in the RunSpec — a changed workflow invalidates
  approval like any other execution-profile change.
