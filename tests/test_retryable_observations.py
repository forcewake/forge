"""R42-01 (#374) — transient feedback observations stay retryable.

The recorded defect (the review's P01): ``inbox+step committed, 202
answered → GitLab transiently unavailable → the handler returned with
no request outcome → the step recorded success`` — the command's DATA
survived but its processing OBLIGATION was lost: no round, no pending
request, nothing for the reconciler. The comment on the old plain
return ("the redelivery re-enters here") was FALSE — a webhook
redelivery deduplicates on the committed inbox; nothing re-enters.

These are the service-level rules (the REAL RunService over the note
ingress, sqlite):

- the P01 shape — a 503 on BOTH head-reading paths (the branch head AND
  the MR fallback) raises the TYPED retryable observation; nothing is
  recorded, nothing replied, and the deferral is journaled
  (``provider_observation.retry`` with ``observed: false`` — the
  head-never-observed FACT — plus ``feedback.outcome = pending``);
- the mutation arm — with ONLY the defer reverted to the plain return
  (the shipped symbol patched at test time, the #344 discipline), the
  command COMPLETES with no outcome: the detector's other half;
- the recovery — the SAME delivery re-enters once the surface is back
  and produces ONE outcome without a new user comment;
- 403 and 404 are SEPARATE PERMANENT refusals (``mr_forbidden`` /
  ``mr_missing``) — recorded, terminal, no retry storm, no reply
  attempt, no model calls;
- the round-admission MR-state read — the second defect site: a 503
  leaves the request durably ``recorded`` (accepted pending work) and
  the step retryable; the recovery admits the round;
- a head that moves between the deferral and its resolution is the
  EXISTING typed ``stale_head`` conflict — the original binding is
  preserved across the retry, never re-bound, never a
  ``note_id_conflict``;
- a crash after the pending outcome but before the reply re-attempts
  ONLY the reply — the durable result stands, no second correction
  admission;
- the exhaustion inference — the deferral on the claim's LAST attempt
  journals ``feedback.outcome = exhausted`` (the visible end-state the
  operator repair query lists);
- the repair query — accepted/succeeded feedback steps lacking a
  request outcome are FINDABLE (age/count), never auto-replayed;
- the step seam — ``fail_step`` honors the typed exception's declared
  ``retry_after`` as a floor on the next due time.

The full webhook → worker → restart trace lives in
``tests/production_entry/test_retryable_observations.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.revisions import (
    REQUEST_MR_FORBIDDEN,
    REQUEST_MR_MISSING,
    REQUEST_RECORDED,
    REQUEST_ROUND_ADMITTED,
    REQUEST_STALE_HEAD,
    REQUEST_STAGED,
    ReviewObservationRetryable,
    review_feedback_requests_of,
)
from forge.durable import FlowRun, Outbox, StepRun, as_aware_utc
from forge.durable.claims import bind_claim
from forge.durable.models import ReviewRound
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.runs.service import (
    FEEDBACK_OUTCOME_EVENT,
    PROVIDER_OBSERVATION_RETRY_EVENT,
    RunService,
    feedback_steps_without_outcome,
)
from forge.worker.steps import claim_due_steps, execution_claim, schedule_command_step
from tests.test_review_feedback import (
    RecordingImplementer,
    ReviewFakeGitLab,
    _feedback_command,
    _get_run,
    _landed_run,
    make_review_service,
)
from tests.test_review_rounds import RoundWriter, _ready_run
from tests.test_runs_service import FakeWriter

FIX_NOTE = "/fix rename `forge-demo/x.md` handler"


class FaultableGitLab(ReviewFakeGitLab):
    """The MR read surface with injectable per-read faults.

    ``branch_status``/``mr_status`` fault :meth:`get_branch_head` /
    :meth:`get_merge_request` with the given HTTP status — the two
    surfaces a head read travels; ``note_failures`` faults the MR-note
    WRITE the replies ride (the crash-after-outcome window).
    """

    def __init__(self) -> None:
        super().__init__()
        self.branch_status: int | None = None
        self.mr_status: int | None = None
        self.note_failures = 0

    async def get_branch_head(self, project_id: int, branch_name: str) -> str:
        if self.branch_status is not None:
            self.calls.append(("get_branch_head", (project_id, branch_name)))
            raise GitLabAPIError(self.branch_status, f"branch read faulted {self.branch_status}")
        return await super().get_branch_head(project_id, branch_name)

    async def get_merge_request(self, project_id: int, mr_iid: int):
        if self.mr_status is not None:
            self.calls.append(("get_merge_request", (project_id, mr_iid)))
            raise GitLabAPIError(self.mr_status, f"mr read faulted {self.mr_status}")
        return await super().get_merge_request(project_id, mr_iid)

    async def create_mr_note(self, project_id: int, mr_iid: int, body: str):
        if self.note_failures > 0:
            self.note_failures -= 1
            raise GitLabAPIError(503, "note write faulted")
        return await super().create_mr_note(project_id, mr_iid, body)


def _faulted_fake() -> FaultableGitLab:
    fake = FaultableGitLab()
    from tests.test_review_feedback import ISSUE_DESC, ISSUE_IID, ISSUE_TITLE

    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    return fake


@pytest.fixture()
async def retry_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _outbox_rows(db) -> list[Outbox]:
    async with db() as session:
        return list((await session.execute(select(Outbox).order_by(Outbox.id.asc()))).scalars())


async def _outcome_rows(db) -> list[dict]:
    return [
        dict(row.payload)
        for row in await _outbox_rows(db)
        if row.event_type == FEEDBACK_OUTCOME_EVENT
    ]


async def _observation_retries(db) -> list[dict]:
    return [
        dict(row.payload)
        for row in await _outbox_rows(db)
        if row.event_type == PROVIDER_OBSERVATION_RETRY_EVENT
    ]


async def _rounds(db) -> list[ReviewRound]:
    async with db() as session:
        return list((await session.execute(select(ReviewRound))).scalars().all())


# ----------------------------------------------------------------------
# The P01 shape — a transient head read keeps the command retryable
# ----------------------------------------------------------------------


class TestTheTransientHeadObservation:
    async def test_p01_a_503_on_both_head_paths_raises_the_typed_retry(self, retry_db):
        """The review's P01, asserted as the NEW contract: handler_calls=1,
        requests=0 — and the step can never record
        succeeded-with-no-outcome because the handler FAILS the step
        with the typed retryable observation instead of returning."""
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = 503  # the branch-head read
        fake.mr_status = 503  # the MR fallback read
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid
        fake.calls.clear()
        payload = _feedback_command(mr_iid, FIX_NOTE, note_id="9501")

        with pytest.raises(ReviewObservationRetryable) as deferred:
            await service.run_command(dict(payload))

        assert deferred.value.reason == "mr_head"
        assert deferred.value.status_code == 503
        # exactly ONE handler pass (the branch read + the MR fallback)
        assert [name for name, _ in fake.calls if name == "get_branch_head"] == ["get_branch_head"]
        assert [name for name, _ in fake.calls if name == "get_merge_request"] == [
            "get_merge_request"
        ]
        # the command's data survives; its OUTCOME does not exist yet
        run = await _get_run(retry_db, run_id)
        assert review_feedback_requests_of(run.evidence or {}) == {}
        assert fake.mr_notes == []
        # the observability trail: the head-never-observed fact + pending
        retries = await _observation_retries(retry_db)
        assert [row["reason"] for row in retries] == ["mr_head"]
        assert retries[0]["observed"] is False
        assert [row["outcome"] for row in await _outcome_rows(retry_db)] == ["pending"]

    async def test_p01_mutation_arm_the_plain_return_loses_the_obligation(
        self, retry_db, monkeypatch
    ):
        """Revert ONLY the defer to the plain return (the #374 defect,
        patched onto the shipped symbol the way a regression would
        reintroduce it) and the SAME trace completes silently: the
        command returns, nothing is recorded, nothing is journaled —
        succeeded-with-no-outcome. This arm documents the defect's
        observable; the test above is the detector that fails on it."""

        async def _plain_return(self, run_id, note_id, reason, exc) -> None:  # noqa: ARG002
            return None  # the reverted defer: log-and-return

        monkeypatch.setattr(RunService, "_defer_review_observation", _plain_return)
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = fake.mr_status = 503
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid

        completed = False
        try:
            await service.run_command(_feedback_command(mr_iid, FIX_NOTE, note_id="9502"))
            completed = True
        except ReviewObservationRetryable:
            pytest.fail("the defer must own the raise — the plain return is the defect")
        assert completed is True
        run = await _get_run(retry_db, run_id)
        assert review_feedback_requests_of(run.evidence or {}) == {}
        assert await _observation_retries(retry_db) == []
        assert await _outcome_rows(retry_db) == []

    async def test_the_recovered_retry_produces_one_outcome_without_a_new_comment(self, retry_db):
        """The faulted window passes (two identical deliveries both defer —
        ONE logical correction pending); the SAME command re-enters once
        the surface is back and produces exactly ONE outcome."""
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = fake.mr_status = 503
        service = make_review_service(retry_db, fake)
        run_id, head = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid
        payload = _feedback_command(mr_iid, FIX_NOTE, note_id="9503")

        # two identical native events during the in-flight observation
        for _ in range(2):
            with pytest.raises(ReviewObservationRetryable):
                await service.run_command(dict(payload))
        assert len(await _observation_retries(retry_db)) == 2  # both journaled

        # the fault heals — the retry re-enters with the SAME identity
        fake.branch_status = fake.mr_status = None
        await service.run_command(dict(payload))

        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert list(requests) == ["9503"]  # ONE logical correction
        assert requests["9503"].status == REQUEST_STAGED
        assert requests["9503"].head_sha == head  # bound at the first GOOD observation
        replies = [n for n in fake.mr_notes if "/approve-revision" in n["body"]]
        assert len(replies) == 1
        outcomes = await _outcome_rows(retry_db)
        assert outcomes[-1] == {
            "run_id": run_id,
            "note_id": "9503",
            "outcome": "completed",
            "reason": REQUEST_STAGED,
        }

    async def test_a_403_head_read_is_a_permanent_typed_refusal(self, retry_db):
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = fake.mr_status = 403
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid

        # completes normally — permanent, never a retryable raise
        await service.run_command(_feedback_command(mr_iid, FIX_NOTE, note_id="9504"))
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9504"].status == REQUEST_MR_FORBIDDEN
        assert fake.mr_notes == []  # the write surface is as forbidden as the read
        assert (await _outcome_rows(retry_db))[-1]["outcome"] == "refused"
        assert (await _outcome_rows(retry_db))[-1]["reason"] == REQUEST_MR_FORBIDDEN

        # a re-delivery is silent and NEVER a retry storm
        read_calls = len(fake.calls)
        await service.run_command(_feedback_command(mr_iid, FIX_NOTE, note_id="9504"))
        # one head-read pass (the branch read + the MR fallback), nothing more
        assert len(fake.calls) == read_calls + 2
        run = await _get_run(retry_db, run_id)
        assert list(review_feedback_requests_of(run.evidence or {})) == ["9504"]

    async def test_a_404_head_read_is_a_separate_permanent_refusal(self, retry_db):
        """403 and 404 are SEPARATE permanent cases — the record states
        which one it was."""
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = fake.mr_status = 404
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid

        await service.run_command(_feedback_command(mr_iid, FIX_NOTE, note_id="9505"))
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9505"].status == REQUEST_MR_MISSING
        assert requests["9505"].status != REQUEST_MR_FORBIDDEN
        assert (await _outcome_rows(retry_db))[-1]["reason"] == REQUEST_MR_MISSING


# ----------------------------------------------------------------------
# The round-admission observation (the second defect site)
# ----------------------------------------------------------------------


class TestTheRoundAdmissionObservation:
    async def test_a_transient_mr_state_read_leaves_the_request_recorded_and_retryable(
        self, retry_db
    ):
        """ready_for_human + /fix: the admission's MR-state read fails
        transiently — the request is already DURABLE (accepted pending
        work) and the step fails retryable; nothing stranded, no
        round."""
        FakeWriter.reset()
        RoundWriter.counter = 0
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake, writer_class=RoundWriter)
        run_id, candidate, mr_iid = await _ready_run(retry_db, fake, service)
        fake.mr_status = 503  # the admission's MR-state read only
        payload = _feedback_command(
            mr_iid, "/fix also handle `forge-demo/x.md` empty input", note_id="9601"
        )

        with pytest.raises(ReviewObservationRetryable) as deferred:
            await service.run_command(dict(payload))
        assert deferred.value.reason == "mr_state"

        # accepted, durable, PENDING — the obligation the retry discharges
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9601"].status == REQUEST_RECORDED
        assert requests["9601"].head_sha == candidate
        assert await _rounds(retry_db) == []
        assert [row["outcome"] for row in await _outcome_rows(retry_db)] == ["pending"]

        # the fault heals — the SAME command admits the round
        fake.mr_status = None
        await service.run_command(dict(payload))
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9601"].status == REQUEST_ROUND_ADMITTED
        assert len(await _rounds(retry_db)) == 1
        assert (await _outcome_rows(retry_db))[-1]["outcome"] == "completed"

    async def test_a_head_moved_between_the_deferral_and_its_resolution_is_stale_head(
        self, retry_db
    ):
        """The original binding survives the retry: a head that moved
        between the retryable observation and its resolution is the
        EXISTING typed ``stale_head`` decision — never a re-binding,
        never a ``note_id_conflict``, never a silent correction against
        a head the reviewer never saw."""
        FakeWriter.reset()
        RoundWriter.counter = 0
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake, writer_class=RoundWriter)
        run_id, candidate, mr_iid = await _ready_run(retry_db, fake, service)
        payload = _feedback_command(
            mr_iid, "/fix also handle `forge-demo/x.md` empty input", note_id="9602"
        )

        fake.mr_status = 503
        with pytest.raises(ReviewObservationRetryable):
            await service.run_command(dict(payload))
        bound = review_feedback_requests_of((await _get_run(retry_db, run_id)).evidence or {})[
            "9602"
        ]
        assert bound.head_sha == candidate

        # a HUMAN EDIT lands while the observation is retryable
        from forge.runs.stubs import factory_branch
        from tests.test_review_feedback import ISSUE_IID

        fake.mr_status = None
        fake.seed_commit(factory_branch(ISSUE_IID, run_id), "human-edit-during-retry", "human edit")
        await service.run_command(dict(payload))

        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9602"].status == REQUEST_STALE_HEAD
        assert requests["9602"].head_sha == candidate  # the binding never moved
        assert await _rounds(retry_db) == []  # human edits preserved, nothing admitted
        assert any("stale_head" in n["body"] for n in fake.mr_notes)
        assert (await _outcome_rows(retry_db))[-1]["reason"] == REQUEST_STALE_HEAD

    async def test_a_403_mr_state_read_is_the_permanent_surface_refusal(self, retry_db):
        FakeWriter.reset()
        RoundWriter.counter = 0
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake, writer_class=RoundWriter)
        run_id, _, mr_iid = await _ready_run(retry_db, fake, service)
        fake.mr_status = 403

        await service.run_command(
            _feedback_command(
                mr_iid, "/fix also handle `forge-demo/x.md` empty input", note_id="9603"
            )
        )
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9603"].status == REQUEST_MR_FORBIDDEN
        assert await _rounds(retry_db) == []
        assert fake.mr_notes == []


# ----------------------------------------------------------------------
# Reply delivery — independently recoverable (never a second admission)
# ----------------------------------------------------------------------


class TestReplyRecovery:
    async def test_a_crash_after_the_outcome_but_before_the_reply_replays_only_the_reply(
        self, retry_db
    ):
        """The staged outcome is durable BEFORE the reply posts; a failed
        comment fails the step (retry) and the re-entry re-attempts ONLY
        the reply — no second staging, no second correction admission,
        the durable result is never invalidated."""
        implementer = RecordingImplementer()
        FakeWriter.reset()
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake, implementer=implementer)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid
        payload = _feedback_command(mr_iid, FIX_NOTE, note_id="9701")
        fake.note_failures = 1  # the reply POST faults, once

        with pytest.raises(GitLabAPIError):
            await service.run_command(dict(payload))

        # the durable result already stands
        run = await _get_run(retry_db, run_id)
        staged = review_feedback_requests_of(run.evidence or {})["9701"]
        assert staged.status == REQUEST_STAGED
        decision_id = staged.decision_id
        dispatches_after_stage = len(implementer.dispatches)
        writers_after_stage = len(FakeWriter.instances)

        # the replay: ONLY the missing reply is re-attempted
        await service.run_command(dict(payload))
        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert list(requests) == ["9701"]
        assert requests["9701"].decision_id == decision_id  # no second staging
        assert len(implementer.dispatches) == dispatches_after_stage  # no dispatch
        assert len(FakeWriter.instances) == writers_after_stage  # no writer
        replies = [n for n in fake.mr_notes if "/approve-revision" in n["body"]]
        assert len(replies) == 1  # the reply landed exactly once

    async def test_a_moved_head_replay_of_a_staged_request_is_not_a_conflict(self, retry_db):
        """The retry's live head may have moved past the recorded binding —
        the durable record answers (the replay body, the journal dedup);
        the identity preservation means NEVER a ``note_id_conflict``
        that would strand the staged correction, and the binding the
        dispatch-time fence checks stays the ORIGINAL one."""
        FakeWriter.reset()
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake)
        run_id, head = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid
        payload = _feedback_command(mr_iid, FIX_NOTE, note_id="9702")
        await service.run_command(dict(payload))
        staged = review_feedback_requests_of((await _get_run(retry_db, run_id)).evidence or {})[
            "9702"
        ]
        assert staged.status == REQUEST_STAGED and staged.head_sha == head

        # the head moves; the SAME command re-enters (the step retry)
        from forge.runs.stubs import factory_branch
        from tests.test_review_feedback import ISSUE_IID

        fake.seed_commit(factory_branch(ISSUE_IID, run_id), "moved-sha-9702", "human edit")

        await service.run_command(dict(payload))  # no note_id_conflict, no re-staging

        run = await _get_run(retry_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert list(requests) == ["9702"]
        assert requests["9702"].status == REQUEST_STAGED  # the outcome stands
        assert requests["9702"].head_sha == head  # the ORIGINAL binding, never re-bound
        assert requests["9702"].decision_id == staged.decision_id
        replies = [n for n in fake.mr_notes if "/approve-revision" in n["body"]]
        assert len(replies) == 1  # the reply journal dedup held


# ----------------------------------------------------------------------
# The exhaustion inference + the step seam
# ----------------------------------------------------------------------


class TestExhaustionAndTheStepSeam:
    async def _delivered_under_claim(self, db, fake, service, payload, *, max_attempts):
        """run_command under a REAL claimed step's ambient claim."""
        async with db() as session:
            async with session.begin():
                await schedule_command_step(
                    session,
                    dict(payload),
                    source_event_id="r42-test-" + str(payload["note_id"]),
                    max_attempts=max_attempts,
                )
        claimed = (await claim_due_steps(db, "r42-worker"))[0]
        with bind_claim(execution_claim(claimed)):
            with pytest.raises(ReviewObservationRetryable):
                await service.run_command(dict(payload))

    async def test_the_final_attempt_deferral_journals_the_exhausted_outcome(self, retry_db):
        """The deferral on the claim's LAST attempt journals
        ``feedback.outcome = exhausted`` — the same arithmetic
        ``fail_step`` applies when it parks the step ``dead``."""
        FakeWriter.reset()
        fake = _faulted_fake()
        fake.branch_status = fake.mr_status = 503
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        mr_iid = (await _get_run(retry_db, run_id)).mr_iid
        payload = _feedback_command(mr_iid, FIX_NOTE, note_id="9801")

        await self._delivered_under_claim(retry_db, fake, service, payload, max_attempts=3)
        assert [row["outcome"] for row in await _outcome_rows(retry_db)] == ["pending"]

        await self._delivered_under_claim(retry_db, fake, service, payload, max_attempts=1)
        outcomes = await _outcome_rows(retry_db)
        assert outcomes[-1]["outcome"] == "exhausted"
        assert outcomes[-1]["reason"] == "mr_head"

    async def test_fail_step_honors_the_declared_retry_after_floor(self, retry_db):
        """The step seam: a failure declaring ``retry_after`` gets it as a
        FLOOR on the next due time (the bounded jittered backoff still
        applies above it)."""
        from forge.worker.steps import fail_step

        async with retry_db() as session:
            async with session.begin():
                await schedule_command_step(
                    session,
                    {"command": "review_feedback", "note_id": "9802"},
                    source_event_id="r" * 64,
                )
        claimed = (await claim_due_steps(retry_db, "r42-floor"))[0]

        outcome = await fail_step(retry_db, claimed, "declared hint", retry_after=120.0)
        assert outcome == "retry"
        async with retry_db() as session:
            step = await session.get(StepRun, claimed.id)
        assert step is not None and step.status == "scheduled"
        remaining = (as_aware_utc(step.due_at) - datetime.now(timezone.utc)).total_seconds()
        assert remaining >= 119, f"the declared floor must hold (remaining={remaining:.1f}s)"

        # a SEPARATE step, failed WITHOUT a hint — the plain bounded backoff
        async with retry_db() as session:
            async with session.begin():
                await schedule_command_step(
                    session,
                    {"command": "review_feedback", "note_id": "9803"},
                    source_event_id="s" * 64,
                )
        claimed = (await claim_due_steps(retry_db, "r42-floor-2"))[0]
        assert await fail_step(retry_db, claimed, "no hint") == "retry"
        async with retry_db() as session:
            step = await session.get(StepRun, claimed.id)
        assert step is not None and step.status == "scheduled"
        remaining = (as_aware_utc(step.due_at) - datetime.now(timezone.utc)).total_seconds()
        assert remaining <= 60, f"no hint — the capped backoff only ({remaining:.1f}s)"


