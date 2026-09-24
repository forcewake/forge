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

## Trace map (review AT-01..AT-06 → PE-1..PE-6, AT-11 → PE-7, AT-10 → CE-1..CE-4)

| Trace | Test | What it pins |
| --- | --- | --- |
| AT-01 → **PE-1** | `TestPE1RestoredWorkBecomesTheShippedCandidate` | Real checkout → real capture+upload over HTTP → real lane subprocess restoring into a generation with the fake vendor doing the resumed turn → the SHIPPED collector subprocess captures the generation's edits (changed/new/deleted) with the checkout untouched. Negative arm: the OLD inline emit sequence produces a 0-byte diff beside the work. |
| AT-02 → **PE-2** | `TestPE2RetryBeforeAnyUsefulWork` | A bootstrap death before the vendor started (no candidate, no checkpoint) + the authenticated `/retry` through the real service: the NATIVE SERVER records exactly one new dispatch whose inputs carry `lane_resume_mode=fresh` — the committed baseline, no checkpoint prerequisite. |
| AT-03 → **PE-3** | `TestPE3RequiredResumeNeverBecomesASilentRestart` | A rotted referenced blob on the control plane: the required-continuation lane subprocess halts `wip_restore_failed` with ZERO vendor events (the fake vendor executable is never spawned), zero publication, no generation, checkout clean. |
| AT-04 → **PE-4** (PG-gated) | `TestPE4PostgresUploadRestartResume` | Postgres-mode upload over real HTTP, control instance torn down, a NEW instance with a fresh session factory: `/resume` produces the exact ResumeSpec from PostgreSQL (row identity + the public `/lane/controls/resume-spec` HTTP surface), no filesystem JSON mirror. Negative arms: the filesystem authority alone answers nothing (the old producer would have refused), and a STALE filesystem index never wins. |
| AT-05 → **PE-5** | `TestPE5NativeJobOutlivesLocalCancellation` | Cancel configured to fail on the native server: `/cancel` lands locally while the job keeps running — the slot stays HELD (draining) at the cap, the next start is parked with NO extra native start; the server marking the job terminal lets the reconciler probe release it, then exactly one more start. Negative arm: releasing on the local verdict (the audited force-override, i.e. the pre-Q35-04 behavior) OVERSUBSCRIBES — two native jobs running at capacity one. |
| AT-06 → **PE-6** | `TestPE6LostStartResponsePreservesUncertainOccupancy` | The server accepts a dispatch but the response is made to fail; the worker dies before the handle update. A RESTARTED worker (fresh session factory, same DB) cannot free the slot: the new start parks, occupancy stays uncertain (intent recorded, handle NULL, draining); the reconciler probe resolves the correlation through the server's surviving state — held while the job runs, released once terminal — then exactly one new start. |
| AT-11 → **PE-7** (`test_revision_executor_proof.py`, #272/R36-13) | `TestPE7ApproveRevisionReachesTheNextExecutorInput` + the stale-authority / question-redelivery / WIP-compatibility / separate-visibility arms | A native `/approve-revision` of revision 2 through the REAL command router (reply over real HTTP), then a worker restart, then the next ACTUAL dispatch: the server's recorded executor input carries revision 2's exact plan digest, and the server's own input FINGERPRINT equals the run's `revision.executor_digest` evidence (recomputed from the ledger). Arms: a late approval minted against the revision-1 world refuses `parent_mismatch` with zero effects; a delayed steer with `expected_plan_revision=1` EXPIRES undelivered; an authorized answer survives the restart as exactly ONE durable row (redelivery deduped at the real ingress, applied at most once); pause → real checkpoint upload → a COMPATIBLE revision keeps the resume dispatch carrying BOTH the new plan digest and the exact pinned checkpoint, while an INCOMPATIBLE one records `checkpoint.reuse_decision = fresh_attempt` and BLOCKS the required resume before any provider I/O; pause received / effect applied / checkpoint committed / resumed project as DISTINCT timeline categories over the `control_commands` rows — never a premature resumed-success. |

### The GitLab CE lane (AT-10 → CE-1..CE-4, `test_gitlab_ce_entry.py`, #268/R36-09)

The GitLab-shaped twin of the layer: the REAL
`forge.runs.service.RunService` + REAL `forge.gitlab.client.GitLabClient`
over real HTTP against the fake native server's **gitlab mode** (same
process discipline — pipelines/jobs/branches/MRs/notes survive worker
death), beside the cold-install qualification of the same profile
(`qualification/profiles/gitlab-ce-v1.md` +
`scripts/qualify_gitlab_ce.py`).

