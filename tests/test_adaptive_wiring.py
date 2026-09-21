"""The service wiring tests — the adaptive substrate over the production seams.

DiscoveryService dispatches through the SAME harness leg; OperatorControlService
owns the mailbox + pause fence; WorkPackageCoordination drives child runs
phase-by-phase through a caller-supplied factory.
"""

from __future__ import annotations

import pytest

from forge.adaptive.discovery import DiscoveryRun
from forge.adaptive.wiring import (
    DiscoveryService,
    OperatorControlService,
    WorkPackageCoordination,
)
from forge.adaptive.workpackage import WorkItemRef, WorkPackage


def _discovery(**overrides: object) -> DiscoveryRun:
    values: dict = {
        "discovery_id": "disc-1",
        "work_id": "wp-1",
        "snapshot_set_digest": "a" * 64,
    }
    values.update(overrides)
    return DiscoveryRun(**values)


class TestDiscoveryService:
    async def test_begin_dispatches_through_the_harness_leg(self):
        dispatched: list[tuple[str, str, str]] = []

        async def harness(discovery_id, work_id, brief):
            dispatched.append((discovery_id, work_id, brief))

        service = DiscoveryService(harness_start=harness)
        discovery = await service.begin_discovery("wp-1", "a" * 64, "research brief")

        assert discovery.status == "running"
        assert len(dispatched) == 1
        assert dispatched[0][1] == "wp-1"
        assert dispatched[0][2] == "research brief"

    async def test_redispatch_after_a_resolved_question(self):
        dispatched: list[str] = []

        async def harness(discovery_id, work_id, brief):
            dispatched.append(discovery_id)

        service = DiscoveryService(harness_start=harness)
        waiting = _discovery(status="waiting_question", open_questions=("Q1",))

        resolved = await service.redispatch_after_question(waiting, "resume brief")

        assert resolved.status == "running"
        assert len(dispatched) == 1

    async def test_redispatch_refuses_a_non_waiting_discovery(self):
        async def harness(discovery_id, work_id, brief):
            pass

        service = DiscoveryService(harness_start=harness)
        with pytest.raises(ValueError, match="not waiting_question"):
            await service.redispatch_after_question(_discovery(), "brief")

    def test_resume_completed_keeps_the_durable_bundle(self):
        service = DiscoveryService(harness_start=None)  # type: ignore[arg-type]
        completed = _discovery(status="complete", evidence_bundle=("ev-1", "ev-2"))

        resumed = service.resume_completed(completed)

        assert resumed.evidence_bundle == ("ev-1", "ev-2")  # NOT repaid
        assert resumed.status == "complete"

    def test_block_on_critical(self):
        service = DiscoveryService(harness_start=None)  # type: ignore[arg-type]
        blocked = service.block_on_critical(
            _discovery(open_questions=("Q-critical",)), "unresolved critical"
        )
        assert blocked.status == "blocked"


