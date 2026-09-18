"""R10: the claim fence — queue ownership implies effect ownership.

The step runtime's fence token already arbitrates the STEP row; these tests
pin the run-level half of the contract:

- :meth:`Controller.transition_guarded` — the whole transition in ONE
  conditional UPDATE, whose rowcount is the arbitration: two actors with the
  same expectation produce exactly one applied transition and one typed
  :class:`StaleClaimError`; the loser writes no outbox row.
- :meth:`Controller.request_cancel` — ``cancel_requested`` and the run's
  ``cancellation_generation`` move in one statement.
- The publisher's grant (:func:`forge.runs.publisher.publication_grant_valid`)
  consulted at the RESERVATION point — a cancel during the long base reads
  forbids a NEW publication reservation; a commit that already started is
  not rolled back, but its completion records superseded evidence and never
  walks the run toward ``ready_for_human``.
- Late step results — a fenced handler finishing after its run went terminal
  records superseded evidence on the step, never a plain success.
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import (
    Controller,
    ExecutionClaim,
    FlowRun,
    FlowStatus,
    InvalidTransition,
    Outbox,
    RunNotFound,
    StepRun,
    StaleClaimError,
    bind_claim,
    current_claim,
)
from forge.models.base import Base
from forge.runs.candidate import parse_unified_diff
from forge.runs.publisher import (
    PublishResult,
    publication_grant_valid,
    publish_validated_candidate,
)
from forge.worker.steps import claim_due_steps, execute_claimed_step, schedule_command_step

BASE_SHA = "base-sha-1"
PROJECT_ID = 42
ISSUE_IID = 7


@pytest.fixture()
async def db(tmp_path):
    """File-backed SQLite: the arbitration tests need several sessions."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/claims.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_run(db, run_id: str | None = None, **overrides) -> str:
    run = FlowRun(id=run_id or uuid4().hex, project_id=1, issue_iid=1, base_sha=BASE_SHA)
    for key, value in overrides.items():
        setattr(run, key, value)
    async with db() as session:
        session.add(run)
        await session.commit()
    return run.id


async def reload_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


async def outbox_count(db, run_id: str) -> int:
    async with db() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Outbox).where(Outbox.flow_run_id == run_id)
                )
            ).scalar_one()
        )


# ----------------------------------------------------------------------
# Guarded transitions: the CAS is the state machine
# ----------------------------------------------------------------------


