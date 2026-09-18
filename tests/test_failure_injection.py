"""ADR-0017 §5 failure-injection exit bar: TWO worker loops on REAL Postgres.

The fast SQLite profile (``tests/test_step_runtime.py``) proves the mechanics;
this suite is the proof the exit bar asks for: a durable runtime where a
worker process dies hard at every checkpoint and the survivor converges on
exactly one consistent outcome — against real Postgres row locks, real
``SKIP LOCKED`` claiming, real partial unique indexes and separate connection
pools per worker.

Checkpoints (a kill lands between each pair of adjacent effects):

- (a) after the ingress commit (inbox + first step persisted), before claim
- (b) after the step claim (running, leased), before any handler effect
- (c) after gate consumption (``waiting_approval → proposing`` committed),
      after the proposal was computed but before any effect persisted
- (d) after ``writer.apply`` succeeded (commit landed, journal complete),
      before ``candidate_shas`` / the step completion persisted
- (e) after Draft-MR creation, before the run state caught up
- (f) after the ``ready_for_human`` transition, before the evidence note

The GitLab surface and the factory agents are faked at the RunService seams
(``FakeGitLab`` + stub agents, exactly like the unit profile); everything
durable — inbox, steps, runs, gates, outbox, action log — runs on Postgres.

Skipped entirely unless ``FORGE_PG_TEST_URL`` is set: the URL's database is
DROPPED and recreated per test, so only point it at a disposable lab DB.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.config import ForgeConfig, Settings
from forge.durable import (
    ActionLog,
    EventInbox,
    FlowRun,
    FlowStatus,
    GateApproval,
    StepRun,
    factory_branch,
    ingest_event,
)
from forge.durable.models import Base
from forge.repository.writer import ChangesetWriter
from forge.runs import RunService
from forge.runs.reconciler import run_reconciler
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from forge.worker import steps as step_runtime
from tests.fi_hooks import CrashAfterPropose, CrashInjector
from tests.fixtures.fake_gitlab import FakeGitLab, FakeGitLabClientFactory

if not os.environ.get("FORGE_PG_TEST_URL"):
    pytest.skip(
        "FORGE_PG_TEST_URL not set — the ADR-0017 failure-injection exit bar "
        "runs only against a disposable real Postgres (its schema is dropped)",
        allow_module_level=True,
    )

PG_TEST_URL = os.environ["FORGE_PG_TEST_URL"]

TEST_SECRET = "test-secret-token"  # noqa: S105 — fake value for tests
PROJECT_ID = 4242
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Make widgets real."
BASE_SHA = "base-sha-1"
VICTIM = "worker-1"  # every kill lands in this process; the other one survives
TERMINAL_STEP_STATUSES = {"succeeded", "dead", "cancelled"}
READY = FlowStatus.READY_FOR_HUMAN.value
WAITING_APPROVAL = FlowStatus.WAITING_APPROVAL.value
WAITING_CI = FlowStatus.WAITING_CI.value


def make_settings() -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL=PG_TEST_URL,
        LITELLM_URL="http://litellm:4000",
        # Pin the environment explicitly — a developer .env must not leak in.
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
    )
    return Settings(**values)


class CountingPlanner(StubPlanner):
    """Stub planner that counts model calls — the "plan ran once" ledger."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def plan(self, *args, **kwargs):
        self.calls += 1
        return await super().plan(*args, **kwargs)


class CountingImplementer(StubImplementer):
    """Stub implementer that counts model calls — recompute detection."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def propose(self, *args, **kwargs):
        self.calls += 1
        return await super().propose(*args, **kwargs)


class PostgresLab:
    """The disposable lab database: drop everything, recreate, hand out pools.

    Every worker gets its OWN engine (own asyncpg pool) — the exit bar is two
    processes, and two tasks sharing one pool would prove nothing about
    concurrent Postgres sessions.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._engines: list[object] = []

    async def reset(self) -> None:
        engine = create_async_engine(self._url)
        try:
            async with engine.begin() as conn:
                names = (
                    (
                        await conn.execute(
                            text("select tablename from pg_tables where schemaname = 'public'")
                        )
                    )
                    .scalars()
                    .all()
                )
                for name in names:
                    await conn.execute(text(f'drop table if exists "{name}" cascade'))
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    def new_session_factory(self) -> async_sessionmaker[AsyncSession]:
        engine = create_async_engine(self._url)
        self._engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False)

    async def dispose(self) -> None:
        for engine in self._engines:
            await engine.dispose()
        self._engines.clear()


