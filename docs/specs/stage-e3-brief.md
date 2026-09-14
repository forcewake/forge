# Stage E3 brief — GitHub path to parity: gate, Actions runner, multi-harness

Closes the gaps the user called out on issue #14: no plan, no runner, one
harness. Read first: ADR-0016/0017/0018/0019, docs/research/github-api.md,
docs/specs/contracts-v0.2.md, existing bridge src/forge/integrations/github_flow.py.

## E3a — plan + human gate on GitHub (parity with GitLab path)

1. **Plan phase**: an `/implement` on a GitHub issue → durable step →
   builtin planner (as today) → post the PLAN as an issue comment
   (same markdown shape as GitLab: plan summary, digest, "Approve this
   exact plan by commenting `@forge /go <run-id>`"), create the pending
   decision (reuse the gate machinery: plan/base/spec/task digests +
   FORGE_DECISION_TTL) and freeze the RunSpec. Run parks in
   `waiting_approval` — add a GitHub-compatible status surface (FlowRun
   already carries it; the bridge must record run state in Postgres like
   the GitLab path — move the bridge onto FlowRun rows instead of the
   ephemeral deterministic id).
2. **Gate phase**: `/go <run-id>` on the same issue → durable step →
   validate + consume the decision (approver = FORGE_APPROVERS GitHub
   logins) → publish phase: CAS commit the candidate via the existing
   GitHubPublishFlow → Draft PR → evidence comment (plan digest, candidate
   SHA, PR link).
3. `/cancel <run-id>` mirrors GitLab semantics (revoke grant, cancel steps,
   superseded evidence).

## E3b — GitHub Actions executor + multi-harness (F04 parity on GitHub)

1. **Execution adapter** src/forge/execution/github_actions.py:
   `launch(handle) → POST /repos/{o}/{r}/actions/workflows/{wf}/dispatches`
   (inputs: run_id, attempt_base_oid, driver, model; ref = factory branch),
   `poll` via workflow-runs-by-head_sha + jobs + artifact listing,
   `cancel` via the cancel endpoint. Typed handle per contracts spec.
2. **Workflow template** ci/templates/forge-harness.github.yml (human-applied
   to the target repo, mirroring the GitLab onboarding): `workflow_dispatch`
   with those inputs, runs on `ubuntu-latest` in a container; steps mirror
   the GitLab proposal-only lane: checkout detached at attempt_base,
   push disabled, install the chosen CLI (per driver, same hardened
   preamble), run it, emit `.forge/candidate.diff` + `.forge/candidate.meta.json`,
   upload as artifacts. No write credentials anywhere in the lane.
3. **Multi-harness**: driver script builders from src/forge/harnesses/
   (Stage D) render the per-driver step for Actions; claude-code, grok-build,
   opencode parity with the GitLab templates. Backend selection comes from
   the RunSpec.
4. **Publisher unchanged**: forge downloads the candidate artifacts (bot
   token), validates (grant/policy/base), publishes via
   `create_commit_on_branch` CAS, updates the Draft PR.
5. Run state: waiting_harness semantics over Actions (reuse the durable
   step + reconciler pattern; deadline = FORGE_HARNESS_TIMEOUT_SECONDS).

## Acceptance

- GitHub issue /implement → plan comment + pending decision; /go →
  Actions run visible in the repo (harness in the runner, no write token in
  logs) → candidate artifact → Draft PR → CI green → evidence.
- Claude/grok/opencode each parseable through the same driver contract
  (fixture-level at minimum, one live-verified if credentials allow).
- Kill/restart at any point converges (durable steps, same FI suite
  semantics).
- All existing tests stay green; new tests for adapter, ingress routing of
  /go /cancel, workflow template contract, publisher unchanged paths.