class TestOperatorControlService:
    def _service(self) -> OperatorControlService:
        return OperatorControlService()

    def test_pause_fences_the_epoch_before_the_interrupt(self):
        svc = self._service()

        state = svc.pause("wp-1", "human:op", "note:1")

        # CTL-05's ordering: the fence closed (epoch bumped from 0) and the
        # interrupt was recorded
        assert state.publication_epoch == 1
        assert state.pause_requested is True
        assert state.interrupt_sent is True

    def test_a_second_pause_with_the_same_key_dedups(self):
        svc = self._service()

        svc.pause("wp-1", "human:op", "note:1")
        svc.pause("wp-1", "human:op", "note:1")

        # same idempotency key → the mailbox keeps ONE pause command
        pauses = [c for c in svc.mailbox.commands.values() if c.kind == "pause"]
        assert len(pauses) == 1

    def test_resume_requires_a_confirmed_checkpoint(self):
        svc = self._service()
        svc.pause("wp-1", "human:op", "note:1")

        # no checkpoint captured → resume refuses
        assert svc.resume("wp-1", "human:op", "note:2") is False

    def test_steer_rejects_acceptance_weakening(self):
        svc = self._service()

        result = svc.steer("wp-1", "human:op", "skip the test")

        assert result["status"] == "rejected"
        assert "revision gate" in result["reason"]

    def test_steer_delivers_guidance(self):
        svc = self._service()

        result = svc.steer("wp-1", "human:op", "fix the assertion first")

        assert result["status"] == "accepted"

    def test_answer_routes_through_the_mailbox(self):
        svc = self._service()

        created = svc.answer("wp-1", "human:op", "Q1", "use option A")

        assert created is True
        answers = [c for c in svc.mailbox.commands.values() if c.kind == "answer"]
        assert len(answers) == 1
        assert answers[0].payload["question_id"] == "Q1"

    def test_a_redelivered_answer_does_not_duplicate(self):
        svc = self._service()

        svc.answer("wp-1", "human:op", "Q1", "use A")
        created2 = svc.answer("wp-1", "human:op", "Q1", "use A")

        assert created2 is False  # same idempotency key — deduped


def _package() -> WorkPackage:
    return WorkPackage(
        package_id="pkg-1",
        objective="orders + billing",
        items=(
            WorkItemRef(item_id="I1", repository_id="orders"),
            WorkItemRef(item_id="I2", repository_id="billing", depends_on=("I1",)),
        ),
    )


class TestWorkPackageCoordination:
    async def test_start_launches_phase_one_only(self):
        launched: list[tuple[str, str]] = []

        async def factory(item_id, repo):
            launched.append((item_id, repo))
            return f"run-{item_id}"

        coord = WorkPackageCoordination(child_run_factory=factory)
        state = await coord.start(_package(), task_brief="do it")

        assert state["state"] == "running"
        assert state["current_phase"] == 0
        assert len(launched) == 1  # phase 1 = I1 only; I2 waits
        assert launched[0] == ("I1", "orders")
        assert state["child_runs"]["I1"]["run_id"] == "run-I1"

    async def test_advance_launches_the_next_phase(self):
        launched: list[str] = []

        async def factory(item_id, repo):
            launched.append(item_id)
            return f"run-{item_id}"

        coord = WorkPackageCoordination(child_run_factory=factory)
        state = await coord.start(_package(), task_brief="b")

        state = await coord.advance(state, _package(), task_brief="b")

        assert launched == ["I1", "I2"]  # phase 2 launched
        assert state["current_phase"] == 1

    async def test_advance_past_the_last_phase_completes(self):
        async def factory(item_id, repo):
            return f"run-{item_id}"

        coord = WorkPackageCoordination(child_run_factory=factory)
        state = await coord.start(_package(), task_brief="b")
        state = await coord.advance(state, _package(), task_brief="b")
        state = await coord.advance(state, _package(), task_brief="b")

        assert state["state"] == "complete"

    async def test_a_failed_child_holds_the_later_phases(self):
        launched: list[str] = []

        async def factory(item_id, repo):
            launched.append(item_id)
            return f"run-{item_id}"

        coord = WorkPackageCoordination(child_run_factory=factory)
        state = await coord.start(_package(), task_brief="b")

        state = await coord.child_failed(state, "I1", "tests failed")

        assert state["state"] == "failed"
        # advancing a failed coordination launches nothing more
        before = len(launched)
        state = await coord.advance(state, _package(), task_brief="b")
        assert len(launched) == before  # no cascade

    async def test_an_invalid_package_refuses_to_start(self):
        async def factory(item_id, repo):
            return "run"

        bad = WorkPackage(
            package_id="bad",
            objective="",
            items=(),
        )
        coord = WorkPackageCoordination(child_run_factory=factory)
        with pytest.raises(ValueError, match="invalid"):
            await coord.start(bad, task_brief="b")