class WorkerProcess:
    """One simulated worker process: step loop + step reaper + run reconciler.

    The step loop is ``run_step_worker``'s loop with one deliberate difference:
    ``run_due_steps`` swallows a per-step ``CancelledError`` (the heartbeat
    lost-lease race) — a killed PROCESS must die mid-step with nothing
    recorded, so this loop lets ``CancelledError`` propagate untouched.
    """

    def __init__(
        self,
        lab: PostgresLab,
        name: str,
        settings: Settings,
        config: ForgeConfig,
        reconciler_service: RunService,
        *,
        poll_interval: float = 0.05,
    ) -> None:
        self.name = name
        self.factory = lab.new_session_factory()
        self.settings = settings
        self.config = config
        self.reconciler_service = reconciler_service
        self.poll_interval = poll_interval
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    def start(self) -> list[asyncio.Task]:
        self.tasks = [
            asyncio.create_task(self._step_loop(), name=f"{self.name}:steps"),
            asyncio.create_task(
                step_runtime.run_step_reaper(self.factory, self.stop, interval=self.poll_interval),
                name=f"{self.name}:reaper",
            ),
            asyncio.create_task(
                run_reconciler(
                    self.reconciler_service,
                    interval_seconds=self.poll_interval,
                    shutdown_event=self.stop,
                ),
                name=f"{self.name}:reconciler",
            ),
        ]
        return self.tasks

    async def _step_loop(self) -> None:
        while not self.stop.is_set():
            try:
                claimed = await step_runtime.claim_due_steps(self.factory, self.name)
                for step in claimed:
                    try:
                        await step_runtime.execute_claimed_step(
                            self.factory, self.settings, self.config, step
                        )
                    except asyncio.CancelledError:
                        raise  # process death is never converted into a step failure
                    except Exception:
                        # fail_step already recorded retry/dead on the row.
                        logging.getLogger(__name__).exception("Step %s failed", step.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Step worker pass failed")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def kill(self) -> None:
        """Hard process death: cancel every loop; no cleanup, no final writes."""
        for task in self.tasks:
            task.cancel()
        await gather_bounded(self.tasks, what=f"{self.name} loops")

    async def shutdown(self) -> None:
        self.stop.set()
        await gather_bounded(self.tasks, what=f"{self.name} loops")


class Cluster:
    """Shared stubs + injector + the two worker processes over the lab DB."""

    def __init__(self, lab: PostgresLab) -> None:
        self.lab = lab
        self.settings = make_settings()
        self.config = ForgeConfig()
        self.fake = FakeGitLab()
        self.fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        self.fake.seed_commit("main", BASE_SHA, "initial")
        self.planner = CountingPlanner()
        self.implementer = CountingImplementer()
        self.reviewer = StubReviewer()
        self.injector = CrashInjector()
        self.hooked_implementer = CrashAfterPropose(self.implementer, self.injector, "c")
        self.control = lab.new_session_factory()  # the "gateway"/assertion process
        self.workers: dict[str, WorkerProcess] = {}

    def install(self, monkeypatch) -> None:
        """Stub GitLab + agents at the RunService seams (the unit-profile pattern)."""
        monkeypatch.setattr(
            "forge.runs.service.GitLabClient", FakeGitLabClientFactory(shared=self.fake)
        )
        monkeypatch.setattr(
            "forge.runs.service.build_default_agents",
            lambda *args, **kwargs: (self.planner, self.hooked_implementer, self.reviewer),
        )

    def spawn(self, name: str) -> WorkerProcess:
        service = RunService(
            session_factory=self.lab.new_session_factory(),
            gitlab=self.fake,
            settings=self.settings,
            config=self.config,
            planner=self.planner,
            implementer=self.hooked_implementer,
            reviewer=self.reviewer,
        )
        worker = WorkerProcess(self.lab, name, self.settings, self.config, service)
        worker.start()
        if name == VICTIM:
            # Crash hooks fire only inside this process's task stacks.
            self.injector.watch(worker.tasks)
        self.workers[name] = worker
        return worker

    async def shutdown(self) -> None:
        for worker in self.workers.values():
            await worker.shutdown()


@pytest.fixture()
async def pg():
    """Fresh forge schema per test on the disposable lab Postgres."""
    lab = PostgresLab(PG_TEST_URL)
    await lab.reset()
    yield lab
    await lab.dispose()


@pytest.fixture()
async def cluster(pg, monkeypatch):
    """Two-worker cluster with stubbed seams; guarantees loops stop at teardown."""
    harness = Cluster(pg)
    harness.install(monkeypatch)
    try:
        yield harness
    finally:
        await harness.shutdown()


# ----------------------------------------------------------------------
# Transactional ingress (ADR-0017 §1) — the gateway's one-transaction commit
# ----------------------------------------------------------------------


async def ingress(factory: async_sessionmaker[AsyncSession], command: dict, note_id: int) -> None:
    """Persist inbox row + first scheduled step in ONE commit (the ``202`` contract).

    Redis dedup and the queue wake are accelerators in front of this
    transaction, not part of the durability contract — the suite exercises
    the transaction plus the step runtime behind it.
    """
    source_event_id = step_runtime.command_source_event_id(
        command["command"], command["project_id"], note_id
    )
    async with factory() as session:
        async with session.begin():
            _, created = await ingest_event(
                session,
                source_event_id=source_event_id,
                project_id=command["project_id"],
                event_type="run_command",
                payload={**command, "note_id": note_id},
            )
            assert created, f"note {note_id} already ingested"
            await step_runtime.schedule_command_step(
                session, command, source_event_id=source_event_id
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


def cancel_command(run_id: str) -> dict:
    return {
        "command": "cancel",
        "project_id": PROJECT_ID,
        "issue_iid": ISSUE_IID,
        "author_username": "alice",
        "note_text": f"@forge /cancel {run_id}",
    }


# ----------------------------------------------------------------------
# Checkpoint hooks (monkeypatched seams — no production test code)
# ----------------------------------------------------------------------


def arm_claim_crash(monkeypatch, injector: CrashInjector, checkpoint: str) -> None:
    """(a) die at the first claim attempt: the step is out but nobody owns it."""
    original = step_runtime.claim_due_steps

    async def claim_due_steps(*args, **kwargs):
        if injector.should_fire(checkpoint):
            injector.crash_now()
        return await original(*args, **kwargs)

    monkeypatch.setattr(step_runtime, "claim_due_steps", claim_due_steps)


def arm_execute_crash(monkeypatch, injector: CrashInjector, checkpoint: str) -> None:
    """(b) die after the claim committed (running + leased), before any effect."""
    original = step_runtime.execute_run_command

    async def execute_run_command(*args, **kwargs):
        if injector.should_fire(checkpoint):
            injector.crash_now()
        return await original(*args, **kwargs)

    monkeypatch.setattr(step_runtime, "execute_run_command", execute_run_command)


def arm_writer_crash(monkeypatch, injector: CrashInjector, checkpoint: str) -> None:
    """(d) die after writer.apply returned succeeded — commit landed, state lost."""
    original = ChangesetWriter.apply

    async def apply(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if injector.should_fire(checkpoint):
            injector.crash_now()
        return result

    monkeypatch.setattr(ChangesetWriter, "apply", apply)


def arm_draft_mr_crash(monkeypatch, injector: CrashInjector, checkpoint: str) -> None:
    """(e) die after the Draft MR was created but before the state save."""
    original = RunService._create_draft_mr

    async def _create_draft_mr(self, *args, **kwargs):
        mr_iid = await original(self, *args, **kwargs)
        if injector.should_fire(checkpoint):
            injector.crash_now()
        return mr_iid

    monkeypatch.setattr(RunService, "_create_draft_mr", _create_draft_mr)


def arm_evidence_note_crash(monkeypatch, injector: CrashInjector, checkpoint: str) -> None:
    """(f) die after the READY transition, before the evidence note started."""
    original = RunService._post_journaled_note

    async def _post_journaled_note(self, project_id, issue_iid, body, run_id, kind):
        if kind == "post_evidence_note" and injector.should_fire(checkpoint):
            injector.crash_now()
        return await original(self, project_id, issue_iid, body, run_id, kind)

    monkeypatch.setattr(RunService, "_post_journaled_note", _post_journaled_note)


# ----------------------------------------------------------------------
# Control-plane reads and convergence waits (no sleeps — poll Postgres)
# ----------------------------------------------------------------------


async def eventually(predicate, *, description: str, timeout: float = 20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while True:
        outcome = predicate()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return outcome
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {description}")
        await asyncio.sleep(interval)


async def run_status(control, run_id: str) -> str | None:
    async with control() as session:
        run = await session.get(FlowRun, run_id)
        return run.status if run is not None else None


async def first_run_id(control) -> str | None:
    async with control() as session:
        return await session.scalar(select(FlowRun.id).order_by(FlowRun.created_at).limit(1))


def wait_status(control, run_id: str, *statuses: str, **kwargs):
    async def reached() -> bool:
        return await run_status(control, run_id) in statuses

    return eventually(
        reached,
        description=f"run {run_id[:8]} to reach {statuses}",
        **kwargs,
    )


async def all_steps(control) -> list[StepRun]:
    """The COMMAND-runtime steps. R07 checkpoint rows
    (step_name="checkpoint:*") live in the same table and are not part of
    these contracts — they are store entries, not schedulable work."""
    async with control() as session:
        rows = list((await session.execute(select(StepRun))).scalars().all())
    return [row for row in rows if not row.step_name.startswith("checkpoint:")]


async def wait_steps_settled(control, *, at_least: int = 1, timeout: float = 30.0) -> list[StepRun]:
    """Every step reached a terminal lifecycle state — nothing running/pending."""

    async def settled() -> list[StepRun] | None:
        rows = await all_steps(control)
        if len(rows) >= at_least and all(row.status in TERMINAL_STEP_STATUSES for row in rows):
            return rows
        return None

    return await eventually(settled, description="all steps to settle", timeout=timeout)


async def expire_running_leases(control) -> None:
    """Force-expire live leases — stands in for the 120s lease wall clock."""
    async with control() as session:
        async with session.begin():
            await session.execute(
                update(StepRun)
                .where(StepRun.status == step_runtime.STEP_RUNNING)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )


async def gate_row(control, run_id: str) -> GateApproval | None:
    async with control() as session:
        return (
            (await session.execute(select(GateApproval).where(GateApproval.flow_run_id == run_id)))
            .scalars()
            .first()
        )


async def actions_of_kind(control, run_id: str, kind: str) -> list[ActionLog]:
    async with control() as session:
        return list(
            (
                await session.execute(
                    select(ActionLog).where(
                        ActionLog.flow_run_id == run_id,
                        ActionLog.action_kind == kind,
                    )
                )
            )
            .scalars()
            .all()
        )


async def report_green_ci(cluster: Cluster, control, run_id: str) -> None:
    """The target project's CI finishing green on the candidate (external world)."""
    async with control() as session:
        run = await session.get(FlowRun, run_id)
        sha = run.candidate_shas[-1]
        branch = factory_branch(run.issue_iid, run_id)
    pipeline = await cluster.fake.create_pipeline(PROJECT_ID, branch)
    cluster.fake.set_pipeline_status(pipeline["id"], "success", sha=sha)


async def wait_tasks_done(tasks: list[asyncio.Task], *, description: str, timeout=5.0) -> None:
    _, pending = await asyncio.wait(set(tasks), timeout=timeout)
    assert not pending, f"victim tasks did not die while {description}"


async def gather_bounded(tasks: list[asyncio.Task], *, timeout: float = 10.0, what: str) -> None:
    """Wait for tasks, but never hang the suite on a task ignoring cancellation."""
    _, pending = await asyncio.wait(set(tasks), timeout=timeout)
    if pending:
        for task in pending:
            task.cancel()
        logging.getLogger(__name__).warning("%s did not finish: %s", what, pending)


def assert_effect_counts(
    cluster: Cluster,
    *,
    commits: int,
    mrs: int,
    plan_notes: int,
    evidence_notes: int,
    duplicate_notes: int = 0,
    cancel_notes: int = 0,
) -> None:
    fake = cluster.fake
    assert len(fake.calls_of("create_commit")) == commits
    assert len(fake.calls_of("create_merge_request")) == mrs
    assert len(fake.notes_containing("Forge plan")) == plan_notes
    assert len(fake.notes_containing("ready for human review")) == evidence_notes
    assert len(fake.notes_containing("already active on this issue")) == duplicate_notes
    assert len(fake.notes_containing("**cancelled**")) == cancel_notes


def sha_message(commit: dict) -> str:
    return commit.get("message") or ""


async def wait_ready_with_evidence(cluster: Cluster, control, run_id: str) -> None:
    """READY plus the evidence note — the note posts just after the transition,
    and every scenario counts its exact number."""
    await wait_status(control, run_id, READY)
    await eventually(
        lambda: cluster.fake.notes_containing("ready for human review"),
        description="the evidence note",
    )


async def drive_go_leg_to_ready(cluster: Cluster, control, run_id: str, note_id: int) -> None:
    """``/go`` through the step runtime, CI reporting green, to ready_for_human."""
    await ingress(control, go_command(run_id), note_id)
    await wait_status(control, run_id, WAITING_CI)
    await report_green_ci(cluster, control, run_id)
    await wait_ready_with_evidence(cluster, control, run_id)


async def run_to_gate(cluster: Cluster, control, *, note_id: int) -> str:
    """Ingress ``/implement`` and wait for the run to park at the human gate."""
    await ingress(control, implement_command(), note_id)
    run_id = await eventually(lambda: first_run_id(control), description="the run row to exist")
    await wait_status(control, run_id, WAITING_APPROVAL)
    return run_id


# ----------------------------------------------------------------------
# S1 — checkpoint (a): crash after the ingress commit, before the claim
# ----------------------------------------------------------------------


class TestS1CrashBetweenIngressAndClaim:
    async def test_s1_scheduled_step_survives_and_survivor_completes_the_run(
        self, cluster: Cluster, monkeypatch
    ):
        """The ``202`` contract: once the step row committed, some worker WILL
        attempt the command — even though the first worker died before claiming."""
        control = cluster.control
        cluster.injector.arm("a")
        arm_claim_crash(monkeypatch, cluster.injector, "a")

        await ingress(control, implement_command(), note_id=100)
        victim = cluster.spawn(VICTIM)
        await wait_tasks_done(victim.tasks, description="dying at its first claim attempt")
        await victim.kill()

        # The crash left exactly the ingress commit's effects: an inbox row and
        # a scheduled, unclaimed, unbumped step.
        async with control() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1
        assert len(steps) == 1
        assert steps[0].status == "scheduled"
        assert steps[0].attempt == 0
        assert steps[0].fence_token == 0
        assert steps[0].lease_owner is None

        # The survivor claims the orphaned step and walks the run to the gate.
        cluster.spawn("worker-2")
        run_id = await eventually(
            lambda: first_run_id(control), description="the survivor to start the run"
        )
        await wait_status(control, run_id, WAITING_APPROVAL)
        assert cluster.planner.calls == 1

        # And the whole loop completes through the gate on the survivor.
        await drive_go_leg_to_ready(cluster, control, run_id, note_id=101)
        rows = await wait_steps_settled(control, at_least=2)
        assert all(step.status == "succeeded" for step in rows)
        async with control() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is not None

        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)
        assert await run_status(control, run_id) == READY


# ----------------------------------------------------------------------
# S2 — checkpoints (b) and (c): the command step dies mid-flight
# ----------------------------------------------------------------------


class TestS2CrashMidCommandStep:
    async def test_s2b_crash_after_claim_before_effect_recovers_via_lease_expiry(
        self, cluster: Cluster, monkeypatch
    ):
        """(b) running + leased, zero effects: the lease expires, the reaper
        reschedules, the survivor re-claims (fence bumps) and executes once."""
        control = cluster.control
        cluster.injector.arm("b")
        arm_execute_crash(monkeypatch, cluster.injector, "b")

        await ingress(control, implement_command(), note_id=200)
        victim = cluster.spawn(VICTIM)
        await wait_tasks_done(victim.tasks, description="dying right after the claim")
        await victim.kill()

        async with control() as session:
            step = (await session.execute(select(StepRun))).scalars().one()
        assert step.status == "running", "the hard kill recorded no failure"
        assert step.lease_owner == VICTIM
        assert step.fence_token == 1
        async with control() as session:
            assert (await session.execute(select(FlowRun))).scalars().all() == []
        assert cluster.planner.calls == 0, "no handler effect ran"

        # No real sleep: force the lease dead; the survivor's reaper reaps.
        await expire_running_leases(control)
        cluster.spawn("worker-2")
        run_id = await eventually(
            lambda: first_run_id(control), description="the re-claimed step to run"
        )
        await wait_status(control, run_id, WAITING_APPROVAL)
        rows = await wait_steps_settled(control)
        assert rows[0].status == "succeeded"
        assert rows[0].attempt == 1, "the reaper bumped the attempt"
        assert rows[0].fence_token == 2, "the re-claim granted a fresh fence"
        assert cluster.planner.calls == 1, "the plan ran exactly once across both workers"

        await drive_go_leg_to_ready(cluster, control, run_id, note_id=201)
        await wait_steps_settled(control, at_least=2)
        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)
        assert await run_status(control, run_id) == READY

    async def test_s2c_crash_after_gate_consumption_recovers_without_recompute_side_effects(
        self, cluster: Cluster
    ):
        """(c) the gate is consumed, the proposal computed — then the process
        dies. The survivor resumes the advance leg: the plan is NOT recomputed
        (it persisted), the proposal recomputes (nothing persisted), and there
        is no double commit."""
        control = cluster.control
        victim = cluster.spawn(VICTIM)
        run_id = await run_to_gate(cluster, control, note_id=300)
        assert cluster.planner.calls == 1

        cluster.injector.arm("c")
        await ingress(control, go_command(run_id), note_id=301)
        # The victim consumes the gate, commits proposing, computes the
        # proposal — and dies before anything persists.
        await wait_status(control, run_id, FlowStatus.PROPOSING.value)
        await wait_tasks_done(victim.tasks, description="dying after the gate consumption")
        await victim.kill()

        gate = await gate_row(control, run_id)
        assert gate is not None and gate.consumed_at is not None, "gate consumed exactly once"
        assert cluster.implementer.calls == 1, "the proposal was computed, never persisted"
        assert await run_status(control, run_id) == FlowStatus.PROPOSING.value

        await expire_running_leases(control)
        cluster.spawn("worker-2")
        await wait_status(control, run_id, WAITING_CI)
        await report_green_ci(cluster, control, run_id)
        await wait_ready_with_evidence(cluster, control, run_id)
        rows = await wait_steps_settled(control, at_least=2)

        assert rows[0].status == "succeeded"
        assert cluster.planner.calls == 1, "the persisted plan was recovered, not recomputed"
        assert cluster.implementer.calls == 2, "one recompute — acceptable pre-persistence"
        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)
        assert await run_status(control, run_id) == READY


# ----------------------------------------------------------------------
# S3 — checkpoint (d): crash after writer.apply, before the state save
# ----------------------------------------------------------------------


class TestS3CrashAfterCommit:
    async def test_s3_recovery_adopts_the_existing_commit_and_never_commits_twice(
        self, cluster: Cluster, monkeypatch
    ):
        control = cluster.control
        victim = cluster.spawn(VICTIM)
        run_id = await run_to_gate(cluster, control, note_id=400)

        cluster.injector.arm("d")
        arm_writer_crash(monkeypatch, cluster.injector, "d")
        await ingress(control, go_command(run_id), note_id=401)
        await wait_status(control, run_id, FlowStatus.COMMITTING.value)
        await wait_tasks_done(victim.tasks, description="dying after writer.apply succeeded")
        await victim.kill()

        branch = factory_branch(ISSUE_IID, run_id)
        commits = cluster.fake.calls_of("create_commit")
        assert len(commits) == 1, "the crashed attempt landed exactly one commit"
        # The branch carries the frozen base plus forge's single candidate.
        branch_commits = [c for c in cluster.fake.branches[branch] if c["sha"] != BASE_SHA]
        assert len(branch_commits) == 1, "exactly one forge commit on the factory branch"
        sha = branch_commits[0]["sha"]
        assert "forge-op:" in sha_message(branch_commits[0])
        # Durable trail: the journal knows, the run row does not.
        commit_actions = await actions_of_kind(control, run_id, "commit")
        assert len(commit_actions) == 1
        assert commit_actions[0].status == "succeeded"
        assert commit_actions[0].remote_result["sha"] == sha
        async with control() as session:
            run = await session.get(FlowRun, run_id)
            assert run.candidate_shas == [], "the crash beat the state save"

        await expire_running_leases(control)
        cluster.spawn("worker-2")
        await wait_status(control, run_id, WAITING_CI)
        await report_green_ci(cluster, control, run_id)
        await wait_ready_with_evidence(cluster, control, run_id)
        await wait_steps_settled(control, at_least=2)

        async with control() as session:
            run = await session.get(FlowRun, run_id)
        assert run.candidate_shas == [sha], "the adopted commit is THE candidate"
        branch_commits = [c for c in cluster.fake.branches[branch] if c["sha"] != BASE_SHA]
        assert len(branch_commits) == 1, "still exactly ONE forge commit on the branch"
        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)
        assert await run_status(control, run_id) == READY