| Trace | Test | What it pins |
| --- | --- | --- |
| AT-10 → **CE-1** | `TestCE1NativeIssueToApprovedDispatch` | A native issue → `/implement` → the evidence-backed plan comment ON THE NATIVE SERVER (digest + `/go` gate + the frozen Implementation block) → the approver's `/go`: ONE native pipeline on the factory branch whose recorded FORGE_* variables carry the frozen RunSpec (`FORGE_ATTEMPT_BASE`, `FORGE_HARNESS_DRIVER=claude-code`, model route), its `forge-agent` job minted running, the durable handle naming the native pipeline/job ids, and the real client never leaving the modeled API (`unknown_paths == []`). |
| AT-10 → **CE-2** | `TestCE2RunnerLossResumesExactWipIntoDraftMR` (shared `ce_lab` fixture) | The controlled runner loss: the first runner's REAL lane subprocess pauses with a committed checkpoint (capture+upload over HTTP), the CI JOB is cancelled on the native server, a RESTARTED worker (fresh session factory, same rows) classifies the loss `blocked` with ZERO forge-side model calls (no LLM repair), `/retry` — admitted BECAUSE the durable checkpoint exists — re-dispatches on a second runner, the resumed lane (REAL subprocess, required restore) rebuilds the exact generation, the SHIPPED collector captures it, the artifacts upload natively, the reconciler downloads over HTTP and publishes ONE native commit + the Draft MR carrying exactly the generation's changed/new/deleted files. Verification: a green pipeline on the STALE base sha never verifies; the CURRENT candidate's pipeline does (`tested_oid` == candidate, MR stays `Draft:`). |
| AT-10 → **CE-3** | `TestCE3OldAttemptCallbackCannotPublishAfterResume` | The first runner's job returns LATE claiming success with a well-formed candidate of its own: the stale attempt's journaled handle, polled by the dead worker's resurrected pass, CANNOT publish after the resume — the run stays `ready_for_human`, the candidate is recorded `superseded`, and the native surface shows zero new writes (no second commit, no MR change, foreign content nowhere). |
| AT-10 → **CE-4** | `TestCE4RequiredRestorationFailureStartsNoModelTurn` | A rotted checkpoint blob at the authority: the second runner's REQUIRED restore fails loudly (`wip_restore_failed`) with ZERO vendor events after the pause leg (no model turn), zero publication, no generation, and nothing ever dispatched to the native surface. |

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
  **GitLab mode** (`--api gitlab`, #268/R36-09): the same process
  discipline against the GitLab CE REST v4 surface the REAL
  `GitLabClient` builds — pipelines (+ the dispatch ledger with the
  FORGE_* VARIABLES), pipeline jobs, job trace/artifacts, job cancel
  (the runner-loss primitive), branches/commits/files, issues/notes,
  merge requests. Per-segment unquoting keeps `%2F`-encoded branch and
  artifact paths ONE path segment, exactly like CE. Control surface:
  `seed_issue`, `seed_file`, `seed_commit`, `seed_pipeline` (independent
  verification pipelines), `seed_artifact`, `mark_terminal`,
  `cancel_job`.
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
- **CE-2's resumed runner is driven, not dispatched (#268/R36-09).** The
  GitLab dispatch seam (`CITharnessBackend.start`) does not yet carry the
  lane-resume contract (`FORGE_LANE_RESUME` / lane-control credentials)
  the GitHub lane dispatches (R32-04) — so CE-2 runs the resumed
  runner's REAL lane subprocess with the resume environment the CI job
  will receive once that parity lands (exactly the provider-neutral
  PE-1 shape). Everything the control plane owns — the checkpoint
  authority that admits the `/retry`, the resume command, the
  restore-into-generation, the shipped collector — is REAL. The
  qualification profile (`qualification/profiles/gitlab-ce-v1.md` §8)
  and the live evidence bundle record the same gap honestly.

## The CE cold-install qualification (R36-09, issue #268)

Beside the offline traces, the profile is qualified against the LIVE lab
by `scripts/qualify_gitlab_ce.py` (staged: `preflight` →
`install-check` → `flow` → `report`; evidence lands in
`qualification/profiles/gitlab-ce-v1-evidence.json`). The rules the
driver enforces on itself: preflight and install-check are free and
always attempted; the paid `flow` stage REFUSES unless preflight is
fully green AND budget caps are present; every refused stage records its
precise reason (an honest refused stage beats a fabricated green); the
shared lab containers are never touched — the runner-loss drill cancels
the CI job through the native API.

## The required CI gate (R36-08, issue #267 / AT-09)

A green fast suite proves nothing about the PG-gated traces — without a
PostgreSQL they *skip silently*. Since R36-08 the `integration` CI job
runs **`scripts/pg_gate.py`**, a required qualification gate beside the
ADR-0017 failure-injection bar:

- It provisions **two disposable databases through the alembic chain**
  (`python -m forge.migrate`, never `create_all`) on a dedicated
  `postgres:16` service container — one for this suite (its conftest
  resets the whole `public` schema per test under `FORGE_PG_TEST_URL`,
  so it must not share a database with the checkpoint traces), one for
  `tests/test_checkpoint_gc.py`, `tests/test_checkpoint_repository.py`
  and `tests/test_checkpoint_retry_authority.py`.
- It collects the selection first and **refuses** (non-zero, report
  artifact still uploaded) when a required test id is missing from the
  manifest — deleting or renaming PE-4, the GC barrier tests or the
  repository PG tests is a marker-removal mutation the gate detects.
- **Zero required skips**: any skip mentioning `FORGE_PG_TEST_URL` fails
  the gate (AT-09 — remove the URL and the job goes red, not
  skip-green). The only tolerated skips are the podman-bound
  retry-authority lab's, recorded as `environment` skips in the report —
  visible, never silent.
- `qualification.required_skips`, `qualification.executed_test_ids`,
  per-test durations and per-profile runtimes land in the uploaded
  `pg-gate-qualification-report` artifact for release evidence.

Local run (the `forge-postgres` lab container):

    podman exec forge-postgres psql -U forge -d forge \
        -c 'CREATE DATABASE forge_gate_e2e'
    FORGE_PG_TEST_URL=postgresql+asyncpg://forge:forge@localhost:5433/forge_gate_e2e \
        uv run python scripts/pg_gate.py --report /tmp/pg-gate-local.json

Negative arms (both must refuse, never skip green):
`--mode use` against a database migrated only to an early revision, and
against one with a migration table dropped; and any run with a wrong or
missing `FORGE_PG_TEST_URL`. The script's decision logic is unit-tested
in `tests/test_pg_gate.py`.