class TestGuardedTransition:
    async def test_two_actors_same_expectation_exactly_one_applies(self, db):
        """The mission scenario: both actors believe the run is ``validating``
        and move it to ``committing``. The first UPDATE matches; the second
        matches 0 rows and raises StaleClaimError — exactly one transition,
        exactly one outbox announcement."""
        run_id = await make_run(db, status=FlowStatus.VALIDATING.value)
        expectation = dict(expected_status=FlowStatus.VALIDATING)

        async with db() as session:
            winner = await Controller(session).transition_guarded(
                run_id, FlowStatus.COMMITTING, reason="actor-a", **expectation
            )
            await session.commit()
        assert winner.status == FlowStatus.COMMITTING.value

        async with db() as session:
            with pytest.raises(StaleClaimError) as exc:
                await Controller(session).transition_guarded(
                    run_id, FlowStatus.COMMITTING, reason="actor-b", **expectation
                )
        assert exc.value.observed_status == FlowStatus.COMMITTING.value
        assert exc.value.expected_status == FlowStatus.VALIDATING.value
        assert (await reload_run(db, run_id)).status == FlowStatus.COMMITTING.value
        # One applied transition, one announcement — the loser wrote nothing.
        assert await outbox_count(db, run_id) == 1

    async def test_generation_pin_fences_after_cancel(self, db):
        """A transition pinned to the cancellation generation it was minted
        under loses the moment a cancel bumps the run."""
        run_id = await make_run(db, status=FlowStatus.PROPOSING.value)

        async with db() as session:
            await Controller(session).transition_guarded(
                run_id,
                FlowStatus.VALIDATING,
                expected_status=FlowStatus.PROPOSING,
                expected_cancellation_generation=0,
            )
            await session.commit()

        async with db() as session:
            assert await Controller(session).request_cancel(run_id) == 1
            await session.commit()

        async with db() as session:
            with pytest.raises(StaleClaimError) as exc:
                await Controller(session).transition_guarded(
                    run_id,
                    FlowStatus.COMMITTING,
                    expected_status=FlowStatus.VALIDATING,
                    expected_cancellation_generation=0,
                )
        assert exc.value.observed_generation == 1
        assert exc.value.expected_generation == 0

        # The CURRENT generation applies.
        async with db() as session:
            await Controller(session).transition_guarded(
                run_id,
                FlowStatus.CANCELLED,
                expected_status=FlowStatus.VALIDATING,
                expected_cancellation_generation=1,
            )
            await session.commit()
        assert (await reload_run(db, run_id)).status == FlowStatus.CANCELLED.value

    async def test_derived_expectation_still_guards(self, db):
        """``expected_status=None`` derives the expectation from a read but
        still predicates the UPDATE on it — the write-time re-check is the
        guard, the read is only the belief."""
        run_id = await make_run(db, status=FlowStatus.PROPOSING.value)
        async with db() as session:
            run = await Controller(session).transition_guarded(run_id, FlowStatus.VALIDATING)
            await session.commit()
        assert run.status == FlowStatus.VALIDATING.value

    async def test_multi_status_expectation_for_cancel_edge(self, db):
        """The enter-from-anywhere edges take a SET of expected sources in
        one predicate."""
        run_id = await make_run(db, status=FlowStatus.COMMITTING.value)
        async with db() as session:
            await Controller(session).transition_guarded(
                run_id,
                FlowStatus.CANCELLED,
                expected_status={
                    FlowStatus.PROPOSING,
                    FlowStatus.VALIDATING,
                    FlowStatus.COMMITTING,
                },
                reason="cancelled mid-flight",
            )
            await session.commit()
        assert (await reload_run(db, run_id)).status == FlowStatus.CANCELLED.value

    async def test_impossible_edge_is_rejected_upfront(self, db):
        """The expected→target edge is validated against ADR-0004 BEFORE the
        UPDATE — an illegal move never reaches the row, writes no outbox."""
        run_id = await make_run(db, status=FlowStatus.ACCEPTED.value)
        async with db() as session:
            with pytest.raises(InvalidTransition):
                await Controller(session).transition_guarded(
                    run_id,
                    FlowStatus.READY_FOR_HUMAN,
                    expected_status=FlowStatus.ACCEPTED,
                )
        assert (await reload_run(db, run_id)).status == FlowStatus.ACCEPTED.value
        assert await outbox_count(db, run_id) == 0

    async def test_unknown_run_raises_run_not_found(self, db):
        """An unknown run surfaces as RunNotFound: the UPDATE matches 0 rows
        and the loser-path lookup finds nothing."""
        async with db() as session:
            with pytest.raises(RunNotFound):
                await Controller(session).transition_guarded(
                    "f" * 32, FlowStatus.PREFLIGHT, expected_status=FlowStatus.ACCEPTED
                )

    async def test_applied_transition_writes_one_guarded_outbox_row(self, db):
        run_id = await make_run(db, status=FlowStatus.VALIDATING.value)
        async with db() as session:
            await Controller(session).transition_guarded(
                run_id, FlowStatus.COMMITTING, expected_status=FlowStatus.VALIDATING
            )
            await session.commit()
        async with db() as session:
            rows = await session.execute(select(Outbox).where(Outbox.flow_run_id == run_id))
            (row,) = rows.scalars().all()
        assert row.event_type == "flow.transition"
        assert row.payload["from"] == FlowStatus.VALIDATING.value
        assert row.payload["to"] == FlowStatus.COMMITTING.value
        assert row.payload["guarded"] is True


class TestRequestCancel:
    async def test_sets_flag_and_bumps_generation_in_one_move(self, db):
        run_id = await make_run(db)
        async with db() as session:
            assert await Controller(session).request_cancel(run_id) == 1
            await session.commit()
        run = await reload_run(db, run_id)
        assert run.cancel_requested is True
        assert run.cancellation_generation == 1

        async with db() as session:
            assert await Controller(session).request_cancel(run_id) == 2
            await session.commit()
        assert (await reload_run(db, run_id)).cancellation_generation == 2

    async def test_unknown_run_raises_run_not_found(self, db):
        async with db() as session:
            with pytest.raises(RunNotFound):
                await Controller(session).request_cancel("e" * 32)


# ----------------------------------------------------------------------
# The publication grant
# ----------------------------------------------------------------------


