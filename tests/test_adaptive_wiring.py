"""The service wiring tests — the adaptive substrate over the production seams.

DiscoveryService dispatches through the SAME harness leg; OperatorControlService
owns the mailbox + pause fence; WorkPackageCoordination drives child runs
phase-by-phase through a caller-supplied factory.
"""

from __future__ import annotations

import hashlib

import pytest

from forge.adaptive.adapters import ClaudeSDKAdapter
from forge.adaptive.discovery import DiscoveryRun
from forge.adaptive.lane_control import LaneSteeringSession
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

    async def test_pause_fences_the_epoch_before_the_interrupt(self):
        svc = self._service()

        state = await svc.pause("wp-1", "human:op", "note:1")

        # CTL-05's ordering: the fence closed (epoch bumped from 0) and the
        # interrupt was recorded
        assert state.publication_epoch == 1
        assert state.pause_requested is True
        assert state.interrupt_sent is True

    async def test_a_second_pause_with_the_same_key_dedups(self):
        svc = self._service()

        await svc.pause("wp-1", "human:op", "note:1")
        await svc.pause("wp-1", "human:op", "note:1")

        # same idempotency key → the mailbox keeps ONE pause command
        pauses = [c for c in svc.mailbox.commands.values() if c.kind == "pause"]
        assert len(pauses) == 1

    async def test_resume_requires_a_confirmed_checkpoint(self):
        svc = self._service()
        await svc.pause("wp-1", "human:op", "note:1")

        # no checkpoint captured → resume refuses
        assert await svc.resume("wp-1", "human:op", "note:2") is False

    async def test_resume_embeds_the_exact_resume_spec_from_the_durable_store(
        self, tmp_path, monkeypatch
    ):
        """NEXT-03: the resume command carries the ResumeSpec of the
        store's CURRENT active checkpoint — the exact reference, its
        sequence and the manifest's declared source — read from the
        durable checkpoint store, never from the pending-command queue."""
        import json

        from forge.api_checkpoint_channel import (
            CHECKPOINT_STORE_DIR_ENV,
            CheckpointStore,
        )

        def _digest(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        monkeypatch.setenv(CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "cp-store"))
        store = CheckpointStore(tmp_path / "cp-store")

        def _checkpoint(files: dict[str, bytes], sequence: int, source_oid: str) -> str:
            manifest = json.dumps(
                {
                    "schema": "forge.wip.manifest/2",
                    "work_id": "wp-1",
                    "sequence": sequence,
                    "source_oids": {"attempt_base": source_oid},
                    "files": {
                        name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                        for name, data in sorted(files.items())
                    },
                    "deletions": [],
                }
            ).encode()
            blobs = {
                entry["digest"]: files[name]
                for name, entry in json.loads(manifest)["files"].items()
            }
            store.put_checkpoint(
                work_id="wp-1",
                manifest_bytes=manifest,
                blobs=blobs,
                sequence=sequence,
            )
            return hashlib.sha256(manifest).hexdigest()

        older = _checkpoint({"a.py": b"v1\n"}, 3, "b" * 40)
        newer = _checkpoint({"a.py": b"v2\n"}, 7, "c" * 40)  # the ACTIVE one

        svc = self._service()
        assert await svc.resume("wp-1", "human:op", "note:9") is True

        resumes = [c for c in svc.mailbox.commands.values() if c.kind == "resume"]
        assert len(resumes) == 1
        payload = resumes[0].payload
        assert payload["checkpoint_ref"] == f"wp-1@{newer}"
        assert payload["checkpoint_sequence"] == 7
        assert payload["source_oid"] == "c" * 40
        assert older != newer  # the ACTIVE one won, not the first arrival

    async def test_resume_without_any_store_still_gates_on_the_in_memory_capture(self):
        """No store configured: the CTL-06 in-memory gate still applies and
        the command carries an empty payload (the legacy spelling)."""
        svc = self._service()
        await svc.pause("wp-1", "human:op", "note:1")
        from dataclasses import replace

        svc.pause_states["wp-1"] = replace(svc.pause_states["wp-1"], checkpoint_captured=True)

        assert await svc.resume("wp-1", "human:op", "note:2") is True
        (resume,) = [c for c in svc.mailbox.commands.values() if c.kind == "resume"]
        assert resume.payload == {}

    async def test_steer_rejects_acceptance_weakening(self):
        svc = self._service()

        result = await svc.steer("wp-1", "human:op", "skip the test")

        assert result["status"] == "rejected"
        assert "revision gate" in result["reason"]

    async def test_steer_delivers_guidance(self):
        svc = self._service()

        result = await svc.steer("wp-1", "human:op", "fix the assertion first")

        assert result["status"] == "accepted"

    async def test_answer_routes_through_the_mailbox(self):
        svc = self._service()

        created = await svc.answer("wp-1", "human:op", "Q1", "use option A")

        assert created is True
        answers = [c for c in svc.mailbox.commands.values() if c.kind == "answer"]
        assert len(answers) == 1
        assert answers[0].payload["question_id"] == "Q1"

    async def test_a_redelivered_answer_does_not_duplicate(self):
        svc = self._service()

        await svc.answer("wp-1", "human:op", "Q1", "use A")
        created2 = await svc.answer("wp-1", "human:op", "Q1", "use A")

        assert created2 is False  # same idempotency key — deduped


