"""A04: stale owners lose the right to mutate — the guarded CAS is THE path.

Regression ground for probe P07 (docs/reviews/2026-09-18-d16f523) and the
R10/A04 discipline across every mutation boundary:

- the PLAIN ``Controller.transition`` routes through the guarded CAS: two
  sessions read ``reviewing``; one commits ``cancelled``; the stale one's
  ``ready_for_human`` write is refused (typed :class:`StaleClaimError`, no
  outbox row, run untouched);
- the revival edges are CASes too — a revive whose run moved between the
  pre-read and the write is refused, never a second reopen;
- the publisher arbitrates the ambient
  :class:`~forge.durable.claims.ExecutionClaim`'s step OWNERSHIP at the
  reservation point: a stale lease/fence/binding stands the publish leg down
  BEFORE the native call, with superseded evidence and zero native writes;
- a late harness callback for a run that reached ANY terminal status is
  recorded as superseded evidence on all three provider lanes (GitLab,
  GitHub, Azure DevOps) — never published, never revived.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import (
    Controller,
    ExecutionClaim,
    FlowRun,
    FlowStatus,
    Outbox,
    StaleClaimError,
    StepRun,
    bind_claim,
)
from forge.models.base import Base
from forge.runs.backends import HarnessOutcome
from forge.runs.candidate import parse_unified_diff
from forge.runs.publisher import PublishResult, publish_validated_candidate
from forge.worker.steps import (
    claim_due_steps,
    execution_claim,
    reschedule_expired_leases,
    schedule_command_step,
)
from tests.fixtures.candidate import create_diff

BASE_SHA = "base-sha-1"
PROJECT_ID = 42
ISSUE_IID = 7


@pytest.fixture()
async def db(tmp_path):
    """File-backed SQLite: the arbitration tests need several sessions."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/guarded.db")
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
# P07: the plain transition is the guarded CAS
# ----------------------------------------------------------------------


class TestP07StaleOwnerLosesTheMutation:
    async def test_stale_ready_write_refused_after_concurrent_cancel(self, db):
        """Probe P07, as a regression: two sessions read ``reviewing``; one
        commits ``cancelled``; the stale one's ``ready_for_human`` write used
        to overwrite. The delegated CAS matches 0 rows instead."""
        run_id = await make_run(db, status=FlowStatus.REVIEWING.value)

        async with db() as winner_session, db() as stale_session:
            stale_view = await stale_session.get(FlowRun, run_id)
            winner_view = await winner_session.get(FlowRun, run_id)
            assert stale_view.status == winner_view.status == FlowStatus.REVIEWING.value
            # Release both read transactions (SQLite is single-writer); the
            # ORM views stay unexpired — each actor now holds the belief it
            # would carry through its whole effectful leg.
            await stale_session.commit()
            await winner_session.commit()

            # Actor B wins the row: the run is cancelled.
            await Controller(winner_session).transition(run_id, FlowStatus.CANCELLED)
            await winner_session.commit()

            # Actor A still believes the run is ``reviewing`` and wants to
            # walk it to ready_for_human — the stale write must be refused.
            assert stale_view.status == FlowStatus.REVIEWING.value, "stale belief is the point"
            with pytest.raises(StaleClaimError) as exc:
                await Controller(stale_session).transition(
                    run_id, FlowStatus.READY_FOR_HUMAN, reason="review done"
                )

        assert exc.value.expected_status == FlowStatus.REVIEWING.value
        assert exc.value.observed_status == FlowStatus.CANCELLED.value
        assert (await reload_run(db, run_id)).status == FlowStatus.CANCELLED.value
        # One announcement — the winner's. The stale owner wrote none.
        assert await outbox_count(db, run_id) == 1

    async def test_two_competing_plain_transitions_one_applies_one_is_stale(self, db):
        """A04 acceptance: two competing transitions with one version —
        exactly one applied, one typed StaleClaimError, one outbox row."""
        run_id = await make_run(db, status=FlowStatus.VALIDATING.value)

        async with db() as first, db() as second:
            # Hold the loaded rows, as a real effectful leg does: the stale
            # actor's belief must survive (the identity map is weak).
            await first.get(FlowRun, run_id)
            second_view = await second.get(FlowRun, run_id)
            await first.commit()
            await second.commit()

            winner = await Controller(first).transition(
                run_id, FlowStatus.COMMITTING, reason="actor-a"
            )
            await first.commit()
            assert winner.status == FlowStatus.COMMITTING.value
            assert second_view.status == FlowStatus.VALIDATING.value, "stale belief is the point"

            with pytest.raises(StaleClaimError) as exc:
                await Controller(second).transition(run_id, FlowStatus.COMMITTING, reason="actor-b")

        assert exc.value.observed_status == FlowStatus.COMMITTING.value
        assert (await reload_run(db, run_id)).status == FlowStatus.COMMITTING.value
        assert await outbox_count(db, run_id) == 1

    async def test_winner_write_still_commits_and_announces_atomically(self, db):
        """The happy path is unchanged: a fresh plain transition applies and
        announces exactly as before (the outbox payload stays announcement-
        compatible — no CAS machinery leaks into consumers)."""
        run_id = await make_run(db, status=FlowStatus.ACCEPTED.value)
        async with db() as session:
            run = await Controller(session).transition(
                run_id, FlowStatus.PREFLIGHT, reason="kickoff"
            )
            await session.commit()
        assert run.status == FlowStatus.PREFLIGHT.value
        async with db() as session:
            (row,) = (await session.execute(select(Outbox))).scalars().all()
        assert row.payload == {
            "flow_run_id": run_id,
            "from": FlowStatus.ACCEPTED.value,
            "to": FlowStatus.PREFLIGHT.value,
            "reason": "kickoff",
        }


