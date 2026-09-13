# 0015. Pluggable implementer backends; coding harnesses execute in project CI

- Status: Accepted
- Date: 2026-09-12
- Context: M2 — real implementer

## Context

The built-in implementer is a single-shot LLM call that returns a structured
ChangeSet ([ADR-0001](0001-commits-api-write-backend-changeset-contract.md)).
Established coding harnesses — Claude Code, GitHub Copilot CLI, opencode and
similar — are materially stronger executors: they bring their own tool loops,
run tests in a real workspace, manage context, and route to strong models.
Forge should be able to delegate implementation to them instead of competing.

[ADR-0002](0002-ci-execution-environment-explicit-execution-profiles.md),
however, fixes a boundary: user-directed code executes only inside GitLab CI,
under an execution profile approved at onboarding. Running a harness on the
forge host or worker would put an autonomous agent that executes
model-generated commands on forge's own infrastructure — a violation of that
boundary and of the no-secrets posture.

## Decision

1. **The implementer step becomes a pluggable backend interface.** The
   controller owns the lifecycle unchanged (ADR-0004); only the
   proposing/committing implementation is pluggable:

   ```text
   ImplementerBackend:
     start(run, task_brief) -> handle        # durable handle, journaled
     poll(handle) -> change_ready(sha) | failed(kind) | running
   ```

   Built-in backend (`builtin`): today's LLM → ChangeSet → Commits API path
   ([ADR-0001] remains valid for it). Harness backend (`ci_harness:<name>`):
   delegates to a coding harness.

2. **Harnesses execute as jobs in the target project's CI** (or a dedicated
   agents pipeline targeting the factory branch) — never on forge
   infrastructure. The job runs inside the execution profile approved at
   onboarding: isolated ephemeral container, project-scoped or harness-only
   credentials, no production secrets, no Docker socket, no privileged mode.

3. **Result contract.** The harness job:
   - checks out the factory branch `factory/<issue-iid>/<run-id>`;
   - runs the harness headless with the task brief (issue + approved plan,
     passed as pipeline variables / ephemeral files);
   - commits and pushes to the factory branch;
   - prints a machine-readable last line:
     `{"head": "<sha>", "summary": "..."}`.
   Forge adopts the result **only after verification**: the branch head
   advanced, the reported SHA matches the actual branch head, and the commit
   parent chain is intact. The verified SHA becomes the candidate and enters
   the same pipeline → quality contract → readonly review → evidence flow.
   Forge never trusts the harness's own success claim.

4. **Credentials stay in GitLab.** Harness API keys (Anthropic, GitHub,
   OpenCode provider keys, …) are project CI variables configured at
   onboarding (masked/protected per policy). Forge stores none of them.

5. **Onboarding adds one static include.** The project's `.gitlab-ci.yml`
   gains a one-time, human-applied `include:` of a forge-controlled CI
   template (versioned in the admin config repository per
   [ADR-0011](0011-config-never-delegates-security-downward.md)); forge then
   triggers runs via the pipeline API with run variables
   (`FORGE_RUN_ID`, `FORGE_ISSUE_IID`, `FORGE_PLAN_DIGEST`, budgets, timeout).
   The denylist on agent edits of CI files stays — the harness never modifies
   CI configuration.

6. **Budgets and limits** (ADR-0013): wall-clock timeout is enforced by
   GitLab `maximum_timeout` on the job; run-level token/cost/time budgets are
   enforced by the controller regardless of what the harness reports.

7. **Backend selection** is per-project configuration
   (`.forge.yml`: `backend: builtin | ci_harness:claude-code | …`), tighten-
   only under [ADR-0011]. The readonly reviewer, quality contract, human
   gates, evidence and audit are backend-independent and stay in forge.

## Consequences

- **Positive:** best-in-class coding agents with real workspace tool loops;
  forge stays a thin, auditable control plane; secrets remain in GitLab CI;
  the execution boundary of [ADR-0002] is preserved; multiple harnesses can
  coexist and be compared per project.
- **Negative:** heavier onboarding (include line, runner image with the
  harness, CI variables); harness output is less structured than a ChangeSet,
  so verification (SHA check, CI, review) becomes the trust boundary and must
  never be skipped; failure classification must also cover harness-level
  failures (auth, quota, timeout) as `infrastructure`, not `code`.
- The ChangeSet contract of [ADR-0001] remains the built-in backend's LLM
  contract and the audit representation for materialized files; harness
  commits bypass materialization by construction (they are already commits).
