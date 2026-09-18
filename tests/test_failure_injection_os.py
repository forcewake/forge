"""ADR-0017 §5 failure-injection exit bar, OS-process profile (review R20).

The coroutine suite (``tests/test_failure_injection.py``) kills asyncio tasks
inside ONE Python process; its results cannot transfer to SIGKILL and process
isolation. This suite is the OS evidence: every worker is a REAL
``python -m forge.worker`` subprocess — its own interpreter, its own
connection pools, its own PID — driven to each checkpoint and killed with
SIGKILL (``kill -9``: no failure recorded, no cleanup run). A fresh worker
process then adopts the durable state and must converge on exactly one
consistent outcome.

What makes a real process killable at a checkpoint: the two remote seams the
worker talks to over 127.0.0.1 are suite-controlled stubs —

- ``tests/fi_os.llm_stub.LLMStub`` — a canned LiteLLM proxy that COUNTS every
  model call per role (the cross-process "plan ran once" ledger) and can be
  armed, once, to HOLD a role's next response forever (the caller blocks
  until SIGKILLed mid-call) or DROP the connection;
- ``tests/fi_os.gitlab_stub.GitLabStub`` — a fake GitLab REST v4 server whose
  ``create_commit`` can be armed to APPLY the commit and then hold or drop
  the response — the ambiguous remote effect R11's identity probe exists
  for. Its refusal of a second open MR per source branch (real GitLab
  behaviour) turns any latent duplicate-publication bug into a loud failure.

The schema comes from the real migration chain (R22): ``python -m
forge.migrate`` runs once per session against ``FORGE_PG_TEST_URL``; each
test truncates the lab database (``alembic_version`` kept — the worker's R22
boot gate must keep seeing a database at head). Recovery uses the real
wall-clock machinery only: leases are force-expired by SQL (standing in for
the 120s lease), and the survivor's own reaper / step loop / reconciler do
the rest. Redis is required because the real worker requires it: point
``FORGE_OS_FI_REDIS_URL`` at a disposable instance.

Checkpoint matrix (coroutine suite ⇄ this suite):

- (a) after the ingress commit, before any claim    — S1  ⇄ TestCheckpointA
- (b) after the claim, before the plan persisted    — S2b ⇄ TestCheckpointB
- (c) gate consumed, proposal not yet persisted     — S2c ⇄ TestCheckpointC
- (d) commit applied, response lost, intent open    — S3  ⇄ TestCheckpointD
- (R11) ambiguous publication: adopt, never re-POST — new ⇄ TestAmbiguousCommit
- (A12) DELAYED apply: negative probe ≠ absence     — new ⇄ TestDelayedApplyCommit
- review leg crash (REVIEWING resume)               — S4f ⇄ TestCheckpointReview
- crash while waiting_ci: adopt the full publication— S3/S4 aftermath ⇄
                                                      TestCheckpointWaitingCI
- SIGTERM (graceful) across the gate                — new ⇄ TestSigtermRestart
- no-crash baseline pinning the stub budgets        — new ⇄ TestOSHappyPath

Run locally::

    FORGE_PG_TEST_URL=postgresql+asyncpg://... FORGE_OS_FI_REDIS_URL=redis://... \
        uv run pytest tests/test_failure_injection_os.py -q

CI: the ``integration-os`` job (nightly / manual dispatch) — deliberately off
the PR gate: real process spawns, a 30s reaper tick and 15s reconciler ticks
put the suite in the ~10 minute range.

Skipped entirely unless BOTH ``FORGE_PG_TEST_URL`` (whose database rows are
truncated per test — point it at a disposable lab DB) and
``FORGE_OS_FI_REDIS_URL`` are set.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import (
    ActionLog,
    FlowRun,
    FlowStatus,
    GateApproval,
    PublicationIntent,
    StepRun,
    factory_branch,
    ingest_event,
)
from forge.runs.checkpoints import checkpoint_step_name
from forge.worker.steps import command_source_event_id, schedule_command_step
from tests.fi_os.gitlab_stub import GitLabStub, base_sha
from tests.fi_os.lab import run_migrations, truncate_all
from tests.fi_os.llm_stub import LLMStub
from tests.fi_os.worker import OSWorker, worker_env

if not os.environ.get("FORGE_PG_TEST_URL"):
    pytest.skip(
        "FORGE_PG_TEST_URL not set — the OS-process failure-injection profile "
        "runs only against a disposable real Postgres (its tables are "
        "truncated; the schema is migrated by python -m forge.migrate)",
        allow_module_level=True,
    )
if not os.environ.get("FORGE_OS_FI_REDIS_URL"):
    pytest.skip(
        "FORGE_OS_FI_REDIS_URL not set — the real `python -m forge.worker` "
        "requires Redis (queue loop + heartbeat); point this at a disposable "
        "Redis instance (CI: the integration-os job's redis service)",
        allow_module_level=True,
    )

PG_TEST_URL = os.environ["FORGE_PG_TEST_URL"]
REDIS_TEST_URL = os.environ["FORGE_OS_FI_REDIS_URL"]

PROJECT_ID = 4242
ISSUE_IID = 7
BASE = base_sha()

TEST_SECRET = "os-fi-webhook-secret"  # noqa: S105 — fake value for tests

READY = FlowStatus.READY_FOR_HUMAN.value
WAITING_APPROVAL = FlowStatus.WAITING_APPROVAL.value
WAITING_CI = FlowStatus.WAITING_CI.value
PROPOSING = FlowStatus.PROPOSING.value
COMMITTING = FlowStatus.COMMITTING.value
REVIEWING = FlowStatus.REVIEWING.value
PREFLIGHT = FlowStatus.PREFLIGHT.value

TERMINAL_STEP_STATUSES = {"succeeded", "dead", "cancelled"}

#: Generous by design: worker interpreter startup (~2s), a 5s step-poll, a
#: 30s reaper tick and a 15s reconciler tick are all real production cadences
#: this suite deliberately does NOT shortcut.
WAIT_WORKER = 120.0
WAIT_CONVERGE = 180.0


# ----------------------------------------------------------------------
# Fixtures: migrated lab DB + local stub servers + worker launcher
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated_db() -> None:
    """R22: the lab schema comes from the shipped migration chain, once."""
    run_migrations(PG_TEST_URL)


class OSLab:
    """One test's world: fresh DB rows, local GitLab + LLM stubs, workers."""

    def __init__(
        self, control: async_sessionmaker, gitlab: GitLabStub, llm: LLMStub, log_dir
    ) -> None:
        self.control = control
        self.gitlab = gitlab
        self.llm = llm
        self.log_dir = log_dir
        self.workers: list[OSWorker] = []
        self._env = worker_env(
            database_url=PG_TEST_URL,
            redis_url=REDIS_TEST_URL,
            gitlab_url=gitlab.url,
            llm_url=llm.url,
        )

    def spawn(self, name: str) -> OSWorker:
        worker = OSWorker(name, self._env, self.log_dir)
        worker.start()
        self.workers.append(worker)
        return worker

    async def kill_leftovers(self) -> None:
        for worker in self.workers:
            if worker.alive:
                worker.sigkill()