# ----------------------------------------------------------------------
# The revival edges are CASes
# ----------------------------------------------------------------------


class TestRevivalCAS:
    async def test_revive_refused_when_the_run_moved_during_revive(self, db):
        """A revive that read ``blocked`` but writes after the run was walked
        to ``proposing`` by another actor matches 0 rows — a run is never
        reopened twice by competing revivals."""
        run_id = await make_run(db, status=FlowStatus.BLOCKED.value, status_reason="transient")

        async with db() as retryer, db() as second_pass:
            stale_view = await second_pass.get(FlowRun, run_id)
            await retryer.get(FlowRun, run_id)
            await retryer.commit()
            await second_pass.commit()

            walked = await Controller(retryer).revive_transition(
                run_id, reason="operator /retry", authorized_by="operator"
            )
            await retryer.commit()
            assert walked.status == FlowStatus.PROPOSING.value
            assert stale_view.status == FlowStatus.BLOCKED.value, "stale belief is the point"

            with pytest.raises(StaleClaimError) as exc:
                await Controller(second_pass).revive_transition(
                    run_id, reason="auto-revive 1/3", authorized_by="auto_revive"
                )

        assert exc.value.observed_status == FlowStatus.PROPOSING.value
        assert (await reload_run(db, run_id)).status == FlowStatus.PROPOSING.value
        assert await outbox_count(db, run_id) == 1

    async def test_plan_restart_refused_when_the_run_moved(self, db):
        """The A13 plan-restart edge pins ``blocked`` the same way: a stale
        restart after the run moved on is refused, never re-planned blind."""
        run_id = await make_run(db, status=FlowStatus.BLOCKED.value, status_reason="config_gone")

        async with db() as restarter, db() as mover:
            stale_view = await restarter.get(FlowRun, run_id)
            await mover.get(FlowRun, run_id)
            await restarter.commit()
            await mover.commit()

            await Controller(mover).revive_transition(
                run_id, reason="revived elsewhere", authorized_by="auto_revive"
            )
            await mover.commit()
            assert stale_view.status == FlowStatus.BLOCKED.value, "stale belief is the point"

            with pytest.raises(StaleClaimError):
                await Controller(restarter).restart_plan_transition(
                    run_id, reason="config readable again", authorized_by="config_recovery"
                )

        assert (await reload_run(db, run_id)).status == FlowStatus.PROPOSING.value


# ----------------------------------------------------------------------
# Publisher ownership: the claim must still own its step at dispatch
# ----------------------------------------------------------------------


def _bundle():
    return parse_unified_diff(create_diff("forge-demo/x.md", "hello\n"), BASE_SHA, "completed")


