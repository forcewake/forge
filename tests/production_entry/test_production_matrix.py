"""R41-07 (#362) — the production-path qualification matrix.

The review's basis, restated: the suite was green and the new defects lived
in COMBINATIONS — admission + finite budget, own commit + head fence,
Redis marker + failed persistence. More isolated happy-path assertions
cannot cover those compositions. This module is the small REQUIRED matrix
the review asked for, executed through the seams a customer drives:

- **native webhook routing** — every command enters through a REAL ASGI
  gateway subprocess (uvicorn serving ``forge.main.create_app`` over the
  trace's durable database), authenticated like GitLab;
- **durable worker entry** — the INSTALLED composition runs in its own OS
  process (``production_matrix_worker.py``: ``run_step_worker`` + the
  ``run_reconciler`` pass tuple, the exact loops ``worker/app.main``
  gathers), never a cancelled in-process task, so the fault windows below
  are REAL process kills;
- **the ACTUAL budget helpers** — ``open_budget_from_spec`` / ``reserve`` /
  ``budget_block_reason`` run inside the shipped legs and the assertions
  read their rows through the same helpers;
- **real SQL transactions** — a file-backed aiosqlite database (real
  PostgreSQL under ``FORGE_PG_TEST_URL``) whose rows survive every process
  kill in the trace;
- **the shipped collector** — the harness arms run
  ``python -m forge.harness_entry --collect-candidate`` exactly as the CI
  template invokes it, and its artifacts travel the native API.

The stubs replace the MODEL only (the deterministic planner/implementer/
reviewer behind ``build_default_agents``) and the runner's file edits —
never the policy under test.

The matrix (MX-1): ``finite/unlimited × builtin/harness ×
initial/correction/retry × Redis-present/absent`` on the GitLab lane — 24
parametrized arms, each a complete production path through the legs its
combination names, asserting the BUSINESS invariant of every axis it
crosses (the frozen budget rows, the independent round child with its OWN
budget on the shared collaboration target, the retry re-dispatch onto the
target branch, the dedup answer backed by a durable row).

The fault windows (MX-2): barriers around the acceptance commit (the
gateway process), the child admission, the native commit, the journal
completion (the worker process) and the occupancy mixed-history window —
the worker and the API are killed INDEPENDENTLY, as real OS processes.

The mutation arms (MX-3): each seeded defect runs against the SAME baseline
sequence as the matrix arm it pairs with, asserting the DEFECT's
observable — the baseline arm's assertions fail under each patch:

- (a) ``budget-before-child`` — the pre-#356 admission ordering: every
  FINITE round admission dies on ``open_budget``'s application guard;
- (b) ``pre-commit-cache`` — the pre-#357 ingress: the correction path's
  retry answers a SUCCESSFUL EMPTY DUPLICATE (the classic and adaptive
  legs already carry their arms in ``test_durable_ingress.py`` — this is
  the matrix's correction-path extension);
- (c) ``base-guard-first`` — the pre-#358 recovery order: the round's OWN
  commit at the head stales the round instead of adopting it, before any
  re-dispatch creates another effect;
- (d) ``any-terminal-occupancy`` — the pre-#360 predicate: a historical
  terminal beside a current running job releases the slot
  (oversubscription at capacity one).

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

import forge.adaptive.mailbox_db  # noqa: F401 — control_commands joins the schema
import forge.adaptive.pause_fence  # noqa: F401 — pause_fences joins the schema
from forge.adaptive.command_router import reset_shared_control_service
from forge.database import reset_engine
from forge.durable import Controller, FlowRun, FlowStatus
from forge.durable.budgets import budget_block_reason, budget_for_run
from forge.durable.models import (
    CollaborationTarget,
    MRReservation,
    Outbox,
    ReviewRound,
    RunBudget,
    StepRun,
)

from .conftest import GL_BASE_BRANCH, GL_PROJECT_ID, PE_HARNESS_MODEL
from .test_durable_ingress import Gateway, _await_truth, _wait_for_ready
from .test_durable_ingress import redis_server as _matrix_redis_server  # noqa: F401 — a fixture
from .test_mutation_gates import trace_record, write_trace_record
from .test_production_entry import make_checkout, run_collector

pytestmark = pytest.mark.production_entry

#: The webhook shared secret the child gateways authenticate with.
MX_WEBHOOK_SECRET = "mx-matrix-whsec"  # noqa: S105 — fixture value

GATEWAY_HARNESS = Path(__file__).parent / "durable_ingress_server.py"
WORKER_HARNESS = Path(__file__).parent / "production_matrix_worker.py"

#: The matrix's own issue (every arm seeds it fresh on a clean database).
MX_ISSUE_IID = 77
MX_ISSUE_TITLE = "Qualify the production paths"
MX_ISSUE_DESC = "The composed matrix issue."

#: The finite axis's frozen profile (resolved at spec freeze; unlimited arms
#: configure nothing — the run then has NO budget row by design).
MX_FINITE_PROFILES = json.dumps({"standard": {"max_calls": 40, "max_tokens": 500_000}})
MX_FINITE_MAX_CALLS = 40

#: The approved write scope (the .forge.yml the classification and the
#: builtin validation both enforce): covers the stub implementer's
#: ``forge-demo/`` proposals AND the /fix note's claimed path.
MX_FORGE_YML = "implement:\n  paths:\n    - forge-demo/**\n"

#: The reviewer's correction (a backticked in-scope path claim).
MX_FIX_NOTE = "/fix also cover `forge-demo/notes.md` empty input"

#: The harness runner's file edits (the model's remote effect, stubbed as
#: direct working-tree edits the shipped collector then captures).
MX_RUNNER_EDITS = {"forge-demo/widget.md": "print('matrix widget')\n"}

#: The required CI job the verification contract demands green.
MX_REQUIRED_JOB = "verify"


def _mx_meta(attempt_base: str) -> bytes:
    """``.forge/candidate.meta.json`` — the CI lane's contract (ADR-0016
    §1) with a KNOWN usage receipt, so the finite arms' budget reconciles
    real counters (never the unknown-receipt stop the R23 policy mandates
    for unverifiable token figures on a hard-limited axis)."""
    return json.dumps(
        {
            "attempt_base": attempt_base,
            "driver": "claude-code",
            "model": PE_HARNESS_MODEL,
            "exit": "completed",
            "usage": {"input_tokens": 400, "output_tokens": 100},
        }
    ).encode("utf-8")


#: The full matrix: 2 × 2 × 3 × 2 = 24 required arms.
MATRIX: tuple[tuple[str, str, str, str], ...] = tuple(
    (budget, backend, leg, redis_mode)
    for budget in ("finite", "unlimited")
    for backend in ("builtin", "harness")
    for leg in ("initial", "correction", "retry")
    for redis_mode in ("redis", "noredis")
)


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_", "AZURE_", "REDIS_URL", "LITELLM_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_shared_control():
    """The process-shared control mailbox never leaks across traces."""
    reset_shared_control_service()
    yield
    reset_shared_control_service()
    reset_engine()


@pytest.fixture()
def redis_server(request: pytest.FixtureRequest):
    """The real-redis fixture from the durable-ingress matrix, re-exported
    (resolved by name so the re-export stays ruff-clean)."""
    return request.getfixturevalue("_matrix_redis_server")


@pytest.fixture(autouse=True)
def _record_the_trace(request: pytest.FixtureRequest):
    """Record every MX trace/arm execution (the #344 record machinery)."""
    from .conftest import TRACE_OUTCOME_KEY

    started = time.monotonic()
    yield
    marker = request.node.get_closest_marker("trace_record")
    if marker is None:
        return
    write_trace_record(
        request.node.nodeid,
        label=str(marker.kwargs.get("label", "")),
        mutation=marker.kwargs.get("mutation"),
        outcome=request.node.stash.get(TRACE_OUTCOME_KEY, "executed"),
        duration_seconds=time.monotonic() - started,
    )


# ----------------------------------------------------------------------
# The child processes: the REAL gateway and the REAL worker
# ----------------------------------------------------------------------


def _mx_env(
    *, db_url: str, gitlab_url: str, budget: str, backend: str, redis_url: str | None
) -> dict[str, str]:
    """The Settings environment every matrix child runs under (the gateway
    and the worker read the same knobs — one frozen configuration)."""
    env = dict(os.environ)
    env.update(
        {
            "GITLAB_URL": gitlab_url,
            "GITLAB_TOKEN": "mx-gitlab-token",  # noqa: S106 — fixture value
            "GITLAB_WEBHOOK_SECRET": MX_WEBHOOK_SECRET,
            "DATABASE_URL": db_url,
            "FORGE_APPROVERS": "alice",
            "FORGE_ADAPTIVE_COMMANDS_ENABLED": "1",
            "FORGE_REVIEW_FEEDBACK_ENABLED": "1",
            "FORGE_CAPTURE_DIR": "",
            "FORGE_VERIFICATION_GRACE_SECONDS": "0",
            "FORGE_REQUIRED_JOBS": MX_REQUIRED_JOB,
            "FORGE_IMPLEMENTER_BACKEND": (
                "ci_harness:claude-code" if backend == "harness" else "builtin"
            ),
        }
    )
    if backend == "harness":
        env["FORGE_HARNESS_MODEL"] = PE_HARNESS_MODEL
    if budget == "finite":
        env["FORGE_BUDGET_PROFILES"] = MX_FINITE_PROFILES
    else:
        env.pop("FORGE_BUDGET_PROFILES", None)
    if redis_url:
        env["REDIS_URL"] = redis_url
    else:
        env.pop("REDIS_URL", None)
    return env


def start_mx_gateway(
    tmp_path: Path,
    *,
    db_url: str,
    gitlab_url: str,
    budget: str,
    backend: str,
    redis_url: str | None = None,
    kill: str | None = None,
    db_failures: int = 0,
    mutation: str | None = None,
    name: str = "mx-gw",
) -> Gateway:
    """The REAL ASGI gateway subprocess (the shared #357 harness) with the
    matrix's frozen configuration and the sanctioned model stub.

    The gateway is ALWAYS pinned to ingress-only (``--no-nudge``): the
    matrix's designated executor is the REAL worker child
    (``production_matrix_worker.py`` — the docstring's "never a cancelled
    in-process task"), and the shipped no-Redis gateway would otherwise
    race it through the after-response in-process nudge on any machine
    without an ambient localhost redis (the CI runner — the exact
    "worker child exited 0 (expected -9)" desynchronization). The 202
    still follows the durable inbox+step commit; only the in-process
    execution accelerator is off."""
    ready = tmp_path / f"{name}-ready.json"
    env = _mx_env(
        db_url=db_url, gitlab_url=gitlab_url, budget=budget, backend=backend, redis_url=redis_url
    )
    command = [
        sys.executable,
        str(GATEWAY_HARNESS),
        "--ready-file",
        str(ready),
        "--db-url",
        db_url,
        "--stub-model",
        "--no-nudge",
    ]
    if kill:
        command += ["--kill", kill]
    if db_failures:
        command += ["--db-failures", str(db_failures)]
    if mutation:
        command += ["--mutation", mutation]
    process = subprocess.Popen(
        command,
        cwd=tmp_path,  # no repo .env: the matrix env above is the whole config
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port = _wait_for_ready(process, ready)
    return Gateway(process, port)


def run_mx_worker(
    tmp_path: Path,
    *,
    db_url: str,
    gitlab_url: str,
    budget: str,
    backend: str,
    name: str = "mx-wk",
    kill: str | None = None,
    nth: int = 1,
    mutation: str | None = None,
    mode: str = "full",
    ticks: int = 2,
    max_seconds: float = 75.0,
) -> subprocess.CompletedProcess[str]:
    """One REAL worker subprocess run — the installed step loop + the
    reconciler pass tuple over a fresh engine, until the queue is quiet.

    ``kill`` names the fault window the child SIGKILLs itself at (the
    return code is then ``-9``); ``mutation`` seeds one regression.
    """
    ready = tmp_path / f"{name}-{time.monotonic_ns()}-ready.json"
    env = _mx_env(
        db_url=db_url,
        gitlab_url=gitlab_url,
        budget=budget,
        backend=backend,
        redis_url=None,  # the worker recovers from SQL alone (the #357 shape)
    )
    command = [
        sys.executable,
        str(WORKER_HARNESS),
        "--ready-file",
        str(ready),
        "--db-url",
        db_url,
        "--mode",
        mode,
        "--max-seconds",
        str(max_seconds),
    ]
    if kill:
        command += ["--kill", kill, "--nth", str(nth)]
    if mutation:
        command += ["--mutation", mutation]
    if mode == "reconciler":
        command += ["--ticks", str(ticks)]
    outcome = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=max_seconds + 45,
    )
    expected = -9 if kill else 0
    if outcome.returncode != expected:
        raise AssertionError(
            f"the worker child exited {outcome.returncode} (expected {expected}):\n"
            f"stdout: {outcome.stdout[-3000:]}\nstderr: {outcome.stderr[-3000:]}"
        )
    return outcome


# ----------------------------------------------------------------------
# Native webhook payloads (the shapes GitLab POSTs)
# ----------------------------------------------------------------------


def _mx_issue_note(note_id: int, text: str) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice Approver", "username": "alice", "email": ""},
        "project": {
            "id": GL_PROJECT_ID,
            "name": "forge-mx",
            "path_with_namespace": "acme/forge-mx",
            "web_url": "https://gitlab.test/acme/forge-mx",
        },
        "object_attributes": {
            "id": note_id,
            "note": text,
            "noteable_type": "Issue",
            "noteable_id": 77,
            "author_id": 11,
            "discussion_id": f"d-{note_id}",
        },
        "issue": {
            "id": 77000 + MX_ISSUE_IID,
            "iid": MX_ISSUE_IID,
            "title": MX_ISSUE_TITLE,
            "description": MX_ISSUE_DESC,
        },
    }


