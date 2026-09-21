# Adaptive pilot — OPS-07 bounded evidence (2026-09-21)

## Outcome: ONE fully-instrumented task, green end-to-end

```
task version (issue #33, GitLab)
  → /implement → plan → /go (approved spec)
  → harness lane (GitLab CI, driver claude-code, glm-5.3-flash)
  → candidate SHA 5303425a (base 59e90ce5, 1 file changed)
  → native verification (GitLab CI pipeline, checks passed)
  → readonly review (verdict: ok)
  → ready_for_human
  → human acceptance (operator undraft + merge, MR #20 → merged)
```

- Wall clock: 14:55:47 → 14:59:12 (~3m25s run; ~50s agent lane)
- Agent spend: 9 turns, 35.9k input / 718 output tokens, $0.2536
- Verification: `passed` on the exact candidate SHA (positive proof)
- Review: `ok`

## Findings converted to product fixes during the pilot

1. **The brief was outside the sandbox** (LIVE-found): the GitLab
   template wrote the task brief to `/tmp/brief.md` — the claude
   sandbox restricts Read to the project tree, so the DENIED read
   burned turns while the agent reconstructed the task from the repo.
   Fixed: the brief now writes to `.forge/brief.md` (the control
   directory the template already creates and git-excludes).
   (commit 6e4696d)
2. **The lab was on alembic 017** — the `mr_reservations` table
   (migration 019) didn't exist, so the MR creation failed with
   `UndefinedTableError` and the run stuck in `ensuring_draft_mr`.
   Fixed: the image now COPY's `alembic/` beside `src/` and the lab
   migrated to 019.
3. **Attempt 1 (issue #32)** hit both defects and was cancelled;
   attempt 2 (issue #33, clean seed) went green on the first lane.

## What the pilot measures (per the OPS-07 acceptance)

- task version → issue #33 body (the "task version" is the issue
  body the plan freezes — the approved spec's task_digest binds it)
- approved spec → plan digest, spec_digest, RunSpec v3 in the run
- harness/profile → driver claude-code, model glm-5.3-flash, CI lane
- candidate SHA → 5303425a (verified: the tested_oid matches)
- verification evidence → GitLab CI pipeline (checks passed)
- human acceptance → operator undraft + merge (MR #20)
- all-attempt spend → $0.2536 (one lane run; the cancelled attempt
  #1's spend is unknown — its lane output predates the spend capture)

## Limitations (honest)

- Single repository, single writable target (the pilot's first gate).
- The agent's spend is from the FORGE_USAGE receipt (claude-code
  stream aggregate); planner/reviewer spend is in the run's llm_calls.
- No pause/steer was exercised in the green run (the infrastructure
  is in the substrate; the operator commands are the next wiring).