class TestPublicationGrantValid:
    @staticmethod
    def _run(**overrides) -> FlowRun:
        run = FlowRun(id="0" * 32, project_id=1, issue_iid=1)
        for key, value in overrides.items():
            setattr(run, key, value)
        return run

    def test_live_run_at_matching_generation_grants(self):
        assert publication_grant_valid(self._run(cancellation_generation=3), 3)

    def test_cancel_requested_revokes(self):
        assert not publication_grant_valid(self._run(cancel_requested=True), None)

    def test_cancelled_status_revokes(self):
        assert not publication_grant_valid(self._run(status="cancelled"), None)

    def test_generation_mismatch_revokes(self):
        assert not publication_grant_valid(self._run(cancellation_generation=4), 3)

    def test_none_generation_applies_flag_level_checks_only(self):
        assert publication_grant_valid(self._run(cancellation_generation=7), None)


def _create_diff(path: str, content: str) -> str:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


class TestCancelForbidsReservation:
    """A cancel during a long LLM/blob read forbids a NEW publication
    reservation (R10) — the grant is re-checked at the reservation point,
    AFTER the reads, BEFORE the adapter."""

    @staticmethod
    def _bundle():
        return parse_unified_diff(_create_diff("forge-demo/x.md", "hello\n"), BASE_SHA, "completed")

    async def test_cancel_during_base_read_forbids_reservation(self, db):
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        run = await reload_run(db, run_id)
        calls: list[str] = []

        async def cancelling_fetch(ref: str, paths: list[str]) -> dict[str, str]:
            # The long read; the cancel lands DURING it — after the entry
            # grant check, before the reservation.
            async with db() as session:
                await Controller(session).request_cancel(run_id)
                await session.commit()
            return {}

        async def native(validated) -> PublishResult:
            calls.append("publish")
            return PublishResult(True, commit_sha="d" * 40)

        # The claim pinned generation 0 (the run's generation when minted).
        claim = ExecutionClaim(
            step_id=1,
            attempt=0,
            owner="w1",
            fence_token=1,
            cancellation_generation=0,
        )
        with bind_claim(claim):
            result = await publish_validated_candidate(
                run,
                self._bundle(),
                session_factory=db,
                fetch_base_contents=cancelling_fetch,
                native_publish=native,
            )

        assert not result.ok
        assert "publication_revoked" in result.reason
        assert calls == [], "a cancelled grant must not open a NEW reservation"

    async def test_cancel_during_commit_records_superseded_not_ready(self, db):
        """Best-effort completion: the already-started remote commit is NOT
        rolled back, but its completion records superseded evidence and the
        run is never walked toward ``ready_for_human``."""
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        run = await reload_run(db, run_id)

        async def fetch(ref: str, paths: list[str]) -> dict[str, str]:
            return {}

        async def native(validated) -> PublishResult:
            async with db() as session:
                await Controller(session).request_cancel(run_id)
                await session.commit()
            return PublishResult(True, commit_sha="d" * 40)

        result = await publish_validated_candidate(
            run,
            self._bundle(),
            session_factory=db,
            fetch_base_contents=fetch,
            native_publish=native,
        )

        assert result.ok, "the commit DID land — the result must not lie"
        assert result.superseded is True
        final = await reload_run(db, run_id)
        assert (final.evidence or {})["superseded"]["reason"] == "cancelled_during_publication"
        assert (final.evidence or {})["superseded"]["commit_sha"] == "d" * 40
        assert final.status != FlowStatus.READY_FOR_HUMAN.value

    async def test_live_grant_publishes_without_supersede(self, db):
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        run = await reload_run(db, run_id)

        async def fetch(ref: str, paths: list[str]) -> dict[str, str]:
            return {}

        async def native(validated) -> PublishResult:
            return PublishResult(True, commit_sha="d" * 40)

        result = await publish_validated_candidate(
            run,
            self._bundle(),
            session_factory=db,
            fetch_base_contents=fetch,
            native_publish=native,
        )
        assert result.ok
        assert result.superseded is False
        assert "superseded" not in ((await reload_run(db, run_id)).evidence or {})


# ----------------------------------------------------------------------
# Late step results (R17 guard, mirrored for steps)
# ----------------------------------------------------------------------


async def _bound_step(db, run_id: str) -> int:
    """Schedule a step bound to *run_id* (the flow_run binding is what lets a
    claim snapshot the run's cancellation generation) and return its id."""
    async with db() as session, session.begin():
        step = await schedule_command_step(
            session,
            {"command": "advance", "project_id": 1},
            source_event_id=uuid4().hex,
        )
        step.flow_run_id = run_id
        return step.id