@pytest.fixture()
async def lab(tmp_path, _migrated_db):
    """Fresh durable state + fresh stub servers per test."""
    engine = create_async_engine(PG_TEST_URL)
    await truncate_all(engine)
    control = async_sessionmaker(engine, expire_on_commit=False)
    gitlab = GitLabStub(project_id=PROJECT_ID)
    llm = LLMStub()
    gitlab.start()
    llm.start()
    harness = OSLab(control, gitlab, llm, tmp_path)
    try:
        yield harness
    finally:
        await harness.kill_leftovers()
        gitlab.stop()
        llm.stop()
        await engine.dispose()


# ----------------------------------------------------------------------
# Control-plane helpers (the test is the "gateway" + assertion process)
# ----------------------------------------------------------------------


async def eventually(predicate, *, description: str, timeout: float, lab: OSLab, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        outcome = predicate()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return outcome
        if time.monotonic() >= deadline:
            logs = "\n".join(
                f"--- worker {w.name} (pid {w.pid}, log {w.log_path}):\n{w.tail()}"
                for w in lab.workers
            )
            raise AssertionError(f"timed out after {timeout}s waiting for {description}\n{logs}")
        await asyncio.sleep(interval)


async def ingress(lab: OSLab, command: dict, note_id: int, *, due_in: float = 0.0) -> None:
    """Persist inbox row + scheduled step in ONE commit (the ``202`` contract).

    The gateway is out of scope here (this suite proves the WORKER half of
    ADR-0017), so the test writes the ingress transaction directly, with a
    controllable ``due_at`` — a future due date is checkpoint (a)'s kill
    window: committed, but not yet claimable.
    """
    source_event_id = command_source_event_id(command["command"], command["project_id"], note_id)
    due_at = datetime.now(timezone.utc) + timedelta(seconds=due_in)
    async with lab.control() as session:
        async with session.begin():
            _, created = await ingest_event(
                session,
                source_event_id=source_event_id,
                project_id=command["project_id"],
                event_type="run_command",
                payload={**command, "note_id": note_id},
            )
            assert created, f"note {note_id} already ingested"
            await schedule_command_step(
                session, command, source_event_id=source_event_id, due_at=due_at
            )


def implement_command() -> dict:
    return {
        "command": "start_run",
        "project_id": PROJECT_ID,
        "issue_iid": ISSUE_IID,
        "author_username": "alice",
    }


def go_command(run_id: str) -> dict:
    return {
        "command": "go",
        "project_id": PROJECT_ID,
        "issue_iid": ISSUE_IID,
        "author_username": "alice",
        "author_user_id": 11,
        "note_text": f"@forge /go {run_id}",
    }


async def run_status(lab: OSLab, run_id: str) -> str | None:
    async with lab.control() as session:
        run = await session.get(FlowRun, run_id)
        return run.status if run is not None else None


async def first_run_id(lab: OSLab) -> str | None:
    async with lab.control() as session:
        return await session.scalar(select(FlowRun.id).order_by(FlowRun.created_at).limit(1))


async def get_run(lab: OSLab, run_id: str) -> FlowRun:
    async with lab.control() as session:
        return await session.get(FlowRun, run_id)


async def wait_status(lab: OSLab, run_id: str, *statuses: str, timeout: float = WAIT_WORKER):
    async def reached() -> bool:
        return await run_status(lab, run_id) in statuses

    return await eventually(
        reached,
        description=f"run {run_id[:8]} to reach {statuses}",
        timeout=timeout,
        lab=lab,
    )


async def all_steps(lab: OSLab) -> list[StepRun]:
    async with lab.control() as session:
        return list((await session.execute(select(StepRun))).scalars().all())


async def command_steps(lab: OSLab) -> list[StepRun]:
    """The schedulable command steps — R07 ``checkpoint:`` result rows are
    ALSO StepRun rows (born ``succeeded``), so generic ``steps[0]`` indexing
    would race whichever row Postgres lists first."""
    async with lab.control() as session:
        return list(
            (
                await session.execute(
                    select(StepRun)
                    .where(StepRun.step_name.not_like("checkpoint:%"))
                    .order_by(StepRun.id)
                )
            )
            .scalars()
            .all()
        )


async def wait_steps_settled(lab: OSLab, *, at_least: int = 2) -> list[StepRun]:
    async def settled() -> list[StepRun] | None:
        rows = await all_steps(lab)
        if len(rows) >= at_least and all(r.status in TERMINAL_STEP_STATUSES for r in rows):
            return rows
        return None

    return await eventually(
        settled, description="all steps to settle", timeout=WAIT_CONVERGE, lab=lab
    )


async def gate_row(lab: OSLab, run_id: str) -> GateApproval | None:
    async with lab.control() as session:
        return (
            (await session.execute(select(GateApproval).where(GateApproval.flow_run_id == run_id)))
            .scalars()
            .first()
        )


async def succeeded_actions(lab: OSLab, run_id: str, kind: str) -> list[ActionLog]:
    async with lab.control() as session:
        return list(
            (
                await session.execute(
                    select(ActionLog).where(
                        ActionLog.flow_run_id == run_id,
                        ActionLog.action_kind == kind,
                        ActionLog.status == "succeeded",
                    )
                )
            )
            .scalars()
            .all()
        )


async def plan_checkpoints(lab: OSLab, run_id: str) -> list[StepRun]:
    async with lab.control() as session:
        return list(
            (
                await session.execute(
                    select(StepRun).where(
                        StepRun.flow_run_id == run_id,
                        StepRun.step_name == checkpoint_step_name("plan"),
                    )
                )
            )
            .scalars()
            .all()
        )


async def intents_of(lab: OSLab, run_id: str) -> list[PublicationIntent]:
    async with lab.control() as session:
        return list(
            (
                await session.execute(
                    select(PublicationIntent).where(PublicationIntent.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )


async def expire_running_leases(lab: OSLab) -> None:
    """Force-expire live leases — stands in for the 120s lease wall clock."""
    async with lab.control() as session:
        async with session.begin():
            await session.execute(
                text(
                    "update step_runs set lease_expires_at = now() - interval '1 second' "
                    "where status = 'running'"
                )
            )


async def make_scheduled_steps_due(lab: OSLab) -> None:
    """Collapse the retry backoff — the same wall-clock stand-in as above."""
    async with lab.control() as session:
        async with session.begin():
            await session.execute(
                text("update step_runs set due_at = now() where status = 'scheduled'")
            )


async def candidate_sha(lab: OSLab, run_id: str) -> str:
    async def published() -> str | None:
        run = await get_run(lab, run_id)
        shas = list(run.candidate_shas or [])
        return shas[-1] if shas else None

    return await eventually(
        published, description="the run to record a candidate sha", timeout=WAIT_CONVERGE, lab=lab
    )


def set_ci_green(lab: OSLab, run_id: str, sha: str) -> None:
    """The external world: the target project's CI finishing green."""
    lab.gitlab.set_pipeline_success(sha, factory_branch(ISSUE_IID, run_id))


async def drive_to_ready(lab: OSLab, run_id: str, sha: str) -> None:
    set_ci_green(lab, run_id, sha)
    await wait_status(lab, run_id, READY, timeout=WAIT_CONVERGE)
    await eventually(
        lambda: lab.gitlab.notes_with("ready for human review"),
        description="the evidence note",
        timeout=WAIT_CONVERGE,
        lab=lab,
    )


def sigkill(worker: OSWorker) -> None:
    """Hard process death, with the acceptance evidence on the log."""
    assert worker.alive, f"worker {worker.name} already dead (rc={worker.exit_code})"
    worker.sigkill()
    print(
        f"[os-fi] SIGKILLed worker '{worker.name}' pid={worker.pid} "
        f"(lease owner {worker.owner_id}-...)",
        flush=True,
    )


async def start_run_leg(lab: OSLab, *, note_id: int, due_in: float = 0.0) -> str:
    """Ingress ``/implement``; returns once a run exists (not yet at gate)."""
    await ingress(lab, implement_command(), note_id, due_in=due_in)
    return await eventually(
        lambda: first_run_id(lab),
        description="the run row to exist",
        timeout=WAIT_WORKER,
        lab=lab,
    )


def assert_single_publication(
    lab: OSLab, *, commits: int = 1, mrs: int = 1, plan_notes: int = 1, evidence_notes: int = 1
) -> None:
    counts = lab.gitlab.snapshot_counts()
    assert counts["commit_posts"] == commits, counts
    assert counts["mrs_created"] == mrs, counts
    assert len(lab.gitlab.notes_with("Forge plan")) == plan_notes, counts
    assert len(lab.gitlab.notes_with("ready for human review")) == evidence_notes, counts


def llm_counts(lab: OSLab) -> dict[str, dict[str, int]]:
    return lab.llm.snapshot()


# ----------------------------------------------------------------------
# Baseline: a worker completes a full run against the stubs
# ----------------------------------------------------------------------


class TestOSHappyPath:
    async def test_one_worker_completes_implement_to_ready(self, lab: OSLab):
        """The stub budgets every kill test asserts against: one plan, one
        proposal, one review, one commit, one MR, two notes — all delivered
        by a single real worker process."""
        lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=100)
        await wait_status(lab, run_id, WAITING_APPROVAL)
        gate = await gate_row(lab, run_id)
        assert gate is not None and gate.consumed_at is None

        await ingress(lab, go_command(run_id), note_id=101)
        await wait_status(lab, run_id, WAITING_CI)
        sha = await candidate_sha(lab, run_id)
        await drive_to_ready(lab, run_id, sha)
        rows = await wait_steps_settled(lab)
        assert all(step.status == "succeeded" for step in rows)

        counts = llm_counts(lab)
        assert counts["served"]["planner"] == 1
        assert counts["served"]["implementer"] == 1
        assert counts["served"]["reviewer"] == 1
        assert_single_publication(lab)
        run = await get_run(lab, run_id)
        assert run.candidate_shas == [sha]
        assert run.mr_iid is not None


# ----------------------------------------------------------------------
# Checkpoint (a) — after the ingress commit, before the claim
# ----------------------------------------------------------------------


class TestCheckpointA:
    async def test_sigkill_between_ingress_and_claim_survivor_claims_the_orphan(self, lab: OSLab):
        """The ``202`` contract across process death: the step row committed
        with a FUTURE due date, so the victim provably never claimed it (no
        run row can even exist yet) — the fresh worker must adopt the orphan
        step (attempt 0, first fence) and walk the run to the gate."""
        victim = lab.spawn("worker-1")
        await ingress(lab, implement_command(), note_id=200, due_in=120.0)

        # Two poll intervals with the step deliberately not due: the victim
        # polled and could not claim — the kill lands in the pre-claim window.
        await asyncio.sleep(11)
        steps = await command_steps(lab)
        assert len(steps) == 1
        assert steps[0].status == "scheduled"
        assert steps[0].attempt == 0
        assert steps[0].fence_token == 0
        assert steps[0].lease_owner is None
        sigkill(victim)

        await make_scheduled_steps_due(lab)
        lab.spawn("worker-2")
        run_id = await eventually(
            lambda: first_run_id(lab),
            description="the survivor's claim to start the run",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        await wait_status(lab, run_id, WAITING_APPROVAL, timeout=WAIT_CONVERGE)
        steps = await command_steps(lab)
        assert steps[0].status == "succeeded"
        assert steps[0].attempt == 0, "first-ever claim — no reaper bump"
        assert steps[0].fence_token == 1

        counts = llm_counts(lab)
        assert counts["served"]["planner"] == 1, "the plan ran exactly once"
        await ingress(lab, go_command(run_id), note_id=201)
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)
        await drive_to_ready(lab, run_id, await candidate_sha(lab, run_id))
        await wait_steps_settled(lab)
        assert_single_publication(lab)


# ----------------------------------------------------------------------
# Checkpoint (b) — after the claim, before the plan persisted
# ----------------------------------------------------------------------


class TestCheckpointB:
    async def test_sigkill_mid_plan_recovers_with_one_delivered_plan(self, lab: OSLab):
        """SIGKILL while the planner call is in flight (the stub holds the
        response): the step stays running + leased by the dead PID with the
        plan checkpoint unwritten. The reaper reschedules, the fresh worker
        re-claims (new fence, attempt bump) and plans exactly once."""
        llm = lab.llm
        llm.arm("planner", "hold")
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=300)
        await eventually(
            lambda: llm.snapshot()["held"]["planner"] == 1,
            description="the planner call to arrive at the stub and hold",
            timeout=WAIT_WORKER,
            lab=lab,
        )
        sigkill(victim)

        steps = await command_steps(lab)
        assert steps[0].status == "running", "the hard kill recorded no failure"
        assert steps[0].lease_owner is not None
        assert steps[0].lease_owner.startswith(victim.owner_id)
        assert steps[0].fence_token == 1
        assert steps[0].attempt == 0
        run = await get_run(lab, run_id)
        assert run.status == PREFLIGHT, "the run row exists, the plan does not"
        assert await plan_checkpoints(lab, run_id) == []
        assert llm_counts(lab)["served"]["planner"] == 0, "no plan was ever delivered"

        await expire_running_leases(lab)
        lab.spawn("worker-2")
        await wait_status(lab, run_id, WAITING_APPROVAL, timeout=WAIT_CONVERGE)
        steps = await command_steps(lab)
        assert steps[0].attempt == 1, "the reaper bumped the attempt"
        assert steps[0].fence_token == 2, "the re-claim granted a fresh fence"
        assert len(await plan_checkpoints(lab, run_id)) == 1, "one plan, persisted once"
        counts = llm_counts(lab)
        assert counts["requests"]["planner"] == 2, "victim + survivor both called"
        assert counts["served"]["planner"] == 1, "exactly one plan was ever delivered"
        assert len(lab.gitlab.notes_with("Forge plan")) == 1
        print(
            f"[os-fi] recovered checkpoint (b): killed pid={victim.pid}, "
            f"step attempt={steps[0].attempt} fence={steps[0].fence_token}",
            flush=True,
        )

        await ingress(lab, go_command(run_id), note_id=301)
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)
        await drive_to_ready(lab, run_id, await candidate_sha(lab, run_id))
        await wait_steps_settled(lab)
        assert_single_publication(lab)


# ----------------------------------------------------------------------
# Checkpoint (c) — gate consumed, proposal not persisted
# ----------------------------------------------------------------------


class TestCheckpointC:
    async def test_sigkill_mid_proposal_replays_plan_recomputes_proposal(self, lab: OSLab):
        """The gate is consumed and the run committed to ``proposing``, then
        the process dies mid-proposal-call. Recovery: the persisted plan is
        REPLAYED (no second planner call), the proposal legitimately
        recomputes (nothing was persisted), and there is no double commit."""
        llm = lab.llm
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=400)
        await wait_status(lab, run_id, WAITING_APPROVAL)
        assert llm_counts(lab)["served"]["planner"] == 1

        llm.arm("implementer", "hold")
        await ingress(lab, go_command(run_id), note_id=401)
        await eventually(
            lambda: llm.snapshot()["held"]["implementer"] == 1,
            description="the proposal call to arrive at the stub and hold",
            timeout=WAIT_WORKER,
            lab=lab,
        )
        gate = await gate_row(lab, run_id)
        assert gate is not None and gate.consumed_at is not None, "gate consumed once"
        assert await run_status(lab, run_id) == PROPOSING
        assert len(await plan_checkpoints(lab, run_id)) == 1
        sigkill(victim)

        assert llm_counts(lab)["served"]["implementer"] == 0, "no proposal ever delivered"
        assert await succeeded_actions(lab, run_id, "commit") == []
        assert await intents_of(lab, run_id) == [], "no publication was even attempted"

        await expire_running_leases(lab)
        lab.spawn("worker-2")
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)
        counts = llm_counts(lab)
        assert counts["served"]["planner"] == 1, "the persisted plan was replayed"
        assert counts["requests"]["implementer"] == 2, "victim + survivor proposed"
        assert counts["served"]["implementer"] == 1, "one proposal delivered"
        await drive_to_ready(lab, run_id, await candidate_sha(lab, run_id))
        await wait_steps_settled(lab)
        assert_single_publication(lab)
        assert await run_status(lab, run_id) == READY


