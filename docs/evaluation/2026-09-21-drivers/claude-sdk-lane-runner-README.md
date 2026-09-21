# claude-sdk-lane — the REAL runner cycle (2026-09-21)

The full production path, driver as implementer, nothing synthetic:

issue #34 (forge-lab) → `/implement` → plan `d0f54699...` → `/go` →
factory branch `factory/34/d0f54699` → pipeline 344 on the UNRAID
docker-executor runner → job `forge-agent` (claude-sdk-lane template:
npm claude CLI + uv python 3.13 + forge[interactive]@01eb90b) →
`python -m forge.lane_driver` drove ClaudeSDKDriverClient →
`lane_driver: claude-sdk-lane exit=completed reason=completed` →
candidate collected → CI verification PASSED → Draft MR !21.

MR diff: `farewell(name)` added, `greet()` untouched, tests written by
the agent. Run reached `ready_for_human`. Trace:
`claude-sdk-lane-runner-trace.log`.

Operational notes (found live): `/go` needs the FULL 32-char run id
(the plan shows the 8-char prefix — the no-op ignore is silent); a
run's attempt base freezes the repo's CI config at planning time (a
broken lab .gitlab-ci.yml inherited into the factory branch = pipeline
with zero jobs).
