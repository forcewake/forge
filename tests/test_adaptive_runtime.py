"""The EXE epic core runtime substrate — protocol, matrix, checkpoints.

EXE-01/EXE-03/EXE-05 in tests: the honest capability boundary (a batch
runtime never claims interactivity), the checkpoint portability rules
(filesystem persistence is NOT conversational persistence), and the
bounded episode loop that applies control only at checkpoint boundaries.
"""

from __future__ import annotations

import pytest

from forge.adaptive.runtime import (
    BatchController,
    CapabilityMatrix,
    CheckpointRestartRuntime,
    ControlOp,
    HarnessRuntime,
    RuntimeEvent,
    SessionRestorer,
    portable_checkpoint,
    rehydrate,
)


class _NanoRuntime:
    """The smallest honest runtime: exports checkpoints, nothing else.

    Satisfies HarnessRuntime structurally — proof the protocol is
    implementable without inheriting any interactivity.
    """

    def __init__(self) -> None:
        self._task = ""

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"checkpoint_export"})

    async def start(self, task: str, *, profile_id: str) -> None:
        self._task = task

    async def send_control(self, op: ControlOp) -> None:
        del op  # a nano runtime has no control surface at all

    async def events(self) -> list[RuntimeEvent]:
        return []

    async def checkpoint(self) -> dict:
        return {"task": self._task}

    async def restore(self, state: dict) -> None:
        self._task = state["task"]


class TestProtocolShape:
    async def test_minimal_runtime_constructs_and_satisfies_the_protocol(self):
        nano = _NanoRuntime()
        await nano.start("t", profile_id="nano")
        assert isinstance(nano, HarnessRuntime)

    async def test_reference_runtime_satisfies_its_own_protocol(self):
        assert isinstance(CheckpointRestartRuntime(), HarnessRuntime)


class TestControlShapes:
    def test_control_op_rejects_unknown_op(self):
        with pytest.raises(ValueError, match="unknown control op"):
            ControlOp(op="panic")

    def test_runtime_event_rejects_unknown_kind(self):
        with pytest.raises(ValueError, match="unknown event kind"):
            RuntimeEvent(kind="explosion")

    def test_runtime_event_payload_defaults_to_empty_dict(self):
        assert RuntimeEvent(kind="usage").payload == {}


class TestCapabilityMatrix:
    def test_unknown_profile_supports_nothing(self):
        matrix = CapabilityMatrix()
        assert matrix.supports("never-registered", "checkpoint_export") is False

    def test_interactive_vs_checkpoint_only_classification(self):
        matrix = CapabilityMatrix()
        matrix.register("claude-sdk", {"interrupt", "live_input", "questions", "checkpoint_export"})
        matrix.register("batch-cli", {"checkpoint_export"})
        matrix.register("questions-only", {"questions"})

        assert matrix.interactive_profiles() == ["claude-sdk"]
        assert matrix.checkpoint_only("batch-cli") is True
        # An interactive profile that also checkpoints is NOT checkpoint-only.
        assert matrix.checkpoint_only("claude-sdk") is False
        assert matrix.checkpoint_only("questions-only") is False
        assert matrix.checkpoint_only("never-registered") is False
        assert matrix.supports("batch-cli", "interrupt") is False

    def test_interrupt_without_live_input_is_not_interactive(self):
        matrix = CapabilityMatrix()
        matrix.register("half-interactive", {"interrupt", "checkpoint_export"})
        assert matrix.interactive_profiles() == []

    def test_registration_rejects_capabilities_consumers_cannot_degrade_for(self):
        matrix = CapabilityMatrix()
        with pytest.raises(ValueError, match="unknown capabilities"):
            matrix.register("typo-profile", {"checkpoint_export", "interupt"})


class TestCheckpointRestartRuntime:
    async def test_pause_applies_at_next_checkpoint_not_immediately(self):
        runtime = CheckpointRestartRuntime()
        await runtime.start("fix the flaky test", profile_id="batch-cli")
        await runtime.send_control(ControlOp(op="pause"))

        # Queued, not applied: no checkpoint_ready before a boundary.
        drained = await runtime.events()
        assert all(event.kind != "checkpoint_ready" for event in drained)

        await runtime.checkpoint()
        drained = await runtime.events()
        assert [event.kind for event in drained] == ["checkpoint_ready"]

    async def test_capabilities_are_checkpoint_export_only(self):
        runtime = CheckpointRestartRuntime()
        assert runtime.capabilities == frozenset({"checkpoint_export"})

    async def test_checkpoint_restore_round_trip(self):
        runtime = CheckpointRestartRuntime()
        await runtime.start("fix the flaky test", profile_id="batch-cli")

        state = await runtime.checkpoint()
        assert state == {
            "task": "fix the flaky test",
            "pending_control": {"op": "none", "payload": ""},
            "profile_id": "batch-cli",
            "state": "checkpoint-restart",
        }

        revived = CheckpointRestartRuntime()
        await revived.restore(state)
        assert await revived.checkpoint() == state

    async def test_queued_pause_is_consumed_by_the_checkpoint_that_applies_it(self):
        runtime = CheckpointRestartRuntime()
        await runtime.start("t", profile_id="batch-cli")
        await runtime.send_control(ControlOp(op="pause"))

        state = await runtime.checkpoint()
        # The pause landed HERE — nothing stays queued past the boundary.
        assert state["pending_control"] == {"op": "none", "payload": ""}

    async def test_foreign_restore_refused(self):
        runtime = CheckpointRestartRuntime()
        with pytest.raises(ValueError, match="foreign checkpoint shape"):
            await runtime.restore({"schema": "forge.checkpoint.portable/1"})
        with pytest.raises(ValueError, match="foreign checkpoint state marker"):
            await runtime.restore(
                {
                    "task": "t",
                    "pending_control": {"op": "none", "payload": ""},
                    "profile_id": "batch-cli",
                    "state": "someone-elses-runtime",
                }
            )