# ----------------------------------------------------------------------
# Checkpoint (d) — commit applied, response held, intent left open
# ----------------------------------------------------------------------


class TestCheckpointD:
    async def test_sigkill_after_commit_landed_open_intent_is_adopted(self, lab: OSLab):
        """The stub APPLIES the commit then holds the response; the worker is
        SIGKILLed waiting. The R11 intent is left ``dispatched`` — recovery
        must PROBE by identity and adopt the landed commit, never re-POST."""
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=500)
        await wait_status(lab, run_id, WAITING_APPROVAL)

        lab.gitlab.arm_commit_effect("hold")
        await ingress(lab, go_command(run_id), note_id=501)
        await eventually(
            lambda: lab.gitlab.commit_holds == 1,
            description="the commit POST to land and hold",
            timeout=WAIT_WORKER,
            lab=lab,
        )
        sigkill(victim)

        branch = factory_branch(ISSUE_IID, run_id)
        forge_commits = lab.gitlab.forge_commits_on(branch)
        assert len(forge_commits) == 1, "the killed attempt landed exactly one commit"
        sha = forge_commits[0]["id"]
        intents = await intents_of(lab, run_id)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.status == "dispatched", "the crash left the intent open"
        assert intent.operation_key in forge_commits[0]["message"], (
            "the commit carries the intent's (forge-op:) marker"
        )
        assert intent.expected_parent_oid == BASE
        assert await succeeded_actions(lab, run_id, "commit") == [], (
            "the journal never caught up — the commit is journaled-unknown"
        )
        run = await get_run(lab, run_id)
        assert run.status == COMMITTING
        assert list(run.candidate_shas or []) == []

        await expire_running_leases(lab)
        lab.spawn("worker-2")
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)

        async def intent_adopted() -> bool:
            return await _intent_status(lab, run_id) == "adopted"

        await eventually(
            intent_adopted,
            description="the open intent to be resolved by identity probe (adopted)",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        adopted_intent = (await intents_of(lab, run_id))[0]
        print(
            f"[os-fi] adopted operation_key={adopted_intent.operation_key} "
            f"sha={adopted_intent.provider_object_id} after killing pid={victim.pid}",
            flush=True,
        )
        assert adopted_intent.provider_object_id == sha, "the ADOPTED commit is the landed one"
        run = await get_run(lab, run_id)
        assert list(run.candidate_shas or []) == [sha], "the adopted commit is THE candidate"
        assert lab.gitlab.snapshot_counts()["commit_posts"] == 1, (
            "recovery probed and adopted — never a second POST"
        )
        assert lab.gitlab.mrs_created == 1

        await drive_to_ready(lab, run_id, sha)
        await wait_steps_settled(lab)
        assert_single_publication(lab)
        assert await run_status(lab, run_id) == READY


async def _intent_status(lab: OSLab, run_id: str) -> str | None:
    intents = await intents_of(lab, run_id)
    return intents[0].status if intents else None


# ----------------------------------------------------------------------
# A12 — DELAYED apply: accepted, applied after the negative probe
# ----------------------------------------------------------------------


class TestDelayedApplyCommit:
    async def test_delayed_apply_negative_probe_settles_then_adopts_never_duplicates(
        self, lab: OSLab
    ):
        """The A12 hole: the stub ACCEPTS the publication POST but DELAYS the
        commit's application past the recovery's negative probe. A recovery
        that treated ``zero marker hits + unchanged head`` as safe-to-
        redispatch would POST a duplicate and BOTH would land. Instead the
        negative probe parks the intent in the effect-certainty window
        (``probing``), the late-landing commit is ADOPTED by the window-end
        re-probe, and the run converges on exactly one candidate."""
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=650)
        await wait_status(lab, run_id, WAITING_APPROVAL)

        lab.gitlab.arm_commit_delay(45.0)
        lab.gitlab.arm_commit_effect("hold")
        await ingress(lab, go_command(run_id), note_id=651)
        await eventually(
            lambda: lab.gitlab.commit_holds == 1,
            description="the commit POST to be accepted (application delayed) and hold",
            timeout=WAIT_WORKER,
            lab=lab,
        )
        sigkill(victim)

        branch = factory_branch(ISSUE_IID, run_id)
        counts = lab.gitlab.snapshot_counts()
        assert counts["commit_posts"] == 1, "exactly one write was accepted"
        assert counts["commits_applied"] == 0, "the accepted effect is NOT applied yet"
        assert lab.gitlab.delayed_apply_pending == 1
        assert lab.gitlab.forge_commits_on(branch) == [], (
            "every branch read still shows the old head — the negative-probe view"
        )
        intents = await intents_of(lab, run_id)
        assert len(intents) == 1 and intents[0].status == "dispatched"
        run = await get_run(lab, run_id)
        assert run.status == COMMITTING
        assert list(run.candidate_shas or []) == []

        # Recovery: a fresh worker probes the STALE head. The negative read
        # must park the intent in the certainty window — never re-POST.
        await expire_running_leases(lab)
        lab.spawn("worker-2")

        async def intent_settling() -> bool:
            return await _intent_status(lab, run_id) == "probing"

        await eventually(
            intent_settling,
            description="the negative probe to park the intent in the A12 certainty window",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        assert lab.gitlab.snapshot_counts()["commit_posts"] == 1, (
            "the certainty window forbids a re-dispatch off one negative read"
        )

        # The provider's slow application completes AFTER the negative probe.
        await eventually(
            lambda: lab.gitlab.delayed_apply_pending == 0,
            description="the delayed commit application to complete",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        forge_commits = lab.gitlab.forge_commits_on(branch)
        assert len(forge_commits) == 1, "exactly ONE logical candidate exists"
        sha = forge_commits[0]["id"]

        # The window-end re-probe ADOPTS the late-landing commit: the run
        # advances with no second write and a consistent history.
        await eventually(
            lambda: _intent_status(lab, run_id) == "adopted",
            description="the certainty-window re-probe to adopt the late-landing commit",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        adopted = (await intents_of(lab, run_id))[0]
        assert adopted.provider_object_id == sha, "the adopted commit is THE landed one"
        assert lab.gitlab.snapshot_counts()["commit_posts"] == 1, (
            "recovery adopted — never a second POST"
        )
        assert lab.gitlab.mrs_created == 1
        run = await get_run(lab, run_id)
        assert list(run.candidate_shas or []) == [sha]

        await drive_to_ready(lab, run_id, sha)
        await wait_steps_settled(lab)
        assert_single_publication(lab)
        assert await run_status(lab, run_id) == READY


# ----------------------------------------------------------------------
# R11 — ambiguous publication: accept the write, drop the response
# ----------------------------------------------------------------------


class TestAmbiguousCommit:
    async def test_dropped_commit_response_resolves_by_identity_not_repost(self, lab: OSLab):
        """The stub accepts the publication POST, applies the commit, and
        drops the connection before responding. The step fails with a retry;
        the worker is SIGKILLed on top. The recovery must resolve by identity
        (one probe, adopt) — ONE write total."""
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=600)
        await wait_status(lab, run_id, WAITING_APPROVAL)

        lab.gitlab.arm_commit_effect("drop")
        await ingress(lab, go_command(run_id), note_id=601)
        await eventually(
            lambda: lab.gitlab.commit_drops == 1,
            description="the commit POST to be applied and dropped",
            timeout=WAIT_WORKER,
            lab=lab,
        )
        sigkill(victim)

        branch = factory_branch(ISSUE_IID, run_id)
        counts = lab.gitlab.snapshot_counts()
        assert counts["commit_posts"] == 1, "ONE write total, before recovery"
        assert counts["commits_applied"] == 1, "and it landed"
        forge_commits = lab.gitlab.forge_commits_on(branch)
        assert len(forge_commits) == 1
        sha = forge_commits[0]["id"]

        # The drop raced the victim's fail_step: cover both left-behind shapes
        # (running-leased, or rescheduled by the failure path).
        await expire_running_leases(lab)
        await make_scheduled_steps_due(lab)
        lab.spawn("worker-2")
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)

        counts = lab.gitlab.snapshot_counts()
        assert counts["commit_posts"] == 1, "recovery probed by identity — never re-POSTed"
        assert lab.gitlab.forge_commits_on(branch) == forge_commits, (
            "still exactly ONE forge commit on the branch"
        )
        assert await _intent_status(lab, run_id) == "adopted"
        run = await get_run(lab, run_id)
        assert list(run.candidate_shas or []) == [sha]

        await drive_to_ready(lab, run_id, sha)
        await wait_steps_settled(lab)
        assert_single_publication(lab)
        assert await run_status(lab, run_id) == READY


# ----------------------------------------------------------------------
# Review leg — SIGKILL mid-review, the REVIEWING resume converges
# ----------------------------------------------------------------------


class TestCheckpointReview:
    async def test_sigkill_mid_review_resumes_to_exactly_one_review_and_note(self, lab: OSLab):
        """CI is green; the victim's reconciler moved the run to REVIEWING
        and is killed mid-review-call. The fresh worker's reconciler resumes
        the stranded leg: the review runs once, the run reaches ready and the
        evidence note is posted exactly once."""
        llm = lab.llm
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=700)
        await wait_status(lab, run_id, WAITING_APPROVAL)
        await ingress(lab, go_command(run_id), note_id=701)
        sha = await candidate_sha(lab, run_id)

        llm.arm("reviewer", "hold")
        set_ci_green(lab, run_id, sha)

        async def reviewing_with_held_call() -> bool:
            return (
                llm.snapshot()["held"]["reviewer"] == 1
                and await run_status(lab, run_id) == REVIEWING
            )

        await eventually(
            reviewing_with_held_call,
            description="the run to reach REVIEWING with the review call held",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        sigkill(victim)

        assert await succeeded_actions(lab, run_id, "post_evidence_note") == []
        assert lab.gitlab.notes_with("ready for human review") == []

        lab.spawn("worker-2")
        await wait_status(lab, run_id, READY, timeout=WAIT_CONVERGE)
        counts = llm_counts(lab)
        assert counts["requests"]["reviewer"] == 2, "victim + survivor reviewed"
        assert counts["served"]["reviewer"] == 1, "exactly one review was ever delivered"
        assert counts["served"]["planner"] == 1 and counts["served"]["implementer"] == 1
        await eventually(
            lambda: len(lab.gitlab.notes_with("ready for human review")) == 1,
            description="the single evidence note",
            timeout=WAIT_CONVERGE,
            lab=lab,
        )
        assert_single_publication(lab)
        assert await run_status(lab, run_id) == READY


# ----------------------------------------------------------------------
# Post-publication — SIGKILL while waiting_ci, everything adopted
# ----------------------------------------------------------------------


class TestCheckpointWaitingCI:
    async def test_sigkill_while_waiting_ci_new_worker_adopts_without_new_writes(self, lab: OSLab):
        """The victim fully published (commit journaled, MR recorded, run at
        waiting_ci) and is killed before CI. The fresh worker adopts every
        journaled effect and finishes the run with ZERO new remote writes and
        ZERO new model calls beyond the review."""
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=800)
        await wait_status(lab, run_id, WAITING_APPROVAL)
        await ingress(lab, go_command(run_id), note_id=801)
        sha = await candidate_sha(lab, run_id)
        run = await get_run(lab, run_id)
        mr_iid = run.mr_iid
        assert mr_iid is not None
        sigkill(victim)

        counts = llm_counts(lab)
        assert counts["served"]["planner"] == 1
        assert counts["served"]["implementer"] == 1
        assert counts["served"]["reviewer"] == 0, "the review never started"
        published = lab.gitlab.snapshot_counts()
        assert published["commit_posts"] == 1 and published["mrs_created"] == 1

        lab.spawn("worker-2")
        await drive_to_ready(lab, run_id, sha)
        rows = await wait_steps_settled(lab)
        assert all(step.status == "succeeded" for step in rows)

        counts = llm_counts(lab)
        assert counts["served"]["reviewer"] == 1, "only the review was still owed"
        assert lab.gitlab.snapshot_counts()["commit_posts"] == 1
        assert lab.gitlab.snapshot_counts()["mrs_created"] == 1, (
            "the journaled MR was adopted — never a second one"
        )
        run = await get_run(lab, run_id)
        assert run.mr_iid == mr_iid and run.candidate_shas == [sha]
        assert len(await succeeded_actions(lab, run_id, "commit")) == 1
        assert await run_status(lab, run_id) == READY


