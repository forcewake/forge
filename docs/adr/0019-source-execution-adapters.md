# ADR-0019: Source/execution adapters; GitHub App vertical slice

Status: accepted (2026-09-13)
Context: review findings F27, F31, F32; decision to add GitHub **in this
repository** (modular monolith, one release train), after the safety and
durable-runtime phases.

## Decision

1. **Four orthogonal axes, four contracts** — never one "backend" concept:
   - **Source adapter** (GitLab CE, GitHub): work items, comments,
     repository reads, change requests (MR/PR), identities. Ports, not a
     500-method PlatformClient: WorkItems, RepositoryReader,
     ChangeRequests, VerificationReader, IdentityResolver,
     PublicationWriter. Identities are connection-scoped opaque IDs;
     provider-specific capabilities are declared, not flattened.
   - **Execution adapter** (GitLab CI, GitHub Actions, later isolated
     local/K8s): launch/poll/cancel/collect/reconcile-launch over a typed
     handle; declares capabilities (network policy, artifact integrity,
     cancellation granularity).
   - **Harness driver** (builtin, Claude, Codex, Grok Build, OpenCode):
     invocation, events, exit classification, usage receipt — inside the
     untrusted execution lane; never creates MR/PR, never sees approvals.
   - **Model route + credentials**: provider protocol, endpoint reference,
     capabilities, credential reference from the broker. BYOK is a
     credential-ownership mode, not a harness. Actual resolved model is
     recorded from trusted receipts or left `unknown`.
2. **Capability manifests, no lowest-common-denominator.** GitHub
   `createCommitOnBranch(expectedHeadOid)` is a branch-wide CAS; GitLab
   `last_commit_id` is file-level. GitHub PR CI may test a synthetic merge
   revision — verification keeps `subject_head_oid`, `target_base_oid`,
   `tested_oid` apart plus native run/attempt and check producer. Recipes
   are allowed only when required capabilities are met.
3. **GitHub first slice (beta)**: GitHub App installation identity (private
   key only in the credential broker), HMAC-validated ingress, same-repo
   `issue_comment` commands, builtin → Draft PR via trusted publisher →
   Actions verification → review. Fork/`pull_request_target` privileged
   flows are explicitly out of scope for the first beta.
   **Trigger ground truth (research 2026-09-13, docs/research/github-api.md):**
   App installation tokens DO trigger workflows and BYPASS the first-time-
   contributor approval prompt — the execution adapter must therefore pin
   the trigger model explicitly (dispatch vs push), never rely on GitHub's
   recursion/approval guardrails for authorization, and validate every
   trigger assumption with contract fixtures + a live canary.
4. **Repository shape**: one repo, `src/forge` module boundaries first;
   uv workspace packages only after two adapters/driver boundaries prove
   stable; incompatible vendor CLIs live in separate runner images, not in
   one control-plane environment. Plugin auto-install from `.forge.yml` is
   forbidden forever.

## Consequences

- Core stops importing GitLabClient/FastAPI/CLI SDKs; adapters implement
  ports against recorded contract fixtures (including pagination limits,
  raw-diff endpoint, incomplete evidence states).
- The GitLab transport is wrapped, not rewritten; legacy reactive agents
  stay behind a bridge until their flows are replaced.