# ----------------------------------------------------------------------
# S4 — checkpoints (e) and (f): crashes in the publishing leg
# ----------------------------------------------------------------------


class TestS4CrashInPublishingLeg:
    async def test_s4e_crash_after_mr_creation_adopts_the_journaled_mr(
        self, cluster: Cluster, monkeypatch
    ):
        control = cluster.control
        victim = cluster.spawn(VICTIM)
        run_id = await run_to_gate(cluster, control, note_id=500)

        cluster.injector.arm("e")
        arm_draft_mr_crash(monkeypatch, cluster.injector, "e")
        await ingress(control, go_command(run_id), note_id=501)
        await wait_status(control, run_id, FlowStatus.ENSURING_DRAFT_MR.value)
        await wait_tasks_done(victim.tasks, description="dying after the MR was created")
        await victim.kill()

        assert len(cluster.fake.calls_of("create_merge_request")) == 1, "the MR exists..."
        async with control() as session:
            run = await session.get(FlowRun, run_id)
        assert run.mr_iid is None, "...but the run state never caught up"
        mr_actions = await actions_of_kind(control, run_id, "create_merge_request")
        assert len(mr_actions) == 1 and mr_actions[0].status == "succeeded"

        await expire_running_leases(control)
        cluster.spawn("worker-2")
        await wait_status(control, run_id, WAITING_CI)
        await report_green_ci(cluster, control, run_id)
        await wait_ready_with_evidence(cluster, control, run_id)
        await wait_steps_settled(control, at_least=2)

        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)
        mr_actions = await actions_of_kind(control, run_id, "create_merge_request")
        assert len(mr_actions) == 1, "recovery adopted the MR — no second one"
        update_actions = await actions_of_kind(control, run_id, "update_merge_request")
        assert len(update_actions) == 1, "the adopted MR was updated, not recreated"
        assert await run_status(control, run_id) == READY

    async def test_s4f_crash_after_ready_before_evidence_note_recovers_exactly_one_note(
        self, cluster: Cluster, monkeypatch
    ):
        control = cluster.control
        victim = cluster.spawn(VICTIM)
        run_id = await run_to_gate(cluster, control, note_id=600)

        cluster.injector.arm("f")
        arm_evidence_note_crash(monkeypatch, cluster.injector, "f")
        await ingress(control, go_command(run_id), note_id=601)
        await wait_status(control, run_id, WAITING_CI)
        await report_green_ci(cluster, control, run_id)
        # The victim's reconciler drives ready → and dies mid-announcement.
        await wait_status(control, run_id, READY)
        await wait_tasks_done([victim.tasks[2]], description="dying before the evidence note")
        await victim.kill()

        assert cluster.fake.notes_containing("ready for human review") == []
        assert await actions_of_kind(control, run_id, "post_evidence_note") == []

        # The survivor's reconciler pass finds a READY run with no journaled
        # evidence note and posts exactly one.
        cluster.spawn("worker-2")
        await eventually(
            lambda: cluster.fake.notes_containing("ready for human review"),
            description="the recovered evidence note",
        )
        await wait_steps_settled(control, at_least=2)

        assert_effect_counts(cluster, commits=1, mrs=1, plan_notes=1, evidence_notes=1)

        async def completed_note_journal() -> list[ActionLog] | None:
            rows = await actions_of_kind(control, run_id, "post_evidence_note")
            if len(rows) == 1 and rows[0].status == "succeeded":
                return rows
            return None  # the journal may still be completing — wait for it

        note_actions = await eventually(
            completed_note_journal, description="the evidence-note journal to complete"
        )
        assert len(note_actions) == 1
        assert await run_status(control, run_id) == READY


