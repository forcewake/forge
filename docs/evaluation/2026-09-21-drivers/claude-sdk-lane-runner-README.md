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

## GitHub-side full cycle (2026-09-22, run 240b9129 — PR #85)

issue #84 → /implement → LIVE DISCOVERY (plan cites "evidence ev-4/ev-5",
farewell() found at line 5) → /go <8-char prefix> → GitHub Actions
claude-sdk-lane job (npm claude-code@2.1.273 + pip claude-agent-sdk +
python -m forge.lane_driver --driver claude) → candidate → Draft PR #85.
Live-found en route (all fixed): #154 eternal-preflight (broad handler +
honest /go-while-preflight reply); GitHub /go prefix resolution; the
claude-sdk-lane harness_entry arm + its SDK install (the bootstrap's
forge install carries no extras).

## Adaptive control LIVE (2026-09-22, run 0f586216 — issue #86)

The operator-command half of the control chain verified live: `/steer
<run-id> <guidance>` posted as a GitHub issue comment → authenticated
ingress (app on FORGE_ADAPTIVE_COMMANDS_ENABLED=1) → ControlCommandRouter
(approver gate, run resolution) → **PostgresMailbox row (steer/received,
work-scoped)** — the durable control_commands table holds the command.
The lane-side REMOTE consumer (outbound lane-control API + in-job poller)
is the last assembly slice (agent dispatched). En-route live findings:
env-file line gluing strikes twice (HOME=...FORGE_ADAPTIVE — always
terminate env files with a newline before appending); the ingress flags
must ride the APP container, not only the worker.

## WAVE B — /steer end-to-end LIVE (2026-09-22, run 9982b655)

GitHub issue comment → authenticated ingress → ControlCommandRouter →
PostgresMailbox (steer/received) → the Actions lane job polls the
outbound /lane/controls API (work-scoped HMAC token computed by the
DISPATCH, SecretStr unmasked) → LaneControlChannel feeds the real
LaneSteeringSession drain → vendor steer delivered to the RUNNING
Claude agent → ack ladder all the way home in Postgres:

received → authorized → dispatching → vendor_accepted → applied → checkpointed

The candidate meta carries steering_journal (steer → applied) +
episode timings (turn 211s). Live-found en route: SecretStr str() is
the MASK not the value (get_secret_value); FORGE_RUN_ID needed on the
DRIVER step too; emit-meta rebuilt the v2 dict and dropped the journal
(sidecar pass-through now).

## WAVE C — /pause + verified checkpoint LIVE (2026-09-22, run 88385ca7)

/pause from the issue → the same inbound chain → the lane's steering
drain runs the REAL checkpoint transaction: interrupt acknowledged
(3ms) → cooperative capture (git tracked baseline + content-addressed
local store) → upload through /lane/checkpoints (the lane's work-
scoped token — the shared secret NEVER enters the lane job) →
pause_status=paused, receipt verified=true, remote_ref mounted.
Server-side: GET /lane/checkpoints/<work> → 200, 124 digest-verified
blobs stored. En-route live-found (each fixed): the upload channel
must be a WipUploadChannel PROTOCOL object (a bare function lacks
.upload_checkpoint); a work-scoped token must satisfy the channel's
config check (requiring BOTH it and the secret contradicts EXE-04).
