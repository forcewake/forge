# Dogfooding: forge is developed through forge

This repository runs its own factory loop. A feature goes: **issue →
`/implement` → plan → human `/go` → coding agent in Actions → candidate →
trusted publisher → Draft PR → review → merge (human)** — the exact flow
forge sells, executed on this repo ([ADR-0020](../adr/0020-github-actions-executor.md)).

## The loop in practice

1. **Open an issue** describing the change (see
   [Writing the task](../harnesses/onboarding.md#writing-the-task-what-the-agent-actually-reads)
   — the same brief quality rules apply to forge's own issues).
2. Comment **`/implement`** (or assign the **`forge`** label). The GitHub
   App webhook starts a durable run; forge posts the plan comment with the
   **Implementation block** (selected harness, model, fallbacks, budget
   class).
3. **Reply `/go <run-id>`** as an approver. forge dispatches the
   [forge-harness workflow](../../.github/workflows/forge-harness.yml)
   (dispatch-only, pinned to `main` — this repo IS the product), the agent
   works in an ephemeral runner with no write credential, and the trusted
   publisher opens a **Draft PR**.
4. **Review the PR.** forge's own reactive reviewer comments on further
   pushes; CI (`ci.yml`) must be green; the merge button stays human.
5. **ADR → spec first** for anything architectural: write the ADR under
   `docs/adr/` (next free number) and a stage brief under `docs/specs/`
   as part of the issue — the planner carries them into the brief.

## What is wired on this repo

| Piece | Value |
|---|---|
| Trigger | `/implement` comment or the `forge` label (ADR-0020 §4 — no partner-program assignee) |
| App | `forcewake-forge` (installation-scoped; bot identity `forcewake-forge[bot]`) |
| Lane | `.github/workflows/forge-harness.yml` (dispatch-only, proposal-only, `PINNED_REF = main`) |
| Harness | claude-code via the z.ai gateway; MCP servers: Context7 + Microsoft Learn (`FORGE_HARNESS_MCP`) |
| Reactive review | `pull_request` opened/synchronize → inline review threads |
| Debug lane | failed Actions jobs on PR heads → durable debug comment |
| Security | code-scanning/Dependabot alerts → `/security` triage (when enabled) |

## Ground rules while dogfooding

- **Never weaken the verification** to make a run pass: the review, CI and
  the full test suite gate merges, not the harness's confidence.
- **The lane cannot push** — every candidate arrives as a Draft PR built by
  the trusted publisher; direct pushes to `main` by humans remain allowed
  (the dogfood loop is additive, not mandatory, until the team decides
  otherwise).
- **Failures are data**: a blocked run with `harness_*` classification is a
  real bug report against forge — fix the product, not the symptom.