def _mx_mr_note(note_id: int, text: str, mr_iid: int) -> dict:
    payload = _mx_issue_note(note_id, text)
    payload["object_attributes"]["noteable_type"] = "MergeRequest"
    payload.pop("issue")
    payload["merge_request"] = {
        "id": 88000 + mr_iid,
        "iid": mr_iid,
        "title": "Draft: the candidate",
        "source_branch": "forge/factory-mx",
        "target_branch": GL_BASE_BRANCH,
        "state": "opened",
    }
    return payload


async def _post(
    gateway: Gateway, note_id: int, text: str, *, delivery: str, mr_iid: int | None = None
):
    """POST one note webhook; a SIGKILLed child surfaces as ``None``."""
    payload = (
        _mx_mr_note(note_id, text, mr_iid) if mr_iid is not None else _mx_issue_note(note_id, text)
    )
    headers = {"X-Gitlab-Token": MX_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"}
    headers["X-Gitlab-Event-UUID"] = delivery
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            return await client.post(gateway.base_url + "/webhook", json=payload, headers=headers)
        except httpx.TransportError:
            return None


# ----------------------------------------------------------------------
# Durable reads (real SQL, the actual budget helpers)
# ----------------------------------------------------------------------


async def _all_runs(pe_db) -> list[FlowRun]:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return list((await session.execute(select(FlowRun))).scalars().all())


async def _run_of(pe_db, run_id: str) -> FlowRun:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return await session.get(FlowRun, run_id)


async def _budget_of(pe_db, run_id: str) -> RunBudget | None:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return await budget_for_run(session, run_id)


async def _block_reason(pe_db, run_id: str) -> str | None:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return await budget_block_reason(session, run_id)


async def _rounds_of(pe_db) -> list[ReviewRound]:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return list(
            (await session.execute(select(ReviewRound).order_by(ReviewRound.round_number)))
            .scalars()
            .all()
        )


async def _target_of(pe_db, run_id: str) -> CollaborationTarget | None:
    factory = pe_db.worker_factory()
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None or not run.target_id:
            return None
        return await session.get(CollaborationTarget, run.target_id)


async def _reservation_branches(pe_db, run_id: str) -> list[str]:
    factory = pe_db.worker_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(MRReservation.branch).where(MRReservation.flow_run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _steps_of(pe_db, name: str) -> list[StepRun]:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return list(
            (await session.execute(select(StepRun).where(StepRun.step_name == name)))
            .scalars()
            .all()
        )


def _dispatch_refs(gitlab_native) -> list[str]:
    return [entry["ref"] for entry in gitlab_native.dispatches()]


async def _expire_dead_claims(pe_db) -> int:
    """Advance the clock past the lease (and the reaper's grace window) of
    every RUNNING step — the honest spelling of "the lease window elapsed"
    for a worker SIGKILLed mid-step (the lease is 120s; the trace waits it
    out in one tick). The REAL reaper inside the recovery worker then
    reschedules the step — nothing here touches step state beyond the
    clock."""
    from datetime import timedelta

    from forge.worker.steps import STEP_REAP_GRACE_SECONDS, _utcnow

    factory = pe_db.worker_factory()
    async with factory() as session:
        result = await session.execute(
            update(StepRun)
            .where(StepRun.status == "running")
            .values(lease_expires_at=_utcnow() - timedelta(seconds=STEP_REAP_GRACE_SECONDS + 5))
        )
        await session.commit()
        return int(result.rowcount or 0)


# ----------------------------------------------------------------------
# The shared baseline sequence (every arm and mutation runs IT)
# ----------------------------------------------------------------------


def _seed_workspace(gitlab_native, tmp_path: Path | None, backend: str) -> tuple[Path, str] | None:
    """The repo snapshot the classification/publish/collector legs read."""
    gitlab_native.seed_issue(MX_ISSUE_IID, MX_ISSUE_TITLE, MX_ISSUE_DESC)
    gitlab_native.seed_file(".forge.yml", MX_FORGE_YML)
    if backend != "harness":
        return None
    checkout, base_oid = make_checkout(tmp_path, "mx-workspace")
    gitlab_native.seed_commit(GL_BASE_BRANCH, base_oid, "frozen base")
    for name in ("README.md", "src/app.py", "run.sh"):
        gitlab_native.seed_file(name, (checkout / name).read_text())
    return checkout, base_oid


def _worker_run(tmp_path, pe_db, gitlab_native, *, budget: str, backend: str, name: str, **kwargs):
    return run_mx_worker(
        tmp_path,
        db_url=pe_db.url,
        gitlab_url=gitlab_native.base_url,
        budget=budget,
        backend=backend,
        name=name,
        **kwargs,
    )


async def _drive_matrix(
    pe_db,
    gitlab_native,
    tmp_path: Path,
    *,
    budget: str,
    backend: str,
    leg: str,
    redis_url: str | None,
    tag: str,
    keep_gateway: bool = False,
) -> SimpleNamespace:
    """The SAME baseline sequence every matrix arm and mutation arm runs:
    issue → /implement → /go → (the backend's publication leg) → the named
    leg's own sequence — everything through the REAL gateway and the REAL
    worker subprocess over one durable database and one native server.

    ``keep_gateway`` leaves the gateway running for the caller that drives
    further deliveries through it (the fault-window and mutation traces);
    everyone else gets it stopped here.
    """
    workspace = _seed_workspace(gitlab_native, tmp_path, backend)
    gateway = start_mx_gateway(
        tmp_path,
        db_url=pe_db.url,
        gitlab_url=gitlab_native.base_url,
        budget=budget,
        backend=backend,
        redis_url=redis_url,
        name=f"mx-gw-{tag}",
    )
    dispatches_before = len(gitlab_native.dispatches())
    try:
        # --- the INITIAL leg: /implement → the human gate ---------------
        first = await _post(
            gateway, 3001, f"@forge /implement {MX_ISSUE_TITLE}", delivery=f"{tag}-imp"
        )
        assert first is not None and first.status_code == 202
        assert first.json()["run_command"] is True
        _worker_run(
            tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk1"
        )
        [run] = await _all_runs(pe_db)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        run_id = str(run.id)
        target = await _target_of(pe_db, run_id)
        assert target is not None and target.status == "active"

        # --- the INITIAL leg: /go → the backend's publication -----------
        go = await _post(gateway, 3002, f"@forge /go {run_id}", delivery=f"{tag}-go")
        assert go is not None and go.status_code == 202
        _worker_run(
            tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk2"
        )

        if backend == "builtin":
            published = await _run_of(pe_db, run_id)
            assert published.status == FlowStatus.WAITING_CI.value, published.status
        else:
            waiting = await _run_of(pe_db, run_id)
            assert waiting.status == FlowStatus.WAITING_HARNESS.value, waiting.status
            (dispatch,) = gitlab_native.dispatches()
            job = gitlab_native.jobs(dispatch["pipeline_id"])[0]
            # The runner's work (the stubbed model effect) + the SHIPPED
            # collector + the native artifacts + the green job.
            checkout, base_oid = workspace  # type: ignore[misc]
            for path, content in MX_RUNNER_EDITS.items():
                (checkout / path).parent.mkdir(parents=True, exist_ok=True)
                (checkout / path).write_text(content)
            outcome = run_collector(checkout, work_id=run_id, attempt_base=base_oid)
            assert outcome.returncode == 0, outcome.stderr
            reported = json.loads(outcome.stdout)
            diff = Path(reported["diff_path"]).read_bytes()
            gitlab_native.seed_artifact(job["id"], ".forge/candidate.diff", diff)
            gitlab_native.seed_artifact(job["id"], ".forge/candidate.meta.json", _mx_meta(base_oid))
            gitlab_native.mark_job(job["id"], "success")
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk3"
            )
            published = await _run_of(pe_db, run_id)
            assert published.status == FlowStatus.WAITING_CI.value, published.status

        branch = str((await _target_of(pe_db, run_id)).source_branch)
        assert branch
        run = await _run_of(pe_db, run_id)
        candidate_sha = str(list(run.candidate_shas or [])[-1])
        assert candidate_sha
        mr_iid = int(run.mr_iid)
        commits_before = len(gitlab_native.state()["branches"].get(branch, []))

        # --- the named leg ----------------------------------------------
        child_id = ""
        if leg == "initial":
            gitlab_native.seed_pipeline(
                ref=branch,
                sha=candidate_sha,
                status="success",
                jobs=[{"name": MX_REQUIRED_JOB, "status": "success"}],
            )
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk4"
            )
            ready = await _run_of(pe_db, run_id)
            assert ready.status == FlowStatus.READY_FOR_HUMAN.value, ready.status
        elif leg == "correction":
            gitlab_native.seed_pipeline(
                ref=branch,
                sha=candidate_sha,
                status="success",
                jobs=[{"name": MX_REQUIRED_JOB, "status": "success"}],
            )
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk4"
            )
            ready = await _run_of(pe_db, run_id)
            assert ready.status == FlowStatus.READY_FOR_HUMAN.value, ready.status
            fix = await _post(gateway, 3003, MX_FIX_NOTE, delivery=f"{tag}-fix", mr_iid=mr_iid)
            assert fix is not None and fix.status_code == 202
            assert fix.json()["run_command"] is True
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk5"
            )
            rounds = await _rounds_of(pe_db)
            assert len(rounds) == 1
            child_id = str(rounds[0].child_run_id)
        else:  # retry — the infrastructure-red verdict parks the run blocked
            gitlab_native.seed_pipeline(
                ref=branch,
                sha=candidate_sha,
                status="failed",
                jobs=[{"name": MX_REQUIRED_JOB, "status": "failed"}],
            )
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk4"
            )
            blocked = await _run_of(pe_db, run_id)
            assert blocked.status == FlowStatus.BLOCKED.value, blocked.status
            # The harness lane cannot prove no vendor session started (its
            # model runs inside CI), so the honest continuation source is
            # the operator's EXPLICIT restart verb — the documented retry
            # argument position; the builtin lane records its bootstrap
            # classification and the plain retry re-dispatches.
            verb = " restart" if backend == "harness" else ""
            retry = await _post(
                gateway, 3003, f"@forge /retry {run_id}{verb}", delivery=f"{tag}-rty"
            )
            assert retry is not None and retry.status_code == 202
            _worker_run(
                tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name=f"{tag}-wk5"
            )

        # --- the replay probe (the Redis axis): the SAME delivery again --
        replay = await _post(gateway, 3002, f"@forge /go {run_id}", delivery=f"{tag}-go")
        assert replay is not None and replay.status_code == 202
        assert replay.json()["deduplicated"] is True
        assert len(await _steps_of(pe_db, "go")) == 1  # ONE durable command

        return SimpleNamespace(
            run_id=run_id,
            branch=branch,
            mr_iid=mr_iid,
            candidate_sha=candidate_sha,
            child_id=child_id,
            target=await _target_of(pe_db, run_id),
            dispatches_total=len(gitlab_native.dispatches()) - dispatches_before,
            commits_after_leg=len(gitlab_native.state()["branches"].get(branch, []))
            - commits_before,
            gateway=gateway,
        )
    finally:
        if not keep_gateway:
            gateway.stop()