async def _bound_claim(db, run_id: str, owner: str = "worker-a") -> tuple[ExecutionClaim, StepRun]:
    """Schedule + claim a step bound to *run_id*; return (claim, claimed row)."""
    async with db() as session, session.begin():
        step = await schedule_command_step(
            session,
            {"command": "advance", "project_id": 1},
            source_event_id=uuid4().hex,
        )
        step.flow_run_id = run_id
        step_id = step.id
    claimed = await claim_due_steps(db, owner)
    assert [s.id for s in claimed] == [step_id]
    row = claimed[0]
    return execution_claim(row), row


async def _publish(db, run, claim: ExecutionClaim | None, native_calls: list) -> PublishResult:
    """One publish through the boundary; *claim* bound when given."""

    async def fetch(ref: str, paths: list[str]) -> dict[str, str]:
        return {}

    async def native(validated) -> PublishResult:
        native_calls.append(validated)
        return PublishResult(True, commit_sha="d" * 40)

    kwargs = {}
    if claim is not None:
        kwargs["session_factory"] = db  # the reservation leg needs the step row
    if claim is None:
        return await publish_validated_candidate(
            run,
            _bundle(),
            fetch_base_contents=fetch,
            native_publish=native,
        )
    with bind_claim(claim):
        return await publish_validated_candidate(
            run,
            _bundle(),
            fetch_base_contents=fetch,
            native_publish=native,
            **kwargs,
        )


class TestPublisherClaimOwnership:
    async def test_live_claim_publishes_without_stand_down(self, db):
        """The control case: a claim whose step row still shows its owner,
        fence and live lease dispatches normally — the arbitration only
        refuses STALE owners."""
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        claim, _row = await _bound_claim(db, run_id)
        run = await reload_run(db, run_id)
        native_calls: list = []

        result = await _publish(db, run, claim, native_calls)

        assert result.ok
        assert result.superseded is False
        assert len(native_calls) == 1
        assert "superseded" not in ((await reload_run(db, run_id)).evidence or {})

    @staticmethod
    async def _mutate_step(db, step_id: int, **values) -> None:
        async with db() as session, session.begin():
            await session.execute(update(StepRun).where(StepRun.id == step_id).values(**values))

    @pytest.mark.parametrize(
        ("case", "mutate"),
        [
            pytest.param(
                "fence_moved",
                lambda db, step_id, other: TestPublisherClaimOwnership._mutate_step(
                    db, step_id, fence_token=999
                ),
                id="fence moved by a reaping re-claim",
            ),
            pytest.param(
                "lease_reassigned",
                lambda db, step_id, other: TestPublisherClaimOwnership._mutate_step(
                    db, step_id, lease_owner="worker-z"
                ),
                id="lease owned by another worker",
            ),
            pytest.param(
                "lease_expired",
                lambda db, step_id, other: TestPublisherClaimOwnership._mutate_step(
                    db, step_id, lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)
                ),
                id="lease expired before the heartbeat tick",
            ),
            pytest.param(
                "reaped",
                lambda db, step_id, other: TestPublisherClaimOwnership._mutate_step(
                    db, step_id, status="scheduled", lease_owner=None, lease_expires_at=None
                ),
                id="step reaped back to scheduled",
            ),
            pytest.param(
                "bound_to_other_run",
                lambda db, step_id, other: TestPublisherClaimOwnership._mutate_step(
                    db, step_id, flow_run_id=other
                ),
                id="step bound to a different run",
            ),
        ],
    )
    async def test_stale_claim_stands_down_before_the_native_call(self, db, case, mutate):
        """A04 acceptance: an old lease/fence makes NO new native write, even
        before a heartbeat tick — the leg stands down with superseded
        evidence on the run and a typed claim_superseded reason."""
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        other_run_id = await make_run(db)
        claim, claimed = await _bound_claim(db, run_id)
        await mutate(db, claimed.id, other_run_id)
        run = await reload_run(db, run_id)
        native_calls: list = []

        result = await _publish(db, run, claim, native_calls)

        assert not result.ok
        assert result.reason.startswith("claim_superseded"), result.reason
        assert native_calls == [], "a stale claim must not open a NEW native write"
        evidence = (await reload_run(db, run_id)).evidence or {}
        assert evidence["superseded"]["reason"] == "publication_claim_stale"
        assert evidence["superseded"]["commit_sha"] is None

    async def test_transport_caller_without_a_claim_is_unaffected(self, db):
        """No ambient claim (transport callers, legacy legs): the boundary
        keeps its grant/fence checks and dispatches — the arbitration only
        binds executions that present a claim."""
        run_id = await make_run(db, project_id=PROJECT_ID, issue_iid=ISSUE_IID)
        run = await reload_run(db, run_id)
        native_calls: list = []

        result = await _publish(db, run, None, native_calls)

        assert result.ok
        assert len(native_calls) == 1