# ----------------------------------------------------------------------
# S5 — cancel-vs-publish: /cancel stands down an in-flight proposal
# ----------------------------------------------------------------------


class TestS5CancelRacesInFlightProposal:
    async def test_s5_cancelled_run_never_publishes(self, cluster: Cluster, monkeypatch):
        control = cluster.control
        both = [cluster.spawn(VICTIM), cluster.spawn("worker-2")]
        run_id = await run_to_gate(cluster, control, note_id=700)

        # Freeze the in-flight proposal right before the publication grant is
        # consulted — the exact race window F13's stand-down covers.
        release = asyncio.Event()
        paused = asyncio.Event()
        original = RunService._publication_revoked

        async def _publication_revoked(self, target):
            if target == run_id and not paused.is_set():
                paused.set()
                await release.wait()
            return await original(self, target)

        monkeypatch.setattr(RunService, "_publication_revoked", _publication_revoked)

        await ingress(control, go_command(run_id), note_id=701)
        await eventually(lambda: paused.is_set(), description="the proposal leg to freeze")
        assert await run_status(control, run_id) == FlowStatus.COMMITTING.value

        try:
            # The cancel arrives while the proposal is in flight: the OTHER
            # worker executes it (the victim's loop is frozen mid-step).
            await ingress(control, cancel_command(run_id), note_id=702)
            await wait_status(control, run_id, FlowStatus.CANCELLED.value)
            await eventually(
                lambda: cluster.fake.notes_containing("**cancelled**"),
                description="the cancel confirmation note",
            )

            release.set()  # the frozen leg wakes up — into a revoked publication grant
            rows = await wait_steps_settled(control, at_least=3)
            assert all(step.status == "succeeded" for step in rows)

            assert await run_status(control, run_id) == FlowStatus.CANCELLED.value, (
                "stays cancelled"
            )
            assert cluster.fake.calls_of("create_commit") == [], "no commit before OR after cancel"
            assert cluster.fake.calls_of("create_branch") == [], "publication never started"
            assert cluster.fake.calls_of("create_merge_request") == []
            assert_effect_counts(
                cluster, commits=0, mrs=0, plan_notes=1, evidence_notes=0, cancel_notes=1
            )
        finally:
            release.set()  # never leave a worker frozen across teardown
            for worker in both:
                await worker.shutdown()