class TestSessionRestorer:
    def test_native_session_requires_pinned_interactive_profile(self):
        restorer = SessionRestorer()
        state = {"task": "t", "profile_id": "claude-sdk", "native_session": {"sid": "n-1"}}
        assert restorer.restorable(state, pinned_profile="claude-sdk") == (True, "native")

    def test_native_session_without_matching_pin_reconstructs(self):
        restorer = SessionRestorer()
        state = {"task": "t", "profile_id": "claude-sdk", "native_session": {"sid": "n-1"}}
        # Wrong pin or no pin at all: durable artifacts carry the work.
        assert restorer.restorable(state, pinned_profile="codex-app") == (
            True,
            "reconstructed",
        )
        assert restorer.restorable(state, pinned_profile=None) == (True, "reconstructed")

    def test_native_session_on_a_non_interactive_profile_reconstructs(self):
        restorer = SessionRestorer()
        state = {"task": "t", "profile_id": "batch-cli", "native_session": {"sid": "n-1"}}
        assert restorer.restorable(state, pinned_profile="batch-cli") == (
            True,
            "reconstructed",
        )

    def test_generic_restore_via_durable_artifacts(self):
        restorer = SessionRestorer()
        state = {"task": "t", "profile_id": "batch-cli"}
        assert restorer.restorable(state, pinned_profile=None) == (True, "durable")
        assert restorer.restorable(state, pinned_profile="batch-cli") == (True, "durable")

    def test_plain_checkpoint_with_mismatched_pin_is_refused(self):
        restorer = SessionRestorer()
        state = {"task": "t", "profile_id": "batch-cli"}
        ok, reason = restorer.restorable(state, pinned_profile="claude-sdk")
        assert ok is False
        assert "does not match" in reason

    def test_unrestorable_shape(self):
        restorer = SessionRestorer()
        for broken in ({}, {"task": "no profile"}, {"profile_id": "no task"}):
            ok, reason = restorer.restorable(broken, pinned_profile="claude-sdk")
            assert ok is False, reason


class TestPortableCheckpoint:
    def test_native_session_is_optional(self):
        without = portable_checkpoint({"src/a.py": "x"}, {"plan_revision": 3})
        assert without["native_session"] is None

        with_native = portable_checkpoint({"src/a.py": "x"}, {}, native_session={"sid": "n-1"})
        assert with_native["native_session"] == {"sid": "n-1"}

    def test_schema_tag_and_snapshot_copy(self):
        files = {"src/a.py": "print('a')"}
        bundle = portable_checkpoint(files, {"work_id": "w"})
        assert bundle["schema"] == "forge.checkpoint.portable/1"
        assert bundle["workspace"] == files
        assert bundle["metadata"] == {"work_id": "w"}
        # A snapshot, not an alias — later edits must not rewrite history.
        assert bundle["workspace"] is not files


class TestRehydrate:
    def test_round_trip(self, tmp_path):
        scratch = tmp_path / "fresh-workspace"
        bundle = portable_checkpoint(
            {"src/main.py": "print('hi')\n", "docs/notes.txt": "wip"},
            {"work_id": "w"},
        )

        returned = rehydrate(bundle, scratch)

        assert returned == scratch
        assert (scratch / "src" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert (scratch / "docs" / "notes.txt").read_text(encoding="utf-8") == "wip"

    @pytest.mark.parametrize("evil", ["../escape.txt", "/etc/passwd", "a/../../escape.txt"])
    def test_traversal_refused_before_anything_is_written(self, tmp_path, evil):
        scratch = tmp_path / "scratch"
        bundle = portable_checkpoint({evil: "boom"}, {})

        with pytest.raises(ValueError, match="escapes scratch"):
            rehydrate(bundle, scratch)

        # Validation precedes writing: no partial workspace, no escape.
        assert not scratch.exists()
        assert not (tmp_path / "escape.txt").exists()


class TestBatchController:
    async def test_run_until_pause_stops_at_checkpoint_ready(self):
        runtime = CheckpointRestartRuntime()
        await runtime.start("fix the flaky test", profile_id="batch-cli")
        await runtime.send_control(ControlOp(op="pause"))

        events = await BatchController().run_until_pause(runtime, max_turns=5)

        kinds = [event.kind for event in events]
        assert kinds[:2] == ["turn_started", "turn_finished"]
        assert kinds[-1] == "checkpoint_ready"
        # The pause landed after ONE episode — turns after it never ran.
        assert kinds.count("turn_started") == 1

    async def test_run_until_pause_runs_bounded_episodes_without_a_pause(self):
        runtime = CheckpointRestartRuntime()
        await runtime.start("t", profile_id="batch-cli")

        events = await BatchController().run_until_pause(runtime, max_turns=3)

        kinds = [event.kind for event in events]
        assert kinds.count("turn_started") == 3
        assert "checkpoint_ready" not in kinds

    async def test_resume_with_restores_and_starts_again(self):
        first = CheckpointRestartRuntime()
        await first.start("fix the flaky test", profile_id="batch-cli")
        state = await first.checkpoint()

        second = CheckpointRestartRuntime()
        controller = BatchController()
        await controller.resume_with(second, state)

        events = await controller.run_until_pause(second, max_turns=1)
        assert [event.kind for event in events] == ["turn_started", "turn_finished"]
        # The resumed session drifts nothing from the checkpoint it came from.
        assert await second.checkpoint() == state