# ----------------------------------------------------------------------
# Late callbacks after ANY terminal status, on all three lanes
# ----------------------------------------------------------------------


class TestLateCallbackAfterTerminal:
    async def test_gitlab_harness_candidate_after_failed_run_is_superseded(self, db):
        """GitLab lane: the harness finished AFTER the run went terminal
        (failed — not cancelled): superseded evidence, no commit."""
        import tests.test_runs_harness_service as gl

        fake = gl.FakeGitLab()
        fake.seed_issue(gl.ISSUE_IID, gl.ISSUE_TITLE, gl.ISSUE_DESC)
        fake.seed_commit("main", "base-sha-1", "initial")
        service = gl.make_service(db, fake)
        run_id, pipeline_id, _branch = await gl.start_and_go(service, db, fake)
        gl.seed_success_with_candidate(fake, pipeline_id, attempt_base="base-sha-1")

        async with db() as session:
            await Controller(session).transition(
                run_id, FlowStatus.FAILED, reason="parked elsewhere"
            )
            await session.commit()

        await service._evaluate_harness_one(run_id, datetime.now(timezone.utc))

        run = await gl.get_run(db, run_id)
        assert run.status == FlowStatus.FAILED.value, "a late callback never terminalizes again"
        assert fake.calls_of("create_commit") == [], "nothing may publish for a terminal run"
        assert run.evidence["superseded"]["reason"] == "run already failed"
        assert run.evidence["superseded"]["attempt_base"] == "base-sha-1"

    async def test_github_actions_candidate_after_blocked_run_is_superseded(self, db):
        """GitHub lane: the Actions workflow finished AFTER the run went
        terminal (blocked — not cancelled): superseded evidence, no commit,
        no Draft PR."""
        import tests.test_github_harness as gh

        github = gh.FakeGitHub()
        github.seed_repo(gh.REPO, {"src/app.py": "print('hi')\n"})
        github.heads[gh.REPO]["main"] = gh.BASE_HEAD
        github.seed_issue(gh.REPO, gh.ISSUE, gh.ISSUE_TITLE, gh.ISSUE_DESC)
        service = gh.make_service(db, github, stack=gh.make_stack(github))
        run_id = await gh.start(service)
        await gh.go(service, run_id)
        gh.seed_success_with_candidate(github, run_id)

        async with db() as session:
            await Controller(session).transition(
                run_id, FlowStatus.BLOCKED, reason="parked elsewhere"
            )
            await session.commit()

        await service._evaluate_harness_one(run_id, datetime.now(timezone.utc))

        run = await gh.get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert github.calls_of("create_commit_on_branch") == []
        assert github.calls_of("create_draft_pr") == []
        assert run.evidence["superseded"]["reason"] == "run already blocked"
        assert run.evidence["superseded"]["attempt_base"] == gh.BASE_HEAD

    async def test_azure_pipelines_candidate_after_ready_run_is_superseded(self, db):
        """Azure DevOps lane: the lane's candidate arrives AFTER the run went
        terminal — superseded evidence, no push, no PR."""
        import tests.test_azure_runs as az

        azdo = az.FakeAzureDevOps()
        azdo.seed_work_item(az.WORK_ITEM, az.WORK_ITEM_TITLE, az.WORK_ITEM_DESC_HTML)
        settings = az.make_settings(FORGE_AZDO_LANE_PIPELINE_ID=az.LANE_PIPELINE_ID)
        service = az.make_service(db, azdo, settings=settings)
        run_id = await az.start(service)
        await az.go(service, run_id)
        run = await az.get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value

        async with db() as session:
            await Controller(session).transition(
                run_id, FlowStatus.BLOCKED, reason="parked elsewhere"
            )
            await session.commit()

        # The lane's candidate, fetched late — the consumer guard decides.
        bundle = parse_unified_diff(create_diff("forge-demo/x.md", "hello\n"), az.BASE_HEAD)
        outcome = HarnessOutcome.change_candidate(bundle)
        handle = az.AzurePipelinesHandle.from_json(run.evidence["harness"]["handle"])
        pushes = len(azdo.calls_of("push_commits"))

        await service._publish_harness_candidate(
            run_id, run.project_id, run.issue_iid or 0, outcome, handle
        )

        run = await az.get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert len(azdo.calls_of("push_commits")) == pushes
        assert azdo.pull_requests == []
        assert run.evidence["superseded"]["reason"] == "run already blocked"