# ----------------------------------------------------------------------
# MX-1 — the required matrix (24 arms, the composed combinations)
# ----------------------------------------------------------------------


class TestMX1TheProductionMatrix:
    """One parametrized arm per matrix cell; every axis's BUSINESS
    invariant asserted on artifacts (DB rows read through the actual
    helpers, the native dispatch ledger, branch commits, MR notes)."""

    @pytest.mark.parametrize(("budget", "backend", "leg", "redis_mode"), MATRIX)
    @trace_record("MX-1 the production matrix arm")
    async def test_the_complete_production_path(
        self,
        pe_db,
        gitlab_native,
        tmp_path,
        request: pytest.FixtureRequest,
        budget,
        backend,
        leg,
        redis_mode,
    ):
        redis_url = request.getfixturevalue("redis_server").url if redis_mode == "redis" else None
        mx = await _drive_matrix(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            leg=leg,
            redis_url=redis_url,
            tag=f"{budget[:3]}-{backend[:3]}-{leg[:4]}-{redis_mode}",
        )
        run = await _run_of(pe_db, mx.run_id)
        target = mx.target

        # --- the platform invariants (every arm) -------------------------
        # The collaboration target is the persisted surface: the branch the
        # whole lineage collaborates on, recorded once at admission.
        assert target.source_branch == mx.branch
        assert target.target_branch == GL_BASE_BRANCH
        # Never off the modeled API — the ONE tolerated exception is the
        # MR discussions surface the fake does not model (the FI-4
        # convention: the 404 degrades the auxiliary read, it never fails
        # the request and never leaves the degraded state unasserted).
        off_api = [path for path in gitlab_native.unknown_paths() if "/discussions" not in path]
        assert off_api == []
        # The plan evidence comment landed NATIVELY with the approval gate.
        assert any(f"/go {mx.run_id}" in body for body in gitlab_native.notes()), (
            "the evidence-backed plan gate never reached the native issue"
        )
        # The replay (either axis) left exactly ONE /go command durable.
        assert len(await _steps_of(pe_db, "go")) == 1

        # --- the BUDGET axis (the actual helpers) ------------------------
        root_budget = await _budget_of(pe_db, mx.run_id)
        if budget == "finite":
            assert root_budget is not None, "the finite run opened no budget"
            assert root_budget.max_calls == MX_FINITE_MAX_CALLS  # frozen at open
            assert root_budget.status in ("open", "exhausted")
            assert await _block_reason(pe_db, mx.run_id) is None
        else:
            assert root_budget is None, "the unlimited run must carry no budget row"

        # --- the LEG axis -------------------------------------------------
        if leg == "initial":
            # The first delivery: candidate committed natively, the Draft MR
            # open on the collaboration branch, the closing review recorded
            # WITH its obligation identity (#361).
            assert gitlab_native.branch_head(mx.branch) == mx.candidate_sha
            (mr,) = gitlab_native.merge_requests().values()
            assert mr["title"].startswith("Draft:")
            assert mr["source_branch"] == mx.branch
            review = dict((run.evidence or {}).get("review") or {})
            assert review.get("sha") == mx.candidate_sha
            assert review.get("obligation_digest"), "the review verdict carries no obligation"
        elif leg == "correction":
            # The round child: an INDEPENDENT run id on the SAME persisted
            # collaboration target, its OWN budget under the finite axis,
            # the MR reservation on the target branch (#356/#359 composed).
            assert mx.child_id
            assert mx.child_id[:8] != mx.run_id[:8]  # work identity ≠ parent prefix
            child = await _run_of(pe_db, mx.child_id)
            child_target = await _target_of(pe_db, mx.child_id)
            assert child_target is not None and child_target.id == target.id
            assert child.target_id == target.id
            assert mx.branch in await _reservation_branches(pe_db, mx.child_id)
            child_budget = await _budget_of(pe_db, mx.child_id)
            if budget == "finite":
                assert child_budget is not None, "the round child opened no own budget"
                assert child_budget.max_calls == MX_FINITE_MAX_CALLS
                assert child_budget.id != root_budget.id  # its OWN ledger, no amendment
            else:
                assert child_budget is None
            [round_row] = await _rounds_of(pe_db)
            assert round_row.round_number == 2
            assert round_row.root_run_id == mx.run_id
            if backend == "harness":
                # the round's dispatch rode the TARGET branch
                assert _dispatch_refs(gitlab_native).count(mx.branch) >= 2
            else:
                # the round child published ONE new commit on the target branch
                assert mx.commits_after_leg >= 1
                assert gitlab_native.branch_head(mx.branch) != mx.candidate_sha
            # The operator reply names the round with its human reference
            # (an MR note — the collaboration surface the round rides).
            mr_note_bodies = [entry["body"] for entry in gitlab_native.state()["mr_notes"]]
            assert any("Review round 2 opened" in body for body in mr_note_bodies), (
                "the round admission never answered the reviewer"
            )
        else:  # retry
            # The re-dispatch rode the COLLABORATION target branch (#359):
            # work continues in place, never a re-derived branch.
            assert any("retried by @alice" in body for body in gitlab_native.notes())
            if backend == "harness":
                assert _dispatch_refs(gitlab_native).count(mx.branch) >= 2
            else:
                assert mx.commits_after_leg >= 1  # the retry's new candidate
            assert target.source_branch == mx.branch  # the surface never moved

        # --- the BACKEND axis ---------------------------------------------
        if backend == "harness":
            # The dispatch ledger carries the frozen RunSpec shape.
            dispatch = next(d for d in gitlab_native.dispatches() if d["ref"] == mx.branch)
            variables = {v["key"]: v["value"] for v in dispatch["variables"]}
            assert variables["FORGE_RUN_ID"] == mx.run_id
            assert variables["FORGE_HARNESS_DRIVER"] == "claude-code"
        else:
            # The builtin lane published through the REAL writer: the branch
            # carries the committed candidate and the Draft MR adopted it.
            assert gitlab_native.branch_head(mx.branch) is not None


