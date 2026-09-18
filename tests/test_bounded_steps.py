"""R07 bounded steps: persistable checkpoints with result replay.

The contract under test: a crash must never re-call the model or re-derive
published work. Every non-deterministic step of the builtin GitLab path (the
reference lane) persists its output under ``(run_id, cycle, step,
input_digest)`` BEFORE the next stage is scheduled, and a re-driven leg
replays the persisted result instead of paying again:

- crash-inject at each checkpoint (kill the fake mid-handler, re-drive) →
  the run resumes from the checkpoint and the model was called exactly once
  TOTAL;
- replay idempotency: the same input digest never re-runs the step; a
  changed digest legitimately does;
- manifest-vs-candidate reconciliation: the resumed walk continues the
  RECORDED proposal and adopts the journaled commit — it never re-proposes
  to continue an old publication, and the open R11 publication intent stays
  the publish window's sole owner (the replay hands off to it);
- partial publication: an intent left open by a crash is journaled state,
  resolved by the R11 scanner (adopt), never duplicated.

Crash injection raises :class:`_Boom` from the service seam the real crash
would have died at (a journaled note, a transition, an evidence write); the
step runtime's retry (or the reconciler pass) is then re-driven against a
healthy service instance sharing the same database and fake provider.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest

from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus, GateApproval, Outbox, RunSpec, StepRun
from forge.durable.models import PublicationIntent
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.repository.writer import ChangesetWriter
from forge.runs import RunService
from forge.runs.checkpoints import load_step_output, record_step_output, step_input_digest
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.fixtures.fake_github import FakeGitHub

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


class _Boom(Exception):
    """The injected crash."""


class MidPublicationWriter(ChangesetWriter):
    """The real writer, killed in the R11 crash window: the remote commit
    HAS landed, its journal completion has not. The attempt leaves an OPEN
    publication intent (``dispatched``) and a ``requested`` action row —
    exactly the state a killed worker leaves behind."""

    async def _succeed(self, action_id, intent_id, sha, meta, *, adopted):
        if not adopted:
            raise _Boom("crashed after the commit landed, before its journal")
        await super()._succeed(action_id, intent_id, sha, meta, adopted=True)


class CountingPlanner(StubPlanner):
    """The paid plan call, counted."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
        self.calls += 1
        return await super().plan(
            issue_title, issue_description, flow_run_id=flow_run_id, path_scope=path_scope
        )