# ----------------------------------------------------------------------
# S6 — duplicate /implement under concurrency: the index is the arbiter
# ----------------------------------------------------------------------


class TestS6ConcurrentDuplicateImplement:
    async def test_s6_ten_concurrent_ingresses_yield_exactly_one_active_run(
        self, cluster: Cluster, monkeypatch
    ):
        control = cluster.control
        # Blind each start_run execution's FIRST active-run check so every one
        # of them reaches the INSERT — the partial unique index (F12) decides.
        real_find = RunService._find_active_run
        seen_services: set[int] = set()

        async def racing_find(self, project_id, issue_iid):
            if id(self) not in seen_services:
                seen_services.add(id(self))
                return None
            return await real_find(self, project_id, issue_iid)

        monkeypatch.setattr(RunService, "_find_active_run", racing_find)

        await asyncio.gather(
            *[ingress(control, implement_command(), note_id=800 + i) for i in range(10)]
        )
        cluster.spawn(VICTIM)
        cluster.spawn("worker-2")
        rows = await wait_steps_settled(control, at_least=10)

        assert len(rows) == 10 and all(step.status == "succeeded" for step in rows)
        async with control() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1, "exactly ONE run ever existed — the index ate the rest"
        assert runs[0].status == WAITING_APPROVAL
        assert cluster.planner.calls == 1, "only the winning run ever paid for a plan"
        assert_effect_counts(
            cluster, commits=0, mrs=0, plan_notes=1, evidence_notes=0, duplicate_notes=9
        )