# ----------------------------------------------------------------------
# SIGTERM — the graceful restart across the human gate
# ----------------------------------------------------------------------


class TestSigtermRestart:
    async def test_sigterm_at_the_gate_then_a_fresh_worker_finishes_the_run(self, lab: OSLab):
        """The kill switch has two positions. SIGTERM'd at the gate (nothing
        in flight) the worker exits CLEANLY; a fresh process consumes /go and
        the run converges with the usual exactly-once ledger."""
        victim = lab.spawn("worker-1")
        run_id = await start_run_leg(lab, note_id=900)
        await wait_status(lab, run_id, WAITING_APPROVAL)
        assert llm_counts(lab)["served"]["planner"] == 1

        exit_code = victim.sigterm()
        assert exit_code == 0, f"the worker exited rc={exit_code} on SIGTERM\n{victim.tail()}"
        print(f"[os-fi] SIGTERM'd worker '{victim.name}' pid={victim.pid}: clean exit", flush=True)

        lab.spawn("worker-2")
        await ingress(lab, go_command(run_id), note_id=901)
        await wait_status(lab, run_id, WAITING_CI, timeout=WAIT_CONVERGE)
        counts = llm_counts(lab)
        assert counts["served"]["planner"] == 1, "no re-planning across restart"
        await drive_to_ready(lab, run_id, await candidate_sha(lab, run_id))
        await wait_steps_settled(lab)
        assert_single_publication(lab)
        counts = llm_counts(lab)
        assert counts["served"]["implementer"] == 1
        assert counts["served"]["reviewer"] == 1
        assert await run_status(lab, run_id) == READY
