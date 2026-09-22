"""R28-19: WorkPackage coordination over EXISTING repository runs.

The NXT-23 tests (``test_adaptive_workpackage.py``) pinned the durable
coordinator against an abstract factory seam. These pin the
operational half: the children are REAL FlowRun rows (created the way
``start_run``'s durable core creates them), the outcomes are read from
those rows' CURRENT statuses, and a phase advances only when ALL of its
children reached terminal rows — the review's "a real parent work item
driving two provider-bound child runs".
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge.adaptive.workpackage import (
    PhaseAdvanceRefused,
    WorkItemRef,
    WorkPackageStateError,
    compile_dependencies,
    read_workpackage_state,
)
from forge.adaptive.workpackage_service import (
    ChildOutcomeEvidence,
    ChildSubject,
    WorkPackageService,
    child_run_id_for_intent,
    outcome_evidence_of,
    package_from_plan,
    run_service_starter,
    start_child_run_row,
)
from forge.durable import FlowRun
from forge.models.base import Base

PARENT_RUN = "run-parent-1"

SUBJECTS = {
    "widgets": ChildSubject(project_id=101, issue_iid=11),
    "consumers": ChildSubject(project_id=102, issue_iid=12),
    "contracts": ChildSubject(project_id=103, issue_iid=13),
}


def _digest(seed: str) -> str:
    return (seed * 64)[:64]


def _sha(seed: str) -> str:
    return (seed * 40)[:40]


def _api_item(writable: bool = True) -> WorkItemRef:
    return WorkItemRef(item_id="api", repository_id="widgets", writable=writable)


def _plan_items() -> list[list[WorkItemRef]]:
    """The review's shape: TWO phases, one provider-bound child each."""
    return [
        [_api_item()],
        [WorkItemRef(item_id="consumer", repository_id="consumers")],
    ]


def _service(factory, **kwargs) -> WorkPackageService:
    return WorkPackageService(factory, subject_of=SUBJECTS, **kwargs)