# ----------------------------------------------------------------------
# A05: claim freshness at handler ENTRY — the runtime-side sibling of the
# publisher's reservation check. Queue ownership must imply effect ownership
# at every effect, the first one included: a claim that died while queued
# never reaches the handler, so no guarded mutation can even be attempted by
# a stale owner.
# ----------------------------------------------------------------------


class TestClaimFreshAtHandlerEntry:
    async def _bound_step(self, db) -> tuple[int, object]:
        """A scheduled step bound to a fresh accepted run; returns (step_id, run_id)."""
        run_id = await make_run(db)
        async with db() as session, session.begin():
            step = await schedule_command_step(
                session,
                {"command": "advance", "project_id": 1},
                source_event_id=uuid4().hex,
            )
            step.flow_run_id = run_id
            return step.id, run_id

    async def test_expired_claim_never_reaches_a_guarded_mutation(self, db, monkeypatch):
        """The lease died while the claim sat queued: the runtime skips the
        handler and requeues the step — the guarded transition the handler
        would have attempted is never even tried, and the run is untouched."""
        from forge.worker.steps import execute_claimed_step

        step_id, run_id = await self._bound_step(db)
        claimed = (await claim_due_steps(db, "worker-a"))[0]
        assert claimed.id == step_id

        async with db() as session, session.begin():
            await session.execute(
                update(StepRun)
                .where(StepRun.id == step_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )

        attempted: list[str] = []

        async def stale_handler(settings, forge_config, session_factory, metadata):
            # The first thing this handler would do is a guarded lifecycle
            # mutation — a stale owner must never get this far.
            async with session_factory() as session:
                await Controller(session).transition(run_id, FlowStatus.PREFLIGHT)
            attempted.append("preflight")

        monkeypatch.setattr("forge.worker.steps.execute_run_command", stale_handler)

        await execute_claimed_step(db, object(), object(), claimed)

        assert attempted == [], "a stale owner attempts no mutations"
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            step_row = await session.get(StepRun, step_id)
        assert run is not None and run.status == FlowStatus.ACCEPTED.value
        assert step_row is not None
        assert step_row.status == "scheduled", "the stale claim is requeued for a fresh owner"
        assert step_row.attempt == 1, "the expired-lease attempt is accounted like a reap"

    async def test_reassigned_claim_leaves_the_new_owner_running(self, db, monkeypatch):
        """The claim was reaped and another worker re-claimed it: the stale
        claim skips without touching the row — the new owner's lease, fence
        and attempt accounting are exactly as it left them."""
        from forge.worker.steps import execute_claimed_step

        step_id, _run_id = await self._bound_step(db)
        stale = (await claim_due_steps(db, "worker-a"))[0]

        async with db() as session, session.begin():
            await session.execute(
                update(StepRun)
                .where(StepRun.id == step_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        assert await reschedule_expired_leases(db) == 1
        fresh = (await claim_due_steps(db, "worker-b"))[0]
        assert fresh.fence_token == stale.fence_token + 1

        attempted: list[str] = []

        async def stale_handler(settings, forge_config, session_factory, metadata):
            attempted.append("entered")

        monkeypatch.setattr("forge.worker.steps.execute_run_command", stale_handler)

        await execute_claimed_step(db, object(), object(), stale)

        assert attempted == []
        async with db() as session:
            step_row = await session.get(StepRun, step_id)
        assert step_row is not None
        assert step_row.status == "running"
        assert step_row.lease_owner == "worker-b"
        assert step_row.fence_token == fresh.fence_token
        assert step_row.attempt == 1