class TestLateStepResults:
    async def _claimed_step(self, db):
        run_id = await make_run(db)
        step_id = await _bound_step(db, run_id)
        claimed = await claim_due_steps(db, "worker-a")
        assert [s.id for s in claimed] == [step_id]
        return run_id, claimed[0]

    @staticmethod
    def _noop_handler():
        async def fake_execute(settings, forge_config, session_factory, metadata):
            pass

        return fake_execute

    async def test_claim_snapshots_cancellation_generation(self, db):
        run_id = await make_run(db)
        await _bound_step(db, run_id)

        fresh_claim = await claim_due_steps(db, "worker-a")
        assert fresh_claim[0].cancellation_generation == 0

        # Cancel, then release the claim so the step can be re-claimed: the
        # next claim must see the bumped generation.
        async with db() as session:
            await Controller(session).request_cancel(run_id)
            await session.commit()
        async with db() as session, session.begin():
            await session.execute(
                update(StepRun).where(StepRun.status == "running").values(status="scheduled")
            )
        reclaimed = await claim_due_steps(db, "worker-b")
        assert reclaimed[0].cancellation_generation == 1

    async def test_execute_binds_claim_for_the_dispatch(self, db, monkeypatch):
        """The ExecutionClaim minted from the queue claim is the ambient
        context INSIDE the handler and gone afterwards."""
        _, claimed = await self._claimed_step(db)
        seen: list[ExecutionClaim | None] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            seen.append(current_claim())

        monkeypatch.setattr("forge.worker.steps.execute_run_command", fake_execute)
        await execute_claimed_step(db, object(), object(), claimed)

        (bound,) = seen
        assert bound is not None
        assert bound.step_id == claimed.id
        assert bound.attempt == claimed.attempt
        assert bound.owner == claimed.owner
        assert bound.fence_token == claimed.fence_token
        assert bound.cancellation_generation == 0
        assert current_claim() is None, "the claim must not leak past the dispatch"

    async def test_late_completion_after_run_went_terminal_records_superseded(
        self, db, monkeypatch
    ):
        """A fenced handler finishing after its run went terminal commits its
        step with superseded evidence — the run is never walked to READY."""
        run_id, claimed = await self._claimed_step(db)

        async with db() as session:
            controller = Controller(session)
            await controller.request_cancel(run_id)
            await controller.transition_guarded(
                run_id, FlowStatus.CANCELLED, expected_status=FlowStatus.ACCEPTED
            )
            await session.commit()

        monkeypatch.setattr("forge.worker.steps.execute_run_command", self._noop_handler())
        await execute_claimed_step(db, object(), object(), claimed)

        async with db() as session:
            step = await session.get(StepRun, claimed.id)
            run = await session.get(FlowRun, run_id)
        assert step is not None and run is not None
        assert step.status == "succeeded"
        assert step.output["superseded"]["reason"] == "run_terminal"
        assert step.output["superseded"]["run_status"] == FlowStatus.CANCELLED.value
        assert run.status == FlowStatus.CANCELLED.value, "a late result never sets READY"

    async def test_generation_stale_completion_records_revoked_grant(self, db, monkeypatch):
        """The run is not terminal yet, but a cancel bumped its generation
        after the claim: the late result is still marked superseded."""
        run_id, claimed = await self._claimed_step(db)

        async with db() as session:
            await Controller(session).request_cancel(run_id)
            await session.commit()

        monkeypatch.setattr("forge.worker.steps.execute_run_command", self._noop_handler())
        await execute_claimed_step(db, object(), object(), claimed)

        async with db() as session:
            step = await session.get(StepRun, claimed.id)
        assert step is not None
        assert step.status == "succeeded"
        assert step.output["superseded"]["reason"] == "publication_grant_cancelled"

    async def test_fresh_completion_records_no_supersede_marker(self, db, monkeypatch):
        _, claimed = await self._claimed_step(db)
        monkeypatch.setattr("forge.worker.steps.execute_run_command", self._noop_handler())
        await execute_claimed_step(db, object(), object(), claimed)

        async with db() as session:
            step = await session.get(StepRun, claimed.id)
        assert step is not None
        assert step.status == "succeeded"
        assert step.output is None, "a fresh result stays a plain result"


class TestClaimContext:
    def test_bind_and_reset(self):
        claim = ExecutionClaim(step_id=1, attempt=0, owner="w", fence_token=1)
        assert current_claim() is None
        with bind_claim(claim):
            assert current_claim() is claim
        assert current_claim() is None

    def test_bind_resets_on_error(self):
        claim = ExecutionClaim(step_id=1, attempt=0, owner="w", fence_token=1)
        with pytest.raises(RuntimeError), bind_claim(claim):
            raise RuntimeError("boom")
        assert current_claim() is None