# ----------------------------------------------------------------------
# The operator repair query — feedback.accepted_without_outcome
# ----------------------------------------------------------------------


class TestTheRepairQuery:
    async def _seed_terminal_step(
        self,
        db,
        *,
        note_id: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        async with db() as session:
            session.add(
                StepRun(
                    step_name="review_feedback",
                    status=status,
                    payload=payload,
                    source_event_id=f"repair-{note_id}",
                    finished_at=datetime.now(timezone.utc),
                    started_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    async def test_succeeded_dead_and_healthy_steps_classify(self, retry_db):
        FakeWriter.reset()
        fake = _faulted_fake()
        service = make_review_service(retry_db, fake)
        run_id, _ = await _landed_run(retry_db, fake, service)
        run = await _get_run(retry_db, run_id)
        mr_iid = run.mr_iid
        project_id = run.project_id

        base = {"command": "review_feedback", "project_id": project_id, "mr_iid": mr_iid}

        # 1. the historical damage: a SUCCEEDED step, no request at all
        await self._seed_terminal_step(
            retry_db, note_id="9901", status="succeeded", payload={**base, "note_id": "9901"}
        )
        # 2. the visible exhaustion: a DEAD step, request stranded `recorded`
        async with retry_db() as session:
            row = await session.get(FlowRun, run_id)
            evidence = dict(row.evidence or {})
            evidence.setdefault("review_feedback_requests", {})["9902"] = {
                "schema": "forge.review.feedback/1",
                "note_id": "9902",
                "run_id": run_id,
                "discussion_id": "",
                "mr_iid": mr_iid,
                "actor": "alice",
                "head_sha": "x" * 40,
                "classification": "in-scope_correction",
                "text": "fix",
                "referenced_paths": [],
                "diff_context": "",
                "created_at": "",
                "decision_id": "",
                "status": REQUEST_RECORDED,
                "invalidation": {},
                "conflict_with": "",
            }
            row.evidence = evidence
            await session.commit()
        await self._seed_terminal_step(
            retry_db, note_id="9902", status="dead", payload={**base, "note_id": "9902"}
        )
        # 3. healthy: a SUCCEEDED step whose request reached an outcome
        await service.run_command(_feedback_command(mr_iid, FIX_NOTE, note_id="9903"))
        await self._seed_terminal_step(
            retry_db, note_id="9903", status="succeeded", payload={**base, "note_id": "9903"}
        )
        # 4. no obligation: a SUCCEEDED step that resolved no run
        await self._seed_terminal_step(
            retry_db,
            note_id="9904",
            status="succeeded",
            payload={**base, "mr_iid": 424242, "note_id": "9904"},
        )
        # 5. in-flight (not terminal): never listed
        await self._seed_terminal_step(
            retry_db, note_id="9905", status="scheduled", payload={**base, "note_id": "9905"}
        )

        findings = await feedback_steps_without_outcome(retry_db)
        rows = [row["feedback.accepted_without_outcome"] for row in findings]
        assert [row["note_id"] for row in rows] == ["9901", "9902"]
        assert rows[0]["outcome"] == "succeeded_without_outcome"
        assert rows[0]["request_status"] is None
        assert rows[0]["age_seconds"] is not None
        assert rows[1]["outcome"] == "exhausted"
        assert rows[1]["request_status"] == REQUEST_RECORDED
        assert rows[1]["step_status"] == "dead"
