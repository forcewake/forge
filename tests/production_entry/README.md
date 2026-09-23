# The production-entry invariant suite (`production_entry`)

Issue **#246 / Q35-09** (external review `c7ae8db`): the fast suite is
large and green, but a helper-level test can manually follow a pointer
the real collector never reads — exactly how Q35-01 survived every
test. This package is the thin, mandatory layer that drives the SAME
entry points a customer invokes. It does not replace any unit test.

Marker: `production_entry` (registered in `pyproject.toml`; run with
`uv run pytest -m production_entry`). Target CI budget: well under
90 s without PostgreSQL (the current suite runs in ~10 s).

## The layer's discipline

| Collaborator | What runs for real |
| --- | --- |
| Provider writes | the REAL `forge.integrations.github.GitHubClient` over real HTTP against `fake_native_server.py` — a separate PROCESS whose state (accepted jobs, dispatch ledger with INPUTS, run statuses) survives worker death by construction |
| The lane | `python -m forge.lane_driver --driver codex` as a REAL subprocess; the vendor is the controlled `fake_vendor.py` executable speaking the REAL codex app-server JSON-RPC wire, editing REAL files in its working directory (the restored workspace generation) |
| The collector | the SHIPPED `python -m forge.harness_entry --collect-candidate` subprocess, exactly as the GitHub template invokes it |
| Checkpoints | the REAL `capture_wip` / `restore_wip(promote="generation")` code, uploaded/downloaded over real HTTP through the lane-control + checkpoint-channel routers (`forge.api_lane_control`, `forge.api_checkpoint_channel` — the same ASGI routers `create_app` mounts) served by uvicorn on a loopback port |
| Durable state | real aiosqlite FILE databases (a "restarted worker" is a genuinely fresh engine/session factory over the same rows); real PostgreSQL when `FORGE_PG_TEST_URL` is set |
| Control plane | real service instances (`GitHubRunService`, `OperatorControlService` over `PostgresMailbox`) — the same `make_service` composition the unit suites use, but with every provider I/O over the wire |

Assertions are on artifacts — file bytes, diff digests, the native
server's dispatch ledger, DB row identities — never on status strings
alone. No warning is suppressed globally; fixtures clean up their
processes, engines and ports.

## Trace map (review AT-01..AT-06 → PE-1..PE-6)

| Trace | Test | What it pins |
| --- | --- | --- |
| AT-01 → **PE-1** | `TestPE1RestoredWorkBecomesTheShippedCandidate` | Real checkout → real capture+upload over HTTP → real lane subprocess restoring into a generation with the fake vendor doing the resumed turn → the SHIPPED collector subprocess captures the generation's edits (changed/new/deleted) with the checkout untouched. Negative arm: the OLD inline emit sequence produces a 0-byte diff beside the work. |
| AT-02 → **PE-2** | `TestPE2RetryBeforeAnyUsefulWork` | A bootstrap death before the vendor started (no candidate, no checkpoint) + the authenticated `/retry` through the real service: the NATIVE SERVER records exactly one new dispatch whose inputs carry `lane_resume_mode=fresh` — the committed baseline, no checkpoint prerequisite. |
| AT-03 → **PE-3** | `TestPE3RequiredResumeNeverBecomesASilentRestart` | A rotted referenced blob on the control plane: the required-continuation lane subprocess halts `wip_restore_failed` with ZERO vendor events (the fake vendor executable is never spawned), zero publication, no generation, checkout clean. |
| AT-04 → **PE-4** (PG-gated) | `TestPE4PostgresUploadRestartResume` | Postgres-mode upload over real HTTP, control instance torn down, a NEW instance with a fresh session factory: `/resume` produces the exact ResumeSpec from PostgreSQL (row identity + the public `/lane/controls/resume-spec` HTTP surface), no filesystem JSON mirror. Negative arms: the filesystem authority alone answers nothing (the old producer would have refused), and a STALE filesystem index never wins. |
| AT-05 → **PE-5** | `TestPE5NativeJobOutlivesLocalCancellation` | Cancel configured to fail on the native server: `/cancel` lands locally while the job keeps running — the slot stays HELD (draining) at the cap, the next start is parked with NO extra native start; the server marking the job terminal lets the reconciler probe release it, then exactly one more start. Negative arm: releasing on the local verdict (the audited force-override, i.e. the pre-Q35-04 behavior) OVERSUBSCRIBES — two native jobs running at capacity one. |
| AT-06 → **PE-6** | `TestPE6LostStartResponsePreservesUncertainOccupancy` | The server accepts a dispatch but the response is made to fail; the worker dies before the handle update. A RESTARTED worker (fresh session factory, same DB) cannot free the slot: the new start parks, occupancy stays uncertain (intent recorded, handle NULL, draining); the reconciler probe resolves the correlation through the server's surviving state — held while the job runs, released once terminal — then exactly one new start. |

## The fakes

- **`fake_native_server.py`** — stdlib HTTP server run as a subprocess
  (`--ready-file` carries the chosen port). Models the GitHub
  Actions/REST surface the production client actually touches
  (dispatches with recorded INPUTS, runs list + run status for the
  correlation/probe primitives, cancel, refs, contents, issues and
  comments, pulls). Failure injectors: `dispatch_response=
  server_error` (AT-06's lost start response — the job is minted, only
  the answer fails) and `cancel_mode=fail` (AT-05). Control surface
  under `/__ctl/*`; every unknown path is recorded and served a loud
  404, and the traces assert the ledger stayed empty (the real client
  never fell off the modeled API).
- **`fake_vendor.py`** — the controlled vendor. As `app-server` it
  speaks the codex App Server JSONL wire the real lane drives; on
  `turn/start` it applies REAL file edits (from `FAKE_VENDOR_ACTIONS`)
  in its cwd and answers the lifecycle notifications. As `--once` it
  applies the edits immediately (the pre-pause WIP leg). Every
  vendor-visible event lands in `FAKE_VENDOR_EVENTLOG` — the trace's
  "zero vendor calls" is literally "the log has no events".

## Honesty notes (where the trace simplifies, and why)

- **PE-2's death record.** The bootstrap death is written into the
  run row as the worker's terminal journal would (`harness_
  infrastructure: harness_bootstrap_failed (...)` — the recorded
  classification `evidence_from_record` reads). Driving the full
  reconciler classification (lane meta upload → evaluation) is the
  worker suite's job; this trace pins the /retry decision and the
  dispatch artifacts.
- **PE-4 is PG-gated** and skips cleanly without `FORGE_PG_TEST_URL`
  (the FI convention: the URL's database is disposable; the test
  deletes its rows per run). Everything else runs on the fast profile.
- **PE-5's negative arm** uses `release_run_leases(..., force=True)` —
  the audited override — as the faithful spelling of the OLD
  "release on local status" behavior; the override exists in
  production precisely so this mutant can be demonstrated.
