# Interactive-driver live smokes — 2026-09-21

First live verification of the REAL interactive-driver clients
(`src/forge/adaptive/drivers/`, shipped v0.21.0) against actual vendor
binaries on the lab machine. Evidence JSONs in this directory are the
inputs to the `DriverMatrix` live seed
(`forge.adaptive.drivers.live_registrations`).

## What ran

`scripts/driver_live_smoke.py --driver {claude,codex,opencode}` — one
tiny deterministic task per surface, hard per-step budgets, every step
recorded with outcome + timing (failures included — they are evidence).

| Driver | Backend | Steps | Evidence |
|---|---|---|---|
| claude-sdk | claude CLI 2.1.273 + claude-agent-sdk 0.2.157 over the z.ai Anthropic gateway (BYOK env token) | start → PONG turn → BONG steering turn → interrupt of a long turn | `claude-live.json` — **4/4 ok** |
| codex-app | `codex app-server` (codex-cli 0.153.4) over stdio JSON-RPC, ChatGPT-plan auth | thread → turn completed → mid-flight steer (`expectedTurnId`) → interrupt → `turn/completed(interrupted)` | `codex-live.json` — **5/5 ok** |
| opencode-server | `opencode serve` v2.0.10, lane-local spawn (OpenCodeServer), zai-coding-plan provider, glm-5-turbo | spawn+ready → session+PONG → prompt(BONG)+events → abort | `opencode-live.json` |

## Real-usage mode (`--e2e`, added after the first pass)

PONG proves the wire, not the lane. The `--e2e` mode gives each driver
a fresh scratch repo holding a FAILING test (`calc.py` stub +
`test_calc.py`) and the task *implement `add` so the tests pass, run
pytest to verify* — the agent must read, edit, and execute; **the
judge is pytest run by the smoke itself, never the agent's reply**:

| Driver | Agent turn | Repo tests after | Evidence |
|---|---|---|---|
| claude-sdk | completed (25s, `terminal_reason=completed`) | green | `claude-e2e.json` |
| codex-app | completed (19s, `turn/completed(completed)`) | green | `codex-e2e.json` |
| opencode-server | completed (13s, `session.execution.succeeded`) | green — after the two driver fixes below | `opencode-e2e.json` |

Re-run (this machine, with the vendor CLIs authed):

```bash
uv run --extra interactive python scripts/driver_live_smoke.py \
  --driver claude --out docs/evaluation/2026-09-21-drivers/claude-live.json
```

## Live-found defects (all fixed same day)

The fakes had encoded the RESEARCH doc faithfully; only the live
servers could correct it. This is the argument for live smokes over
contract tests alone.

1. **codex sandbox spelling is asymmetric** (fixed in
   `codex_app.py`): `thread/start`'s `sandbox` string is kebab-case
   (`workspace-write`), while `turn/start`'s `sandboxPolicy.type` is
   camelCase (`workspaceWrite`). Both spellings are accepted as
   operator INPUT and normalized per-surface on emit.
2. **codex `turn/start` responds at turn ACCEPTANCE** (~0.4 s), not at
   completion — completion is the `turn/completed` notification. The
   client's event-driven design was already correct; recorded here as
   the live answer to the research doc's ambiguity.
3. **opencode v2.0.10 is a different API than the research documented**
   (rewritten): every route moved under `/api`, `prompt_async` is gone
   (`POST /api/session/{id}/prompt` is non-blocking, completion via
   SSE `session.execution.succeeded|failed`), model selection is
   MANDATORY (`POST /api/session/{id}/model {"model": {providerID,
   id}}` — a fresh session may resolve a default pointing at a stale
   provider), the event vocabulary is flat `type`+`data` (no
   `session.idle`), abort became `interrupt` (`{"interrupted": bool}`
   — true only when a turn is in flight), and the spec lives at
   `/openapi.json`. Recorded in the research doc's LIVE CORRECTION
   section.
4. **opencode v2.0.10 always enforces a server password** — unset, it
   GENERATES one and prints it to stdout, which a DEVNULL lane loses.
   The spawner now generates and pins one itself
   (`OpenCodeServer.password`).
5. **`opencode_client_from_env` positional-dict trap** (fixed): an env
   dict passed positionally landed in `provider_key`, the factory
   silently read the ambient environment, and every request dialed the
   DEFAULT port 4096. Now a `TypeError` with the fix.
6. **Subprocess env must MERGE, not replace** (fixed in the spawner):
   passing only the password env dropped `PATH` and the binary lookup
   died (`FileNotFoundError` on macOS).

### Found by the e2e pass (the wire smokes could not see these)

7. **The SSE reader was being killed by its own wait machinery**
   (fixed in `opencode.py`): `_ensure_subscription` awaited the reader
   through a cancellable observer wrapper; cancelling the observer on
   the fast-path raced a `CancelledError` INTO the reader — the stream
   silently died after the first frame and every turn limped home on
   transcript reconciliation. The reader task now goes into
   `asyncio.wait` DIRECTLY (wait never cancels its arguments) and
   readiness is a bounded event wait.
8. **`finish: "tool-calls"` is not turn completion** (fixed in
   `opencode.py`): intermediate assistant messages of the agent loop
   carry non-terminal finish values; reconciliation that treated any
   finish as completion declared the turn done after the first tool
   round while the loop was still running. Only `stop`/`error` are
   terminal now (regression-pinned by a test).

## Honesty bounds

A green smoke means exactly: these steps ran against these binary
versions on 2026-09-21 and passed. It is NOT a full
implement-verify-merge lane cycle; when the adaptive lane pilot runs a
driver end-to-end, that evidence lands under its own evaluation
directory and the `DriverMatrix` rows gain their lane-cycle column.