class _FakeClaudeClient:
    """Duck-typed ClaudeSDKClient for the service -> lane flow tests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_session(self, task: str) -> str:
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("steer", session_id, text))

    async def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    async def query(self, session_id: str) -> list[dict]:
        return []


class TestSteerMailboxWiring:
    """The service records accepted steers; a running lane drains them."""

    async def test_an_accepted_steer_records_a_mailbox_command(self):
        svc = OperatorControlService()

        result = await svc.steer("wp-1", "human:op", "fix the assertion first")

        assert result["status"] == "accepted"
        command = svc.mailbox.commands[result["command_id"]]
        assert command.kind == "steer"
        assert command.status == "received"
        assert command.payload["text"] == "fix the assertion first"

    async def test_a_rejected_steer_never_reaches_the_mailbox(self):
        svc = OperatorControlService()

        result = await svc.steer("wp-1", "human:op", "skip the tests")

        assert result["status"] == "rejected"
        assert svc.mailbox.commands == {}

    async def test_a_steer_scopes_to_a_run_id(self):
        svc = OperatorControlService()

        await svc.steer("wp-1", "human:op", "note", run_id="run-7")

        assert (await svc.pending("wp-1"))[0].payload["run_id"] == "run-7"

    async def test_pending_lists_received_commands_in_sequence_order(self):
        svc = OperatorControlService()

        first = await svc.steer("wp-1", "human:op", "first")
        second = await svc.steer("wp-1", "human:op", "second")

        assert [c.command_id for c in await svc.pending("wp-1")] == [
            first["command_id"],
            second["command_id"],
        ]

    async def test_an_accepted_steer_flows_to_a_running_lane_session(self):
        svc = OperatorControlService()
        client = _FakeClaudeClient()
        session = LaneSteeringSession(
            service=svc,
            driver=ClaudeSDKAdapter(client=client),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
        )

        await svc.steer("wp-1", "human:op", "prefer the existing helper", run_id="run-1")
        actions = await session.drain_once()

        assert client.calls == [("steer", "sess-1", "prefer the existing helper")]
        assert [a.outcome for a in actions] == ["applied"]

    async def test_a_lane_session_ignores_a_command_scoped_to_another_run(self):
        svc = OperatorControlService()
        client = _FakeClaudeClient()
        session = LaneSteeringSession(
            service=svc,
            driver=ClaudeSDKAdapter(client=client),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
        )

        await svc.steer("wp-1", "human:op", "for the other lane", run_id="run-9")
        actions = await session.drain_once()

        assert client.calls == []
        assert [a.outcome for a in actions] == ["ignored"]
        # the command stays in the mailbox for its owning lane
        assert (await svc.pending("wp-1"))[0].status == "received"


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