async def _process(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """A fresh engine + session factory over the SAME file = a restart."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _seed_parent(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(FlowRun(id=PARENT_RUN, project_id=1, status="planning"))
        await session.commit()


async def _child_rows(factory: async_sessionmaker[AsyncSession]) -> list[FlowRun]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(FlowRun).where(FlowRun.id != PARENT_RUN).order_by(FlowRun.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _child_row(factory: async_sessionmaker[AsyncSession], run_id: str) -> FlowRun | None:
    async with factory() as session:
        return await session.get(FlowRun, run_id)


async def _finish_child(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    status: str,
    *,
    status_reason: str = "",
    tested_world_digest: str | None = None,
    candidate_sha: str = "",
    driver_exit: str = "",
) -> None:
    """Drive a child run row to a terminal state with journaled evidence."""
    evidence: dict = {}
    if tested_world_digest is not None:
        evidence["tested_world_digest"] = tested_world_digest
    if driver_exit:
        evidence["harness"] = {"driver_exit": driver_exit}
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None, run_id
        run.status = status
        run.status_reason = status_reason
        run.evidence = {**(run.evidence or {}), **evidence}
        if candidate_sha:
            run.candidate_shas = [candidate_sha]
        await session.commit()


async def _state(factory: async_sessionmaker[AsyncSession]) -> dict:
    return await read_workpackage_state(factory, PARENT_RUN) or {}


# ---------------------------------------------------------------------------
# package_from_plan — a multi-phase plan becomes the parent package
# ---------------------------------------------------------------------------


class TestPackageFromPlan:
    def test_a_two_phase_plan_reproduces_itself(self):
        package = package_from_plan("pkg-1", "Widen the API", _plan_items())
        assert compile_dependencies(list(package.items)) == [["api"], ["consumer"]]
        assert package.validate() == []

    def test_phase_order_is_derived_not_trusted(self):
        # items may carry whatever depends_on they like — the PLAN's
        # phases are the authority, and the derived package says so.
        noisy = [
            [WorkItemRef(item_id="api", repository_id="widgets", depends_on=("consumer",))],
            [WorkItemRef(item_id="consumer", repository_id="consumers", depends_on=("ghost",))],
        ]
        package = package_from_plan("pkg-1", "objective", noisy)
        assert compile_dependencies(list(package.items)) == [["api"], ["consumer"]]

    def test_a_single_phase_plan_is_refused(self):
        with pytest.raises(ValueError, match="MULTI-phase"):
            package_from_plan("pkg-1", "one phase", [[_api_item()]])

    def test_an_empty_phase_is_refused(self):
        with pytest.raises(ValueError, match="empty phase"):
            package_from_plan("pkg-1", "holey", [[_api_item()], []])

    def test_read_only_context_repositories_ride_along(self):
        package = package_from_plan(
            "pkg-1", "objective", _plan_items(), read_only_repositories=("runbooks",)
        )
        assert package.reader_repos() == {"runbooks"}


# ---------------------------------------------------------------------------
# dispatch — the children are REAL FlowRun rows
# ---------------------------------------------------------------------------


class TestRealRunDispatch:
    async def test_start_dispatches_phase_zero_as_real_flow_runs(self, tmp_path):
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        service = _service(factory)
        try:
            await service.start_package(
                package_from_plan("pkg-1", "Widen the API", _plan_items()),
                parent_run_id=PARENT_RUN,
                task_brief="Widen the API",
            )
            rows = await _child_rows(factory)
            assert len(rows) == 1  # phase 1 dispatches only after phase 0 PROVES
            child = rows[0]
            assert child.id == child_run_id_for_intent("wp-start:pkg-1:api:0")
            assert child.status == "waiting_harness"
            assert child.provider == "github"
            assert child.project_id == 101
            assert child.issue_iid == 11
            assert child.evidence["workpackage_intent"] == "wp-start:pkg-1:api:0"
            state = await _state(factory)
            assert state["children"]["api"]["child_run_id"] == child.id
            assert state["children"]["api"]["intent_status"] == "launched"
        finally:
            await engine.dispose()

    async def test_read_only_items_get_snapshots_not_runs(self, tmp_path):
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        service = _service(factory)
        plan = [
            [
                _api_item(),
                WorkItemRef(item_id="contract-check", repository_id="contracts", writable=False),
            ],
            [WorkItemRef(item_id="consumer", repository_id="consumers")],
        ]
        try:
            await service.start_package(
                package_from_plan("pkg-1", "objective", plan),
                parent_run_id=PARENT_RUN,
                task_brief="b",
            )
            rows = await _child_rows(factory)
            assert [row.id for row in rows] == [
                child_run_id_for_intent("wp-start:pkg-1:api:0")
            ]  # the snapshot executed nothing — no row for it
            state = await _state(factory)
            assert state["children"]["contract-check"]["kind"] == "reference_snapshot"
        finally:
            await engine.dispose()

    async def test_crash_between_intent_and_dispatch_replays_one_child(self, tmp_path):
        # The review's recovery criterion: a crash after the intent was
        # committed but before the dispatch completed recovers the SAME
        # child, never a second run.
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        calls = {"n": 0}

        async def crashing(item_id, repository_id, *, writable, intent_key, brief):
            await start_child_run_row(  # the row exists before the "crash"
                factory,
                SUBJECTS,
                item_id=item_id,
                repository_id=repository_id,
                intent_key=intent_key,
                brief=brief,
            )
            calls["n"] += 1
            raise RuntimeError("process died after the child row existed, link unsaved")

        service = WorkPackageService(factory, run_starter=crashing)
        package = package_from_plan("pkg-1", "objective", _plan_items())
        try:
            with pytest.raises(RuntimeError, match="link unsaved"):
                await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            # the intent is durable — the crash window opened with it saved
            assert (await _state(factory))["children"]["api"]["intent_status"] == "intended"

            # replay over the SAME database: the deterministic id ADOPTS
            # the row the crashed process created — exactly ONE child.
            replay = _service(factory)
            await replay.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            rows = await _child_rows(factory)
            assert len(rows) == 1
            assert rows[0].id == child_run_id_for_intent("wp-start:pkg-1:api:0")
            assert calls["n"] == 1  # the row was created exactly once
        finally:
            await engine.dispose()

    async def test_crash_before_the_row_exists_also_converges(self, tmp_path):
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)

        async def crashing(item_id, repository_id, *, writable, intent_key, brief):
            raise RuntimeError("process died before the factory created the child")

        service = WorkPackageService(factory, run_starter=crashing)
        package = package_from_plan("pkg-1", "objective", _plan_items())
        try:
            with pytest.raises(RuntimeError, match="before the factory created"):
                await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            await _service(factory).start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            assert len(await _child_rows(factory)) == 1
        finally:
            await engine.dispose()

    async def test_a_real_start_run_composes_through_the_adapter(self, tmp_path):
        # The dispatch seam matches the GitHub start_run shape; the
        # service's one-active-run-per-subject idempotency is INHERITED
        # from the adapted service, exactly as from the default starter.
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        started: list[dict] = []

        class FakeGitHubService:
            async def start_run(
                self,
                *,
                project_id: int,
                issue_number: int,
                issue_title: str,
                issue_description: str,
                author_username: str,
            ) -> str:
                active = [
                    row
                    for row in await _child_rows(factory)
                    if row.project_id == project_id and row.issue_iid == issue_number
                ]
                if active:  # one active run per subject — the real invariant
                    return active[0].id
                started.append({"project_id": project_id, "issue": issue_number})
                run_id = uuid4().hex
                async with factory() as session:
                    session.add(
                        FlowRun(
                            id=run_id,
                            project_id=project_id,
                            issue_iid=issue_number,
                            provider="github",
                            status="waiting_harness",
                        )
                    )
                    await session.commit()
                return run_id

        service = WorkPackageService(
            factory, run_starter=run_service_starter(FakeGitHubService().start_run, SUBJECTS)
        )
        package = package_from_plan("pkg-1", "objective", _plan_items())
        try:
            await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            rows = await _child_rows(factory)
            assert len(rows) == 1
            first_id = rows[0].id

            # the crashed-link replay: start_run is called again, the
            # active run is adopted, still ONE row with the SAME id.
            await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            rows_after = await _child_rows(factory)
            assert [row.id for row in rows_after] == [first_id]
            assert len(started) == 1
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# advance_from_children — outcomes read from the FlowRun rows
# ---------------------------------------------------------------------------


class TestAdvanceFromChildren:
    async def _started(self, tmp_path, *, world: str | None = None):
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        service = _service(factory)
        package = package_from_plan("pkg-1", "Widen the API", _plan_items())
        await service.start_package(
            package,
            parent_run_id=PARENT_RUN,
            task_brief="Widen the API",
            tested_world_digest=world,
        )
        child_id = (await _state(factory))["children"]["api"]["child_run_id"]
        return factory, engine, service, package, child_id

    async def test_child1_success_advances_and_dispatches_child2(self, tmp_path):
        world = _digest("w")
        factory, engine, service, package, child_id = await self._started(tmp_path, world=world)
        try:
            await _finish_child(
                factory,
                child_id,
                "ready_for_human",
                status_reason="verified candidate",
                tested_world_digest=world,
                candidate_sha=_sha("c"),
                driver_exit="completed",
            )
            state = await service.advance_from_children(PARENT_RUN, package)
            assert state["current_phase"] == 1  # the ONLY advance trigger
            assert state["children"]["api"]["outcome"]["status"] == "succeeded"

            rows = await _child_rows(factory)
            assert len(rows) == 2  # child 2 dispatched as a REAL run
            assert state["children"]["consumer"]["child_run_id"] == rows[1].id
            assert state["children"]["consumer"]["child_run_id"] != child_id

            # the per-child evidence landed in the durable outcome
            evidence = state["children"]["api"]["outcome"]["evidence"]
            assert evidence["candidate_sha"] == _sha("c")
            assert evidence["exit"] == "completed"
            assert evidence["tested_world_digest"] == world
            assert evidence["run_status"] == "ready_for_human"
        finally:
            await engine.dispose()

    async def test_a_failed_child_refuses_the_advance(self, tmp_path):
        factory, engine, service, package, child_id = await self._started(tmp_path)
        try:
            await _finish_child(factory, child_id, "failed", status_reason="tests are red")
            with pytest.raises(PhaseAdvanceRefused) as excinfo:
                await service.advance_from_children(PARENT_RUN, package)
            assert excinfo.value.failed == ("api",)
            assert len(await _child_rows(factory)) == 1  # consumer never dispatched
            state = await _state(factory)
            assert state["state"] == "failed"  # the coordinator flipped it
            assert state["failed_item"] == "api"
            assert "consumer" not in state["children"]  # never dispatched
        finally:
            await engine.dispose()

    async def test_a_still_running_child_awaits(self, tmp_path):
        factory, engine, service, package, child_id = await self._started(tmp_path)
        try:
            with pytest.raises(PhaseAdvanceRefused) as excinfo:
                await service.advance_from_children(PARENT_RUN, package)
            assert excinfo.value.awaiting == ("api",)
            assert len(await _child_rows(factory)) == 1  # nothing new dispatched
            assert (await _state(factory))["current_phase"] == 0
        finally:
            await engine.dispose()

    async def test_a_phase_needs_all_children_terminal(self, tmp_path):
        # TWO provider-bound children in phase 0: one terminal, one still
        # running — the phase does not move on a partial outcome.
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        service = _service(factory)
        plan = [
            [
                _api_item(),
                WorkItemRef(item_id="contract-check", repository_id="contracts"),
            ],
            [WorkItemRef(item_id="consumer", repository_id="consumers")],
        ]
        package = package_from_plan("pkg-1", "objective", plan)
        try:
            await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            state = await _state(factory)
            api_id = state["children"]["api"]["child_run_id"]
            check_id = state["children"]["contract-check"]["child_run_id"]
            await _finish_child(factory, api_id, "ready_for_human")
            with pytest.raises(PhaseAdvanceRefused) as excinfo:
                await service.advance_from_children(PARENT_RUN, package)
            assert excinfo.value.awaiting == ("contract-check",)
            assert (await _state(factory))["current_phase"] == 0

            # the terminal sibling's outcome IS recorded (partial ≠ none)
            outcome = (await _state(factory))["children"]["api"]["outcome"]
            assert outcome["status"] == "succeeded"

            await _finish_child(factory, check_id, "ready_for_human")
            state = await service.advance_from_children(PARENT_RUN, package)
            assert state["current_phase"] == 1
        finally:
            await engine.dispose()

    async def test_outcomes_from_another_world_prove_nothing(self, tmp_path):
        active = _digest("a")
        factory, engine, service, package, child_id = await self._started(tmp_path, world=active)
        try:
            await _finish_child(
                factory,
                child_id,
                "ready_for_human",
                tested_world_digest=_digest("z"),  # the PREVIOUS world
            )
            with pytest.raises(PhaseAdvanceRefused):
                await service.advance_from_children(PARENT_RUN, package)
            state = await _state(factory)
            assert state["children"]["api"]["outcome"] is None  # nothing recorded
            assert len(await _child_rows(factory)) == 1
        finally:
            await engine.dispose()

    async def test_a_launched_link_without_its_row_is_loud(self, tmp_path):
        factory, engine, service, package, child_id = await self._started(tmp_path)
        try:
            async with factory() as session:
                run = await session.get(FlowRun, child_id)
                assert run is not None
                await session.delete(run)
                await session.commit()
            with pytest.raises(WorkPackageStateError, match="corruption"):
                await service.advance_from_children(PARENT_RUN, package)
        finally:
            await engine.dispose()

    async def test_the_final_phase_completes_the_package(self, tmp_path):
        factory, engine, service, package, child_id = await self._started(tmp_path)
        try:
            await _finish_child(factory, child_id, "ready_for_human")
            await service.advance_from_children(PARENT_RUN, package)
            state = await _state(factory)
            consumer_id = state["children"]["consumer"]["child_run_id"]
            await _finish_child(factory, consumer_id, "ready_for_human")
            done = await service.advance_from_children(PARENT_RUN, package)
            assert done["state"] == "complete"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# package_status — links and reasons for every child
# ---------------------------------------------------------------------------


class TestPackageStatus:
    async def test_links_reasons_and_partial_outcomes_for_every_child(self, tmp_path):
        factory, engine = await _process(tmp_path / "wp.db")
        await _seed_parent(factory)
        service = _service(factory)
        plan = [
            [
                _api_item(),
                WorkItemRef(item_id="contract-check", repository_id="contracts"),
            ],
            [WorkItemRef(item_id="consumer", repository_id="consumers")],
        ]
        package = package_from_plan("pkg-1", "objective", plan)
        try:
            await service.start_package(package, parent_run_id=PARENT_RUN, task_brief="b")
            state = await _state(factory)
            await _finish_child(
                factory,
                state["children"]["api"]["child_run_id"],
                "ready_for_human",
                status_reason="verified candidate",
            )
            # A reconcile that cannot advance still RECORDS the terminal
            # sibling — the snapshot must show that partial outcome.
            with pytest.raises(PhaseAdvanceRefused):
                await service.advance_from_children(PARENT_RUN, package)
            snapshot = await service.package_status(PARENT_RUN)
            assert snapshot["package_id"] == "pkg-1"
            assert snapshot["state"] == "running"
            by_item = {child["item_id"]: child for child in snapshot["children"]}
            assert set(by_item) == {"api", "contract-check"}  # every DISPATCHED child
            api = by_item["api"]
            assert api["child_run_id"] == state["children"]["api"]["child_run_id"]
            assert api["run_status"] == "ready_for_human"  # LIVE from the row
            assert api["status_reason"] == "verified candidate"
            assert api["outcome"]["status"] == "succeeded"  # reconciled on read
            check = by_item["contract-check"]
            assert check["run_status"] == "waiting_harness"
            assert check["outcome"] is None  # partial outcome, shown as partial
            # phase 1 never dispatched — the package names no link for it yet
            assert "consumer" not in by_item
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# outcome_evidence_of — the child evidence contract
# ---------------------------------------------------------------------------


class TestOutcomeEvidence:
    def test_reads_the_three_fields_off_a_run_row(self):
        run = FlowRun(
            id="x",
            project_id=1,
            candidate_shas=[_sha("old"), _sha("new")],
            evidence={
                "tested_world_digest": _digest("w"),
                "harness": {"driver_exit": "completed"},
            },
        )
        extracted = outcome_evidence_of(run)
        assert extracted.tested_world_digest == _digest("w")
        assert extracted.candidate_sha == _sha("new")  # the LATEST candidate
        assert extracted.exit == "completed"

    def test_the_published_candidate_fallback_and_top_level_exit(self):
        run = FlowRun(
            id="x",
            project_id=1,
            evidence={
                "published_candidate": {"sha": _sha("p")},
                "driver_exit": "failed",
            },
        )
        extracted = outcome_evidence_of(run)
        assert extracted.candidate_sha == _sha("p")
        assert extracted.exit == "failed"

    def test_unknown_stays_unknown_never_guessed(self):
        run = FlowRun(id="x", project_id=1)
        extracted = outcome_evidence_of(run)
        assert extracted == ChildOutcomeEvidence("", "", "")