class CountingImplementer(StubImplementer):
    """The paid propose call, counted."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def propose(self, run, issue_title, **kwargs):
        self.calls += 1
        return await super().propose(run, issue_title, **kwargs)


class CrashService(RunService):
    """RunService that dies at one injected seam, like a killed worker.

    Seams mirror the real crash points: a journaled note that never got
    posted (``crash_note_kind``), a transition that never committed
    (``crash_transition``), an evidence write that never landed
    (``crash_evidence_key``).
    """

    crash_note_kind: str | None = None
    crash_transition: FlowStatus | None = None
    crash_evidence_key: str | None = None

    async def _post_journaled_note(self, project_id, issue_iid, body, run_id, kind):
        if kind == self.crash_note_kind:
            raise _Boom(f"crashed before the {kind} write")
        return await super()._post_journaled_note(project_id, issue_iid, body, run_id, kind)

    async def _transition(self, run_id, status, reason=None):
        if self.crash_transition is not None and status == self.crash_transition:
            raise _Boom(f"crashed before the {status.value} transition")
        await super()._transition(run_id, status, reason=reason)

    async def _merge_run_evidence(self, run_id, patch):
        if self.crash_evidence_key is not None and self.crash_evidence_key in patch:
            raise _Boom(f"crashed before the {self.crash_evidence_key} evidence write")
        return await super()._merge_run_evidence(run_id, patch)


def make_service(db, fake_gitlab, *, crash=None, **overrides) -> RunService:
    """RunService with the REAL writer (journaled intents + adoption) and
    counting stub agents. ``crash`` names the injected seam, if any."""
    values: dict = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        planner=CountingPlanner(),
        implementer=CountingImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    service = CrashService(**values) if crash is not None else RunService(**values)
    if crash is not None:
        assert set(crash) <= {"crash_note_kind", "crash_transition", "crash_evidence_key"}
        for key, value in crash.items():
            setattr(service, key, value)
    return service


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        session.expunge(run)
        return run


async def plan_note_count(fake_gitlab: FakeGitLab) -> int:
    return len([n for n in fake_gitlab.notes if "Forge plan" in n["body"]])


async def evidence_note_count(fake_gitlab: FakeGitLab) -> int:
    return len([n for n in fake_gitlab.notes if "ready for human review" in n["body"]])


async def commit_call_count(fake_gitlab: FakeGitLab) -> int:
    return len([c for c in fake_gitlab.calls if c[0] == "create_commit"])


async def go(service: RunService, run_id: str) -> None:
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )


# ----------------------------------------------------------------------
# The checkpoint store itself
# ----------------------------------------------------------------------


class TestCheckpointStore:
    async def test_record_then_load_round_trips(self, db):
        digest = step_input_digest({"title": "x"})
        assert await record_step_output(
            db, run_id="r1", step="plan", input_digest=digest, output={"plan": "hello"}
        )
        assert await load_step_output(db, run_id="r1", step="plan", input_digest=digest) == {
            "plan": "hello"
        }

    async def test_same_key_never_overwrites(self, db):
        digest = step_input_digest({"title": "x"})
        await record_step_output(
            db, run_id="r1", step="plan", input_digest=digest, output={"plan": "first"}
        )
        assert not await record_step_output(
            db, run_id="r1", step="plan", input_digest=digest, output={"plan": "second"}
        )
        # The FIRST persisted result stands — a re-driven recorder cannot
        # replace the output a replay is entitled to.
        assert await load_step_output(db, run_id="r1", step="plan", input_digest=digest) == {
            "plan": "first"
        }

    async def test_changed_input_digest_cycle_and_run_miss(self, db):
        await record_step_output(
            db,
            run_id="r1",
            step="plan",
            input_digest=step_input_digest({"title": "x"}),
            output={"plan": "v1"},
        )
        assert (
            await load_step_output(
                db, run_id="r1", step="plan", input_digest=step_input_digest({"title": "y"})
            )
            is None
        )
        assert (
            await load_step_output(
                db,
                run_id="r1",
                step="plan",
                input_digest=step_input_digest({"title": "x"}),
                cycle=2,
            )
            is None
        )
        assert (
            await load_step_output(
                db, run_id="r2", step="plan", input_digest=step_input_digest({"title": "x"})
            )
            is None
        )

    async def test_checkpoints_are_step_rows_the_worker_never_claims(self, db):
        """Checkpoint rows are born ``succeeded`` — invisible to the claim
        loop (which selects ``scheduled``), never scheduled, never retried."""
        from forge.worker.steps import claim_due_steps

        await record_step_output(
            db, run_id="r1", step="plan", input_digest="d", output={"plan": "p"}
        )
        async with db() as session:
            rows = (
                (await session.execute(select(StepRun).where(StepRun.flow_run_id == "r1")))
                .scalars()
                .all()
            )
        assert [row.status for row in rows] == ["succeeded"]
        assert rows[0].step_name == "checkpoint:plan"
        assert await claim_due_steps(db, "worker-1") == []


# ----------------------------------------------------------------------
# plan (LLM): checkpoint + mid-planning crash recovery
# ----------------------------------------------------------------------


class TestPlanCheckpoint:
    async def test_crash_after_plan_replays_without_a_second_model_call(self, db, fake_gitlab):
        """Kill the handler after the plan result was persisted but before
        the plan note: the re-claimed /implement RESUMES the same run from
        the checkpoint — the planner runs exactly once and no forked run
        dangles on the issue."""
        crash = make_service(db, fake_gitlab, crash={"crash_note_kind": "post_plan_note"})
        with pytest.raises(_Boom):
            await crash.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        run_id = runs[0].id
        assert (await get_run(db, run_id)).status == FlowStatus.PLANNING.value
        planner = crash._planner  # type: ignore[attr-defined]
        assert isinstance(planner, CountingPlanner) and planner.calls == 1
        # The checkpoint is durable BEFORE the note was ever attempted.
        async with db() as session:
            checkpoints = (
                (
                    await session.execute(
                        select(StepRun).where(
                            StepRun.flow_run_id == run_id,
                            StepRun.step_name == "checkpoint:plan",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(checkpoints) == 1 and checkpoints[0].status == "succeeded"

        # The re-claimed command step re-drives with a FRESH service (a new
        # planner instance): zero model calls, same run, gate opened once.
        resumed_planner = CountingPlanner()
        healthy = make_service(db, fake_gitlab, planner=resumed_planner)
        assert (
            await healthy.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
            == run_id
        )

        assert resumed_planner.calls == 0  # the model was NOT called twice
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        assert await plan_note_count(fake_gitlab) == 1
        async with db() as session:
            gates = (await session.execute(select(GateApproval))).scalars().all()
            specs = (await session.execute(select(RunSpec))).scalars().all()
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(gates) == 1  # one pending decision, not one per attempt
        assert len(specs) == 1  # the frozen spec of the crashed attempt stands
        assert len(runs) == 1  # resumed in place — no dangling second run

    async def test_replay_of_every_tail_stage_is_idempotent(self, db, fake_gitlab):
        """A crash right before the WAITING_APPROVAL move (spec frozen, note
        posted, gate open): the resume re-performs NOTHING — one note, one
        gate, one spec, one model call."""
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.PLANNING.value  # the crash left it here
            await session.commit()

        resumed = make_service(db, fake_gitlab, planner=CountingPlanner())
        assert (
            await resumed.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
            == run_id
        )

        planner = service._planner  # type: ignore[attr-defined]
        assert isinstance(planner, CountingPlanner) and planner.calls == 1
        assert resumed._planner.calls == 0  # type: ignore[attr-defined]
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        assert await plan_note_count(fake_gitlab) == 1
        async with db() as session:
            assert len((await session.execute(select(GateApproval))).scalars().all()) == 1
            assert len((await session.execute(select(RunSpec))).scalars().all()) == 1

    async def test_input_digest_change_legitimately_replans(self, db, fake_gitlab):
        """An EDITED issue between the crash and the re-drive changes the
        plan input digest — re-planning is then the honest answer, not a
        replay of a stale plan."""
        crash = make_service(db, fake_gitlab, crash={"crash_note_kind": "post_plan_note"})
        with pytest.raises(_Boom):
            await crash.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        edited = "Widgets must also fizz the baz."
        healthy = make_service(db, fake_gitlab, planner=CountingPlanner())
        run_id = await healthy.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, edited, "alice")

        planner = healthy._planner  # type: ignore[attr-defined]
        assert isinstance(planner, CountingPlanner) and planner.calls == 1  # the honest re-run
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        async with db() as session:
            # The run executes the spec BOUND to it (re-frozen for the new
            # input) — RunSpec.id is a random hex id, not an ordering key.
            spec = (
                (
                    await session.execute(
                        select(RunSpec).where(
                            RunSpec.run_id == run_id, RunSpec.digest == run.spec_digest
                        )
                    )
                )
                .scalars()
                .one()
            )
        assert spec.document["task"]["description"] == edited


# ----------------------------------------------------------------------
# propose (LLM) + publish: manifest reconciliation and the R11 hand-off
# ----------------------------------------------------------------------


class TestProposeAndPublishCheckpoints:
    async def _started_run(self, db, fake_gitlab) -> str:
        service = make_service(db, fake_gitlab)
        return await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

    async def test_crash_before_validating_resumes_from_the_recorded_manifest(
        self, db, fake_gitlab
    ):
        """Kill the leg between the propose checkpoint and the VALIDATING
        move; the re-claimed /go adopts the RECORDED manifest — the model
        was called exactly once total, one commit was created."""
        run_id = await self._started_run(db, fake_gitlab)
        crash = make_service(db, fake_gitlab, crash={"crash_transition": FlowStatus.VALIDATING})
        with pytest.raises(_Boom):
            await go(crash, run_id)

        implementer = crash._implementer  # type: ignore[attr-defined]
        assert isinstance(implementer, CountingImplementer) and implementer.calls == 1
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.PROPOSING.value
        assert (run.evidence or {}).get("attempt", {}).get("manifest")  # the checkpoint

        healthy = make_service(db, fake_gitlab)
        await go(healthy, run_id)

        assert implementer.calls == 1  # no second proposal
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert len(run.candidate_shas) == 1
        assert await commit_call_count(fake_gitlab) == 1

    async def test_resume_adopts_the_committed_candidate(self, db, fake_gitlab):
        """Kill the leg after the commit landed AND its response was
        journaled but before the run state caught up: the resume adopts the
        JOURNALED commit (succeeded action row + live branch head) — never
        a second commit for the same manifest."""
        run_id = await self._started_run(db, fake_gitlab)
        crash = make_service(
            db, fake_gitlab, crash={"crash_transition": FlowStatus.ENSURING_DRAFT_MR}
        )
        with pytest.raises(_Boom):
            await go(crash, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.COMMITTING.value
        branch = factory_branch(ISSUE_IID, run_id)
        sha = await fake_gitlab.get_branch_head(PROJECT_ID, branch)
        async with db() as session:
            action = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "commit",
                            ActionLog.status == "succeeded",
                        )
                    )
                )
                .scalars()
                .one()
            )
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert action.remote_result["sha"] == sha
        assert len(intents) == 1 and intents[0].status == "committed"  # journaled response

        healthy = make_service(db, fake_gitlab)
        await go(healthy, run_id)

        # The commit was adopted from the journal: ONE commit call total.
        assert await commit_call_count(fake_gitlab) == 1
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.candidate_shas == [sha]
        # The replay handed off to the R11 intent — it never minted a rival.
        async with db() as session:
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(intents) == 1

    async def test_partial_publication_is_reconciled_through_the_open_intent(self, db, fake_gitlab):
        """The hardest window: the remote commit landed but the process died
        before ANY journal completion — an OPEN publication intent and no
        journaled sha. The resumed leg hands off to the R11 machinery: the
        writer's identity probe ADOPTS the landed commit (same key, parent
        intact) — one commit total, no duplicate, no re-proposal."""
        run_id = await self._started_run(db, fake_gitlab)
        crash = make_service(
            db,
            fake_gitlab,
            crash={"crash_transition": FlowStatus.ENSURING_DRAFT_MR},
            writer_class=MidPublicationWriter,
        )
        with pytest.raises(_Boom):
            await go(crash, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.COMMITTING.value
        assert await commit_call_count(fake_gitlab) == 1  # the commit DID land
        async with db() as session:
            action = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "commit",
                        )
                    )
                )
                .scalars()
                .one()
            )
            intent = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert action.status == "requested"  # the outcome was lost
        assert intent.status == "dispatched"  # the open publish window

        healthy = make_service(db, fake_gitlab)
        await go(healthy, run_id)
        await healthy.evaluate_publication_intents()  # the R11 recovery pass

        # One commit on the branch — the landed one, ADOPTED, never duplicated.
        assert await commit_call_count(fake_gitlab) == 1
        branch = factory_branch(ISSUE_IID, run_id)
        sha = await fake_gitlab.get_branch_head(PROJECT_ID, branch)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.candidate_shas == [sha]
        async with db() as session:
            intent = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
            action = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "commit",
                            ActionLog.status == "succeeded",
                        )
                    )
                )
                .scalars()
                .one()
            )
        assert intent.status == "adopted"  # resolved by identity, not re-posted
        assert action.remote_result["sha"] == sha
        assert len(fake_gitlab.merge_requests) == 1


# ----------------------------------------------------------------------
# review (LLM) + notify: replay and stranded-run recovery
# ----------------------------------------------------------------------


class TestReviewAndNotifyCheckpoints:
    async def _success_pipeline(self, db, fake_gitlab, run_id: str) -> None:
        run = await get_run(db, run_id)
        branch = factory_branch(ISSUE_IID, run_id)
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "success", run.candidate_shas[-1])

    async def _waiting_ci_run(self, db, fake_gitlab) -> str:
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await go(service, run_id)
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        return run_id

    async def test_crash_after_review_replays_without_a_second_model_call(self, db, fake_gitlab):
        """Kill the pass after the review was persisted but before the run
        went ready: the reconciler resume replays the PERSISTED review —
        the reviewer ran exactly once total."""
        run_id = await self._waiting_ci_run(db, fake_gitlab)
        await self._success_pipeline(db, fake_gitlab, run_id)
        reviewer = StubReviewer()
        crash = make_service(
            db,
            fake_gitlab,
            reviewer=reviewer,
            crash={"crash_transition": FlowStatus.READY_FOR_HUMAN},
        )
        await crash.evaluate_waiting_ci()  # the crash is swallowed per-run

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value  # stranded, not dead
        assert (run.evidence or {}).get("review", {}).get("sha")  # review persisted
        assert len(reviewer.calls) == 1

        healthy = make_service(db, fake_gitlab, reviewer=StubReviewer())
        await healthy.evaluate_waiting_ci()

        assert len(reviewer.calls) == 1  # replayed, never re-called
        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value
        assert await evidence_note_count(fake_gitlab) == 1

    async def test_stranded_evaluating_ci_resumes(self, db, fake_gitlab):
        """A crash between the EVALUATING_CI move and the verdict evidence
        used to strand the run. The scan now picks it up, makes the move
        exactly once, and finishes on the stored verdict."""
        run_id = await self._waiting_ci_run(db, fake_gitlab)
        await self._success_pipeline(db, fake_gitlab, run_id)
        crash = make_service(db, fake_gitlab, crash={"crash_evidence_key": "pipeline"})
        await crash.evaluate_waiting_ci()  # dies after the transition

        assert (await get_run(db, run_id)).status == FlowStatus.EVALUATING_CI.value

        healthy = make_service(db, fake_gitlab)
        await healthy.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        async with db() as session:
            targets = [
                row.payload["to"]
                for row in (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            ]
        assert targets.count("evaluating_ci") == 1  # the move was made once
        assert "reviewing" in targets and "ready_for_human" in targets

    async def test_stranded_reviewing_without_a_stored_review_runs_it_once(self, db, fake_gitlab):
        """A crash BEFORE the review was recorded leaves the run in
        ``reviewing`` with nothing to replay — the resume runs the reviewer
        its first (and only) time."""
        run_id = await self._waiting_ci_run(db, fake_gitlab)
        await self._success_pipeline(db, fake_gitlab, run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.REVIEWING.value  # stranded pre-review
            await session.commit()

        reviewer = StubReviewer()
        healthy = make_service(db, fake_gitlab, reviewer=reviewer)
        await healthy.evaluate_waiting_ci()

        assert len(reviewer.calls) == 1
        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value

    async def test_evidence_note_recovered_once_after_a_crash(self, db, fake_gitlab):
        """notify: a crash between the ready transition and the evidence
        note is healed by the recovery pass — and the journal keeps the
        re-driven pass from posting the note twice."""
        run_id = await self._waiting_ci_run(db, fake_gitlab)
        await self._success_pipeline(db, fake_gitlab, run_id)
        crash = make_service(db, fake_gitlab, crash={"crash_note_kind": "post_evidence_note"})
        await crash.evaluate_waiting_ci()

        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value
        assert await evidence_note_count(fake_gitlab) == 0  # died before the note

        healthy = make_service(db, fake_gitlab)
        await healthy.evaluate_ready_evidence()
        assert await evidence_note_count(fake_gitlab) == 1
        await healthy.evaluate_ready_evidence()  # the journaled note stands
        assert await evidence_note_count(fake_gitlab) == 1


# ----------------------------------------------------------------------
# Sanity: the replay keys bind the RIGHT inputs
# ----------------------------------------------------------------------


class TestReplayKeyDiscipline:
    def test_plan_key_binds_task_text_and_scope(self):
        a = step_input_digest({"title": "t", "description": "d", "path_scope": ["src/"]})
        b = step_input_digest({"title": "t", "description": "d", "path_scope": ["src/"]})
        c = step_input_digest({"title": "t", "description": "d2", "path_scope": ["src/"]})
        d = step_input_digest({"title": "t", "description": "d", "path_scope": []})
        assert a == b  # same inputs → same key → replay
        assert a != c  # edited task → re-plan
        assert a != d  # changed scope → re-plan

    async def test_full_start_leaves_only_replayable_checkpoints(self, db, fake_gitlab):
        """After a complete planning leg the run's step rows are the plan
        checkpoints (succeeded) — no schedulable residue, nothing for a
        worker to re-execute."""
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            rows = (
                (await session.execute(select(StepRun).where(StepRun.flow_run_id == run_id)))
                .scalars()
                .all()
            )
        assert rows and all(row.status == "succeeded" for row in rows)


# ----------------------------------------------------------------------
# GitHub lane (R07 extended where cheap): /go resume, propose checkpoint,
# review replay — the same replay contract on the second harness path
# ----------------------------------------------------------------------


class CountingGitHubImplementer(StubImplementer):
    """The GitHub lane's paid propose call, counted."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def propose(self, run, issue_title, **kwargs):
        self.calls += 1
        return await super().propose(run, issue_title, **kwargs)