# ----------------------------------------------------------------------
# MX-2 — the fault windows (real process kills at the boundaries)
# ----------------------------------------------------------------------


class TestMX2TheFaultWindows:
    """Barriers around the acceptance commit (the gateway process), the
    child admission / native commit / journal completion (the worker
    process) and the occupancy mixed-history window. Every boundary is a
    REAL SIGKILL of a REAL OS process; the recovering side is a FRESH
    process over the same durable rows."""

    async def _ready_delivery(
        self, pe_db, gitlab_native, tmp_path, *, budget: str, backend: str, tag: str, note_base: int
    ) -> SimpleNamespace:
        """The baseline through the ready delivery (delivery 1 verified),
        with the gateway kept alive for the caller's leg."""
        mx = await _drive_matrix(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            leg="initial",
            redis_url=None,
            tag=tag,
            keep_gateway=True,
        )
        assert mx.run_id
        del note_base
        return mx

    @trace_record("MX-2a the acceptance-commit window (gateway kill)")
    async def test_gateway_killed_after_commit_the_correction_recovers(
        self, pe_db, gitlab_native, tmp_path
    ):
        """The /fix delivery acknowledged-then-killed before the response:
        the committed review_feedback step IS the command — the fresh
        worker admits the round with NO resend (the #357 contract, on the
        matrix's correction path). The API dies here; the worker never
        does (the two processes die INDEPENDENTLY across MX-2)."""
        budget, backend = "finite", "builtin"
        mx = await self._ready_delivery(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            tag="mx2a",
            note_base=3100,
        )
        try:
            # THE WINDOW: the gateway dies AFTER the inbox commit, before
            # the acknowledgement leaves (a fresh gateway plays the resend).
            killer = start_mx_gateway(
                tmp_path,
                db_url=pe_db.url,
                gitlab_url=gitlab_native.base_url,
                budget=budget,
                backend=backend,
                kill="after-commit",
                name="mx2a-gw-kill",
            )
            try:
                response = await _post(
                    killer, 3103, MX_FIX_NOTE, delivery="mx2a-fix", mr_iid=mx.mr_iid
                )
                assert response is None  # the process died inside the request
            finally:
                killer.stop()

            async def _step_landed() -> bool:
                return bool(await _steps_of(pe_db, "review_feedback"))

            assert await _await_truth(_step_landed, timeout=10), "the commit did not survive"
            steps = await _steps_of(pe_db, "review_feedback")
            assert steps[0].status == "scheduled"  # durable, never executed
        finally:
            mx.gateway.stop()

        # NO resend: the fresh worker alone admits the round.
        _worker_run(tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name="mx2a-wk4")
        rounds = await _rounds_of(pe_db)
        assert len(rounds) == 1  # exactly one round, no redelivery anywhere
        assert len(await _steps_of(pe_db, "review_feedback")) == 1
        child = await _run_of(pe_db, rounds[0].child_run_id)
        assert child is not None  # the round's child exists (finite budget opened)

    @trace_record("MX-2b the child-admission window (worker kill)")
    async def test_worker_killed_inside_the_admission_nothing_partial(
        self, pe_db, gitlab_native, tmp_path
    ):
        """SIGKILL inside the round ADMISSION transaction (after the child
        flush, at the child-budget open): the round row, child run, child
        budget, reservation and outbox ALL die with the uncommitted
        connection — delivery 1 stands alone; the redelivered /fix admits
        exactly one round afterwards. The gateway stays UP throughout
        (the API and the worker die INDEPENDENTLY)."""
        budget, backend = "finite", "builtin"
        mx = await self._ready_delivery(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            tag="mx2b",
            note_base=3200,
        )
        try:
            fix = await _post(mx.gateway, 3203, MX_FIX_NOTE, delivery="mx2b-fix", mr_iid=mx.mr_iid)
            assert fix is not None and fix.status_code == 202
            killed = _worker_run(
                tmp_path,
                pe_db,
                gitlab_native,
                budget=budget,
                backend=backend,
                name="mx2b-wk-leg",
                kill="child-admission",
            )
            assert killed.returncode == -9

            # NOTHING partial: no round, no child, no child budget — the
            # admission's ONE transaction died with the process.
            assert await _rounds_of(pe_db) == []
            runs = await _all_runs(pe_db)
            assert [row.id for row in runs] == [mx.run_id]
            # delivery 1's own budget stands (the kill was inside the CHILD's
            # admission, never the parent's rows).
            assert await _budget_of(pe_db, mx.run_id) is not None

            # The API (still up) redelivers; the fresh worker admits exactly
            # ONE round — the request row is the idempotency.
            replay = await _post(
                mx.gateway, 3203, MX_FIX_NOTE, delivery="mx2b-fix2", mr_iid=mx.mr_iid
            )
            assert replay is not None and replay.status_code == 202
        finally:
            mx.gateway.stop()
        # The killed worker's step lease elapses (the trace's clock advance)
        # and the recovery worker's REAL reaper reschedules it; the step
        # loop then re-executes the SAME durable command.
        assert await _expire_dead_claims(pe_db) == 1
        _worker_run(
            tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name="mx2b-wk-recover"
        )
        rounds = await _rounds_of(pe_db)
        assert len(rounds) == 1
        assert await _budget_of(pe_db, rounds[0].child_run_id) is not None

    @trace_record("MX-2c the native-commit window (worker kill)")
    async def test_worker_killed_after_the_native_commit_the_intent_recovers(
        self, pe_db, gitlab_native, tmp_path
    ):
        """SIGKILL after the round child's native commit landed, before the
        journal recorded it: the OPEN publication intent survives, the
        fresh worker's INSTALLED reconciler probe-recovers it (the R11
        marker+parent probe adopts the landed commit) — exactly ONE commit
        beyond delivery 1, no stale, no second effect, the round survives
        to complete (#358's precondition at a real process boundary)."""
        budget, backend = "finite", "builtin"
        mx = await self._ready_delivery(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            tag="mx2c",
            note_base=3300,
        )
        try:
            commits_before = len(gitlab_native.state()["branches"].get(mx.branch, []))
            fix = await _post(mx.gateway, 3303, MX_FIX_NOTE, delivery="mx2c-fix", mr_iid=mx.mr_iid)
            assert fix is not None and fix.status_code == 202
            killed = _worker_run(
                tmp_path,
                pe_db,
                gitlab_native,
                budget=budget,
                backend=backend,
                name="mx2c-wk-leg",
                kill="native-commit",
                nth=1,
            )
            assert killed.returncode == -9
        finally:
            mx.gateway.stop()
        # The native commit LANDED (the recording provider holds it) while
        # the journal never completed: the branch moved past the base.
        assert len(gitlab_native.state()["branches"].get(mx.branch, [])) == commits_before + 1

        # The fresh worker, through the INSTALLED reconciler entry, adopts
        # the round's own effect (never a second commit, never a stale).
        _worker_run(
            tmp_path,
            pe_db,
            gitlab_native,
            budget=budget,
            backend=backend,
            name="mx2c-wk-recover",
            mode="reconciler",
            ticks=3,
        )
        rounds = await _rounds_of(pe_db)
        assert len(rounds) == 1
        assert rounds[0].status not in ("stale", "ended"), rounds[0].status
        child = await _run_of(pe_db, rounds[0].child_run_id)
        assert child.status != FlowStatus.BLOCKED.value
        assert len(gitlab_native.state()["branches"].get(mx.branch, [])) == commits_before + 1

    @trace_record("MX-2d the journal-completion window (worker kill)")
    async def test_worker_killed_after_the_journal_the_own_effect_is_classified_and_adopted(
        self, pe_db, gitlab_native, tmp_path
    ):
        """SIGKILL after the writer's whole ``apply()`` returned (intent,
        commit AND journal complete; the child still sits mid-``committing``):
        the fresh worker's round pass CLASSIFIES the moved head against the
        round's OWN publication identities and adopts it — the
        ``review_round.effect_resolution{own}`` event is the observable —
        ONE commit, no duplicate publication, no stale (#358's own-effect
        classification at a real process boundary)."""
        budget, backend = "finite", "builtin"
        mx = await self._ready_delivery(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            tag="mx2d",
            note_base=3400,
        )
        try:
            commits_before = len(gitlab_native.state()["branches"].get(mx.branch, []))
            fix = await _post(mx.gateway, 3403, MX_FIX_NOTE, delivery="mx2d-fix", mr_iid=mx.mr_iid)
            assert fix is not None and fix.status_code == 202
            killed = _worker_run(
                tmp_path,
                pe_db,
                gitlab_native,
                budget=budget,
                backend=backend,
                name="mx2d-wk-leg",
                kill="journal-completion",
                nth=1,
            )
            assert killed.returncode == -9
        finally:
            mx.gateway.stop()
        assert len(gitlab_native.state()["branches"].get(mx.branch, [])) == commits_before + 1

        _worker_run(
            tmp_path,
            pe_db,
            gitlab_native,
            budget=budget,
            backend=backend,
            name="mx2d-wk-recover",
            mode="reconciler",
            ticks=3,
        )
        # the journaled candidate adopted — still exactly ONE new commit
        assert len(gitlab_native.state()["branches"].get(mx.branch, [])) == commits_before + 1
        rounds = await _rounds_of(pe_db)
        assert len(rounds) == 1 and rounds[0].status not in ("stale", "ended")
        # The classifier's own observable: the round's head resolution was
        # journaled as OWN (the journal's succeeded sha explained the head).
        factory = pe_db.worker_factory()
        async with factory() as session:
            resolutions = (
                (
                    await session.execute(
                        select(Outbox.payload).where(
                            Outbox.flow_run_id == rounds[0].child_run_id,
                            Outbox.event_type == "review_round.effect_resolution",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert any(
            isinstance(entry, dict) and entry.get("resolution") == "own" for entry in resolutions
        ), resolutions

    @trace_record("MX-2e the occupancy mixed-history window")
    async def test_mixed_history_keeps_the_lease_only_correlated_terminal_releases(
        self, pe_db, gitlab_native, tmp_path
    ):
        """The #360 baseline: a draining lease whose branch listing carries
        MIXED history — a PRIOR attempt's terminal pipeline beside the
        CURRENT attempt's running one — KEEPS the slot (the current
        execution decides); only the CORRELATED terminal release frees it.
        The paired mutation arm (MX-3d) runs this same sequence under the
        pre-#360 predicate and oversubscribes."""
        from forge.adaptive.admission import (
            AdmissionPolicy,
            lease_snapshot,
            reconcile_draining,
            try_acquire_lease,
        )
        from forge.config import ForgeConfig, Settings
        from forge.gitlab.client import GitLabClient
        from forge.runs.service import RunService, forge_token

        seeded = await _seed_mixed_history_lease(pe_db, gitlab_native)
        policy = AdmissionPolicy(max_active_per_project=1)
        settings_kwargs = _mx_settings_kwargs(gitlab_native.base_url, pe_db.url)
        gitlab = GitLabClient(
            base_url=settings_kwargs["GITLAB_URL"],
            token=forge_token(Settings(**settings_kwargs)),
        )
        try:
            service = RunService(
                session_factory=pe_db.worker_factory(),
                gitlab=gitlab,
                settings=Settings(**settings_kwargs),
                config=ForgeConfig(),
            )
            probe = service._native_occupancy_probe()

            # Mixed history: the CURRENT attempt's pipeline is RUNNING —
            # the lease keeps its slot (the historical success never frees).
            assert (await probe(seeded.intent_key)).name == "RUNNING"
            assert await reconcile_draining(pe_db.worker_factory(), probe) == 0
            held = (await lease_snapshot(policy, GL_PROJECT_ID, pe_db.worker_factory()))["held"]
            assert held == 1
            assert (
                await try_acquire_lease(
                    policy,
                    GL_PROJECT_ID,
                    pe_db.worker_factory(),
                    run_id="f" * 31 + "5",
                    provider="gitlab",
                )
                is None
            )  # capacity one — no oversubscription while the job runs

            # The CORRELATED terminal release: the current attempt's job
            # goes terminal, the branch search answers TERMINAL, the slot
            # frees exactly once.
            gitlab_native.mark_job(seeded.current_job_id, "success")
            assert (await probe(seeded.intent_key)).name == "TERMINAL"
            assert await reconcile_draining(pe_db.worker_factory(), probe) == 1
            assert await reconcile_draining(pe_db.worker_factory(), probe) == 0  # exactly once
            assert (await lease_snapshot(policy, GL_PROJECT_ID, pe_db.worker_factory()))[
                "held"
            ] == 0
            second = await try_acquire_lease(
                policy,
                GL_PROJECT_ID,
                pe_db.worker_factory(),
                run_id="f" * 31 + "5",
                provider="gitlab",
            )
            assert second is not None  # the honestly-freed slot admits again
        finally:
            await gitlab.close()


# ----------------------------------------------------------------------
# MX-3 — the mutation arms (each defect seeded, its observable asserted)
# ----------------------------------------------------------------------


class TestMX3TheMutationArms:
    """Each arm seeds ONE regression into the process that executes the
    policy (the worker or the gateway subprocess; the occupancy arm patches
    the shipped symbol in-process, where its service runs) and runs the
    SAME baseline sequence as its paired matrix arm, asserting the
    DEFECT's observable — the baseline arm's assertions that FAIL under
    the patch are named in each arm's docstring."""

    @trace_record("MX-3a the budget-before-child mutant", mutation="budget-before-child")
    async def test_budget_before_child_restored_kills_the_finite_round(
        self, pe_db, gitlab_native, tmp_path
    ):
        """The pre-#356 ordering: the child budget opened before the child
        run existed → ``open_budget``'s RunNotFound guard fired for EVERY
        finite spec. Under the patch the finite correction arm's assertions
        (ONE round, the child run, the child's OWN budget row) all fail:
        the admission transaction rolls back and NOTHING lands. The
        unlimited arm is the mutant's control — with no spec limits the
        old ordering never reached the guard (open_budget_from_spec
        returns before any DB read), which is why only the finite arms
        caught the original defect."""
        budget, backend = "finite", "builtin"
        mx = await _drive_matrix(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            leg="initial",
            redis_url=None,
            tag="mx3a",
            keep_gateway=True,
        )
        try:
            fix = await _post(mx.gateway, 3503, MX_FIX_NOTE, delivery="mx3a-fix", mr_iid=mx.mr_iid)
            assert fix is not None and fix.status_code == 202
        finally:
            mx.gateway.stop()
        _worker_run(
            tmp_path,
            pe_db,
            gitlab_native,
            budget=budget,
            backend=backend,
            name="mx3a-wk-mutant",
            mutation="budget-before-child",
        )
        # THE DEFECT'S OBSERVABLE — the admission died on the application
        # guard: no round, no child, no child budget (the transaction held
        # nothing partial, but the feature is GONE for every finite spec).
        assert await _rounds_of(pe_db) == []
        runs = await _all_runs(pe_db)
        assert [row.id for row in runs] == [mx.run_id]
        assert await _budget_of(pe_db, mx.run_id) is not None  # delivery 1's budget stands

    @trace_record(
        "MX-3b the premature-success ingress mutant on the correction path",
        mutation="pre-commit-cache",
    )
    async def test_pre_commit_cache_restored_empties_the_correction(
        self, pe_db, gitlab_native, tmp_path, redis_server
    ):
        """The pre-#357 ingress (SET-NX consulted BEFORE the transaction,
        answering ``deduplicated`` with no DB check): the /fix P02 schedule
        on the CORRECTION path — marker ok, SQL fails, the retry inside
        the TTL answers a SUCCESSFUL EMPTY DUPLICATE. The baseline
        correction arm's assertions (the round admitted, the child, the
        dispatch) all fail under the patch — the acknowledged reviewer
        request is LOST."""
        budget, backend = "finite", "builtin"
        mx = await _drive_matrix(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            leg="initial",
            redis_url=None,
            tag="mx3b",
            keep_gateway=True,
        )
        mx.gateway.stop()  # the mutation lives in a fresh gateway child
        gateway = start_mx_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            budget=budget,
            backend=backend,
            redis_url=redis_server.url,
            db_failures=1,
            mutation="pre-commit-cache",
            name="mx3b-gw-mutant",
        )
        try:
            first = await _post(gateway, 3603, MX_FIX_NOTE, delivery="mx3b-fix", mr_iid=mx.mr_iid)
            assert first is not None and first.status_code == 500  # the old outage

            retry = await _post(gateway, 3603, MX_FIX_NOTE, delivery="mx3b-fix", mr_iid=mx.mr_iid)
            assert retry is not None and retry.status_code == 202
            # THE DEFECT'S OBSERVABLE — the successful empty duplicate:
            assert retry.json()["deduplicated"] is True
        finally:
            gateway.stop()
        # ...and nothing is durable behind the acknowledgement:
        assert await _steps_of(pe_db, "review_feedback") == []
        assert await _rounds_of(pe_db) == []
        _worker_run(
            tmp_path, pe_db, gitlab_native, budget=budget, backend=backend, name="mx3b-wk-after"
        )
        assert await _rounds_of(pe_db) == []  # a worker cannot recover what never landed

    @trace_record("MX-3c the base-guard-first mutant", mutation="base-guard-first")
    async def test_base_guard_first_restored_stales_the_own_commit(
        self, pe_db, gitlab_native, tmp_path
    ):
        """The pre-#358 recovery order (the base-head fence BEFORE any
        own-effect classification): the MX-2d recovery trace fails — the
        round's OWN commit at the head reads FOREIGN, the round stales and
        the child parks blocked with its publication rights revoked BEFORE
        any re-dispatch could adopt the journaled effect (a re-raised
        correction would then build a second effect on a surface forge no
        longer owns)."""
        budget, backend = "finite", "builtin"
        mx = await _drive_matrix(
            pe_db,
            gitlab_native,
            tmp_path,
            budget=budget,
            backend=backend,
            leg="initial",
            redis_url=None,
            tag="mx3c",
            keep_gateway=True,
        )
        try:
            commits_before = len(gitlab_native.state()["branches"].get(mx.branch, []))
            fix = await _post(mx.gateway, 3703, MX_FIX_NOTE, delivery="mx3c-fix", mr_iid=mx.mr_iid)
            assert fix is not None and fix.status_code == 202
            killed = _worker_run(
                tmp_path,
                pe_db,
                gitlab_native,
                budget=budget,
                backend=backend,
                name="mx3c-wk-leg",
                kill="journal-completion",
                nth=1,
            )
            assert killed.returncode == -9
        finally:
            mx.gateway.stop()
        assert (
            len(gitlab_native.state()["branches"].get(mx.branch, [])) == commits_before + 1
        )  # the round's own commit landed

        # The mutant reconciler: the fence fired FIRST — the own effect at
        # the head is misread as foreign and the round is staled.
        _worker_run(
            tmp_path,
            pe_db,
            gitlab_native,
            budget=budget,
            backend=backend,
            name="mx3c-wk-mutant",
            mode="reconciler",
            ticks=3,
            mutation="base-guard-first",
        )
        rounds = await _rounds_of(pe_db)
        assert len(rounds) == 1
        # THE DEFECT'S OBSERVABLE — the round staled on its OWN effect:
        assert rounds[0].status == "stale", rounds[0].status
        child = await _run_of(pe_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.BLOCKED.value  # publication rights revoked
        assert "foreign_head" in (child.status_reason or "")

    @trace_record("MX-3d the any-terminal occupancy mutant", mutation="any-terminal-occupancy")
    async def test_any_terminal_occupancy_restored_oversubscribes(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        """The pre-#360 predicate: ANY terminal pipeline in the branch
        listing released the slot. The MX-2e mixed-history sequence under
        the patch: the PRIOR attempt's terminal row wins, the lease
        releases with the CURRENT job still running, and at capacity one
        the next acquire OVERSUBSCRIBES — the baseline's assertions
        (RUNNING verdict, held slot, refused second acquire) all fail."""
        from forge.adaptive.admission import NativeStatus, reconcile_draining, try_acquire_lease
        from forge.adaptive.admission import AdmissionPolicy
        from forge.config import ForgeConfig, Settings
        from forge.gitlab.client import GitLabClient
        from forge.gitlab.schemas import Pipeline
        from forge.runs.service import RunService, _CI_ACTIVE_STATUSES, forge_token

        seeded = await _seed_mixed_history_lease(pe_db, gitlab_native)
        policy = AdmissionPolicy(max_active_per_project=1)
        settings_kwargs = _mx_settings_kwargs(gitlab_native.base_url, pe_db.url)
        gitlab = GitLabClient(
            base_url=settings_kwargs["GITLAB_URL"],
            token=forge_token(Settings(**settings_kwargs)),
        )
        try:
            service = RunService(
                session_factory=pe_db.worker_factory(),
                gitlab=gitlab,
                settings=Settings(**settings_kwargs),
                config=ForgeConfig(),
            )

            # THE MUTANT — the pre-#360 predicate, patched onto the SHIPPED
            # symbol at test time (the same patch production_matrix_worker
            # seeds into the child; here the policy runs in-process):
            def any_terminal(pipelines: list[Pipeline], corr):  # noqa: ANN001
                if not pipelines:
                    return NativeStatus.TERMINAL
                statuses = {(p.status or "").lower() for p in pipelines}
                if statuses - _CI_ACTIVE_STATUSES:
                    return NativeStatus.TERMINAL
                return NativeStatus.RUNNING

            monkeypatch.setattr(RunService, "_branch_search_occupancy", staticmethod(any_terminal))
            probe = service._native_occupancy_probe()

            # THE DEFECT'S OBSERVABLE — the historical terminal row wins
            # while the CURRENT attempt is still running:
            assert (await probe(seeded.intent_key)).name == "TERMINAL"
            assert await reconcile_draining(pe_db.worker_factory(), probe) == 1  # freed early
            # OVERSUBSCRIPTION at capacity one: the second execution lease
            # is granted with the first job still running natively.
            second = await try_acquire_lease(
                policy,
                GL_PROJECT_ID,
                pe_db.worker_factory(),
                run_id="f" * 31 + "5",
                provider="gitlab",
            )
            assert second is not None, "the oversubscription did not reproduce"
        finally:
            await gitlab.close()


# ----------------------------------------------------------------------
# The occupancy mixed-history seeding (MX-2e and MX-3d share it)
# ----------------------------------------------------------------------


async def _seed_mixed_history_lease(pe_db, gitlab_native) -> SimpleNamespace:
    """A terminal run's execution lease, parked DRAINING with its native
    start intent, over a branch whose listing carries MIXED history: a
    PRIOR attempt's terminal pipeline (journaled before the intent) beside
    the CURRENT attempt's RUNNING one — everything through the production
    admission/journal APIs, the listing on the recording native server."""
    from forge.adaptive.admission import (
        AdmissionPolicy,
        record_native_start_intent,
        try_acquire_lease,
    )
    from forge.durable.models import FlowRun as FlowRunRow

    _seed_workspace(gitlab_native, None, "builtin")  # type: ignore[arg-type]
    run_id = "d" * 31 + "5"
    factory = pe_db.worker_factory()
    async with factory() as session:
        session.add(
            FlowRunRow(
                id=run_id,
                project_id=GL_PROJECT_ID,
                issue_iid=MX_ISSUE_IID,
                provider="gitlab",
                status="failed",  # terminal: the crash backstop will park the lease
            )
        )
        await session.commit()
    branch = f"factory/{MX_ISSUE_IID}/{run_id[:8]}"
    policy = AdmissionPolicy(max_active_per_project=1)
    lease = await try_acquire_lease(
        policy, GL_PROJECT_ID, pe_db.worker_factory(), run_id=run_id, provider="gitlab"
    )
    assert lease is not None

    # The PRIOR attempt's terminal pipeline, journaled as an earlier start.
    prior = gitlab_native.seed_pipeline(
        ref=branch,
        sha="1" * 40,
        status="success",
        jobs=[{"name": MX_REQUIRED_JOB, "status": "success"}],
    )
    async with factory() as session:
        controller = Controller(session)
        action = await controller.record_action(run_id, "harness_start", correlation_id=branch)
        await controller.complete_action(
            action, "succeeded", {"pipeline_id": prior["id"], "ref": branch}
        )
        await session.commit()

    # The CURRENT attempt's start intent (the lease's probe key), then its
    # RUNNING pipeline on the same branch — the mixed-history listing.
    intent_key = f"gitlab:pipeline:{GL_PROJECT_ID}@{branch}"
    await record_native_start_intent(pe_db.worker_factory(), run_id, intent_key)
    current = gitlab_native.seed_pipeline(
        ref=branch,
        sha="2" * 40,
        status="running",
        jobs=[{"name": "forge-agent-mx", "status": "running"}],
    )
    (current_job,) = gitlab_native.jobs(current["id"])

    # Park the lease DRAINING the way production does: the NEXT acquirer's
    # terminal-run reclaim (the run is terminal, the intent unobserved) —
    # and at capacity one that acquirer is honestly REFUSED.
    refused = await try_acquire_lease(
        policy, GL_PROJECT_ID, pe_db.worker_factory(), run_id="e" * 31 + "5", provider="gitlab"
    )
    assert refused is None, "the draining lease did not hold its slot"
    return SimpleNamespace(
        run_id=run_id,
        branch=branch,
        intent_key=intent_key,
        prior_id=int(prior["id"]),
        current_job_id=int(current_job["id"]),
    )


def _mx_settings_kwargs(gitlab_url: str, db_url: str) -> dict:
    from pydantic import SecretStr

    return {
        "GITLAB_URL": gitlab_url,
        "GITLAB_TOKEN": SecretStr("mx-gitlab-token"),
        "GITLAB_WEBHOOK_SECRET": SecretStr(MX_WEBHOOK_SECRET),
        "FORGE_APPROVERS": "alice",
        "DATABASE_URL": db_url,
    }
