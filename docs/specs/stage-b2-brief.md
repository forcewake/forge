# Stage B2 brief — RunSpec, admission, cancel-as-revoke (F13–F16), verification profile (F19)

Handoff brief for the implementing agent. Read first: ADR-0017, ADR-0018,
docs/specs/contracts-v0.2.md. Builds on Stage B1 (durable step runtime in
worker/steps.py + transactional ingress).

## 1. RunSpec (F14)

- New table `run_specs` (alembic migration): id (uuid), run_id FK,
  schema_version, JSONB document, digest (sha256 over canonical JSON),
  created_at. One row per attempt chain; the gate stores the spec digest.
- Build the spec at plan acceptance (end of `start_run`, before the plan
  note): subject (project/connection), source_base_oid (run.base_sha),
  plan digest, policy digest — EXTEND `_policy_digest` from
  `FORGE_APPROVERS`-only to: approvers + target branch + required jobs +
  implementer backend + harness settings digest (canonical JSON of the
  effective values, not env names), config digest (`ForgeConfig` canonical
  dump), budgets (wallclock = FORGE_HARNESS_TIMEOUT_SECONDS + CI wait,
  commit cycles, max calls placeholder).
- The harness step reads its backend config from the spec's stored values,
  not live Settings: serialize the resolved backend config (backend name +
  FORGE_HARNESS_MODEL + template digest placeholder) into the spec at
  acceptance and pass it through the step payload. Live Settings may not
  silently change an approved run's execution.
- Gate `is_valid` gains `spec_digest` validation: a `/go` whose run's
  current spec digest differs from the digest recorded at plan publication
  is invalid (re-approval required).

## 2. Pending decision with deadline (F15)

- When the plan note is posted, create the GateApproval row immediately
  (status pending, generation 0, expires_at = now + FORGE_DECISION_TTL,
  default 7 days — new setting) carrying plan_digest + base_sha +
  task_digest (sha256 of issue title+description AT PLAN TIME) + spec
  digest. `/go` consumes THIS row (already-consumed → duplicate, already
  expired → invalid with reason).
- task_digest drift: if the issue body changed after plan publication,
  `/go` still runs the APPROVED task (spec frozen); the evidence comment
  gains a line noting the issue changed since approval.

## 3. Admission (F16)

- New `runs/admission.py`: `check_admission(settings, forge_config, project_id, actor) -> AdmissionDecision`
  — pure function + policy from settings: FORGE_APPROVERS may start runs
  (other actors get `denied` without any LLM/paid call — applied in
  `start_run` before the planner), project must not be in a denylist, bot
  username must not appear in approvers (startup check → refuse to start
  the app if violated). Applied in the start_run step handler BEFORE the
  planner; denial → run blocked `admission_denied: <reason>` + journal note.
- Onboarding gate: `FORGE_REQUIRE_ONBOARDING` (default false for the lab)
  — when true, `start_run` requires the project to have passed doctor-style
  checks recorded in a `project_onboarding` table (out of scope to
  populate; the check just reads it).

## 4. Cancel-as-revoke (F13)

- `handle_cancel_note` (existing): before the terminal transition, set
  `run.cancel_requested = True` (new column) + cancel scheduled steps
  (UPDATE step_runs SET status='cancelled' WHERE run_id AND status='scheduled').
- Publication-grant enforcement: `writer.apply()` gains a pre-flight
  callable `grant_check` (wired by RunService) that raises if the run is
  cancelled — so even a racing in-flight proposal cannot publish. Harness
  poll path: a verified candidate for a cancelled run is recorded as
  superseded (evidence only), run stays cancelled.
- Late results: `evaluate_waiting_harness`/`evaluate_waiting_ci` skip runs
  with cancel_requested (they are already terminal) — nothing to do beyond
  the guard.

## 5. Verification profile (F19)

- New `runs/verification.py`: `VerificationProfile` (required_jobs tuple,
  producer allowlist placeholder, freshness window seconds) built from
  settings; `evaluate(pipeline, jobs, profile)` replaces
  `evaluate_quality_contract`'s empty-required-jobs pass-through: with an
  EMPTY profile the run can reach review but the evidence comment and MR
  description gain `⚠️ no verification profile configured` — production
  profiles must be non-empty (enforced at admission when
  FORGE_REQUIRE_ONBOARDING).
- Freshness re-check before READY: after the review, re-read the branch
  head; if it moved from the candidate → blocked `candidate_drift` (the
  existing external_change check covers pre-review; this adds post-review).

## Acceptance

- Tests: spec created at plan acceptance + digest change invalidates /go;
  decision deadline expiry blocks; /implement from non-approver never calls
  the LLM (assert planner not called); cancel revokes scheduled steps and a
  late verified candidate stays superseded; empty verification profile
  labels evidence; post-review drift blocks.
- All existing tests green; style per repo; no commits.