class CountingGitHubReviewer:
    """The GitHub-shaped PR review, counted (never re-called on replay)."""

    def __init__(self) -> None:
        self.calls = 0

    async def review(self, **kwargs):
        from forge.factory.reviewer import ReviewVerdict

        self.calls += 1
        return ReviewVerdict(verdict="ok", summary="clean implementation", findings=())


class BoomOnceFlow(GitHubPublishFlow):
    """The publish leg, killed right after the propose checkpoint."""

    def __init__(self, client, proposer, *, reader=None):
        super().__init__(client, proposer, base_branch="main", reader=reader)
        self.armed = True

    async def publish_changeset(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            raise _Boom("crashed after the propose checkpoint, before the commit")
        return await super().publish_changeset(*args, **kwargs)


class TestGitHubLaneCheckpoints:
    GH_REPO = "acme/acme-widget"
    GH_PROJECT = 70010
    GH_ISSUE = 42
    GH_TITLE = "Add password reset"
    GH_DESC = "Users cannot reset their password."
    GH_BASE = "1" * 40

    @pytest.fixture()
    async def gh_db(self):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    @pytest.fixture()
    def gh_fake(self):
        fake = FakeGitHub()
        fake.seed_repo(self.GH_REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[self.GH_REPO]["main"] = self.GH_BASE
        fake.seed_issue(self.GH_REPO, self.GH_ISSUE, self.GH_TITLE, self.GH_DESC)
        return fake

    def make_github_service(
        self, gh_db, gh_fake, *, implementer=None, reviewer=None, flow=None
    ) -> GitHubRunService:
        from forge.config import ForgeConfig

        implementer = implementer or CountingGitHubImplementer()
        reviewer = reviewer or CountingGitHubReviewer()
        flow = flow or GitHubPublishFlow(
            gh_fake, proposer=implementer, base_branch="main", reader=gh_fake
        )
        stack = GitHubAgents(
            client=gh_fake,
            reader=gh_fake,
            planner=StubPlanner(),
            implementer=implementer,
            reviewer=reviewer,
            flow=flow,
        )
        return GitHubRunService(
            gh_db,
            make_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW="",
                FORGE_VERIFICATION_GRACE_SECONDS=0,
            ),
            ForgeConfig(),
            stack=stack,
            repo_full_name=self.GH_REPO,
        )

    async def go_github(self, service: GitHubRunService, run_id: str) -> None:
        await service.handle_go(
            project_id=self.GH_PROJECT,
            issue_number=self.GH_ISSUE,
            note_text=f"/go {run_id}",
            author_username="alice",
        )

    async def test_crashed_publish_resume_does_not_recall_the_model(self, gh_db, gh_fake):
        """A /go whose publish leg died after the propose checkpoint: the
        re-claimed /go RESUMES (the run is past waiting_approval with the
        gate consumed), replays the persisted manifest, and lands waiting_ci
        with ONE proposal call and ONE commit."""
        implementer = CountingGitHubImplementer()
        crash = self.make_github_service(
            gh_db,
            gh_fake,
            implementer=implementer,
            flow=BoomOnceFlow(gh_fake, proposer=implementer, reader=gh_fake),
        )
        run_id = await crash.start_run(
            project_id=self.GH_PROJECT,
            issue_number=self.GH_ISSUE,
            issue_title=self.GH_TITLE,
            issue_description=self.GH_DESC,
            author_username="alice",
        )
        with pytest.raises(_Boom):
            await self.go_github(crash, run_id)

        assert implementer.calls == 1
        assert (await get_run(gh_db, run_id)).status == FlowStatus.PROPOSING.value
        async with gh_db() as session:
            checkpoints = (
                (
                    await session.execute(
                        select(StepRun).where(
                            StepRun.flow_run_id == run_id,
                            StepRun.step_name == "checkpoint:propose",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(checkpoints) == 1 and checkpoints[0].status == "succeeded"

        healthy = self.make_github_service(gh_db, gh_fake, implementer=CountingGitHubImplementer())
        await self.go_github(healthy, run_id)  # resume: replays, never re-proposes

        assert healthy._stack.implementer.calls == 0  # type: ignore[attr-defined]
        run = await get_run(gh_db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert len(run.candidate_shas) == 1
        assert len(gh_fake.calls_of("create_commit_on_branch")) == 1

    async def test_stranded_review_resume_replays_the_stored_review(self, gh_db, gh_fake):
        """A run left in ``reviewing`` by a crashed verification pass: the
        scan picks it up and the PERSISTED review is replayed — the model
        ran exactly once, then the run goes ready."""
        service = self.make_github_service(gh_db, gh_fake)
        run_id = await service.start_run(
            project_id=self.GH_PROJECT,
            issue_number=self.GH_ISSUE,
            issue_title=self.GH_TITLE,
            issue_description=self.GH_DESC,
            author_username="alice",
        )
        await self.go_github(service, run_id)
        reviewer = service._stack.reviewer  # type: ignore[attr-defined]
        assert isinstance(reviewer, CountingGitHubReviewer)

        await service.evaluate_waiting_ci_one(run_id)  # full pass → ready
        assert (await get_run(gh_db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value
        assert reviewer.calls == 1

        # Simulate the crash window: the review is persisted, the run never
        # made the READY move.
        async with gh_db() as session:
            run = await session.get(FlowRun, run_id)
            assert (run.evidence or {}).get("review", {}).get("sha")
            run.status = FlowStatus.REVIEWING.value
            await session.commit()

        fresh_reviewer = CountingGitHubReviewer()
        healthy = self.make_github_service(gh_db, gh_fake, reviewer=fresh_reviewer)
        await healthy.evaluate_waiting_ci_one(run_id)  # not waiting_ci — ignored
        await healthy.resume_verification(run_id)  # the stranded-run driver

        assert fresh_reviewer.calls == 0  # replayed from evidence
        assert (await get_run(gh_db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value
