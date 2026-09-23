"""NEXT-14 — the one supervisor that owns the lane drive cycle.

The turn task, the steering drain task and the terminal classification
are SUBMITTED to a :class:`~forge.adaptive.lane_supervisor.LaneSupervisor`;
these tests pin the race itself (not the vendor clients underneath it):

- the supervisor races a turn against a drain — turn completion cancels
  the drain, and the drain's final work still runs before the consumer
  is released;
- the drain's urgent interrupt fires BEFORE a queued steer (pause-first,
  the same ordering :func:`~forge.adaptive.lane_control.drain_cycle_order`
  pins inside one drain cycle, here at the lifecycle level);
- the terminal classification happens EXACTLY ONCE, whatever else races;
- an urgent request suspends the turn (the turn's own verdict never
  arrives, and the classification names the operator, never a vendor
  completion), and a LATE urgent cannot rewrite a written verdict;
- the composed path end to end: ``drive_lane`` with a pause command in
  the mailbox classifies ``operator_pause`` with the pause APPLIED in
  the journal.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from forge.adaptive.lane_control import drain_cycle_order
from forge.adaptive.lane_supervisor import (
    TERMINAL_COMPLETED,
    TERMINAL_SUSPENDED,
    LaneSupervisor,
    TerminalEvent,
)
from forge.adaptive.models import ControlCommand
from forge.adaptive.wiring import OperatorControlService


def _classifier(events: list[TerminalEvent]):
    """A counting classifier — the exactly-once observable."""

    def classify(event: TerminalEvent) -> str:
        events.append(event)
        if event.kind == TERMINAL_SUSPENDED:
            return f"suspended:{event.urgent.kind if event.urgent else '?'}"
        if event.kind == TERMINAL_COMPLETED:
            return f"completed:{event.result}"
        return f"failed:{event.reason}"

    return classify


class TestTheRace:
    async def test_turn_completion_cancels_the_drain(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))
        drain_state: dict[str, object] = {"cancelled": False, "finally_ran": False}

        async def turn() -> str:
            await asyncio.sleep(0.05)
            return "the-verdict"

        async def drain() -> None:
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                drain_state["cancelled"] = True
                raise
            finally:
                drain_state["finally_ran"] = True

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        outcome = await supervisor.run()

        assert outcome == "completed:the-verdict"
        # the drain was torn down BY the turn's completion, and its own
        # cleanup ran before the consumer was released
        assert drain_state["cancelled"] is True
        assert drain_state["finally_ran"] is True
        assert supervisor.classifications == 1

    async def test_the_drain_runs_concurrently_with_the_turn(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))
        seen: list[str] = []

        async def turn() -> str:
            for _ in range(5):
                await asyncio.sleep(0.01)
            return "done"

        async def drain() -> None:
            for _ in range(50):
                seen.append("drain")
                await asyncio.sleep(0.01)

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        assert await supervisor.run() == "completed:done"
        assert seen, "the drain ran beside the turn, not after it"

    async def test_a_drain_that_raises_never_eats_the_verdict(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def turn() -> str:
            await asyncio.sleep(0.02)
            return "verdict"

        async def broken_drain() -> None:
            raise RuntimeError("control plane exploded")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(broken_drain())
        assert await supervisor.run() == "completed:verdict"
        assert any("exploded" in note for note in supervisor.notes)

    async def test_a_failing_turn_classifies_failed_never_escapes(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def boom() -> str:
            raise RuntimeError("vendor transport died")

        supervisor.submit_turn(boom())
        assert await supervisor.run() == "failed:vendor transport died"
        assert supervisor.classifications == 1

    async def test_the_drain_teardown_is_bounded(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(
            classify=_classifier(events), drain_cancel_wait_s=0.05
        )

        async def turn() -> str:
            return "fast"

        async def hung_drain() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                while True:
                    await asyncio.sleep(10)
            await asyncio.sleep(10)  # even its cleanup hangs

        supervisor.submit_turn(turn())
        supervisor.submit_drain(hung_drain())
        assert await supervisor.run() == "completed:fast"
        assert any("exceeded" in note for note in supervisor.notes)


class TestUrgentBeforeSteer:
    async def test_the_drains_urgent_interrupt_fires_before_a_queued_steer(self) -> None:
        """Pause-first at the LIFECYCLE level: within one drain batch the
        interrupt-class command applies (and flags the supervisor) before
        the queued steer's effect — and the steer that lands after the
        suspension is the RESUME turn's business, never a mid-turn push."""
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))
        effects: list[str] = []

        async def turn() -> str:
            await asyncio.sleep(10)  # a long vendor wait
            return "never-reached"

        def command(kind: str, sequence: int) -> ControlCommand:
            return ControlCommand(
                command_id=f"cmd-{kind}",
                work_id="wp-1",
                kind=kind,  # type: ignore[arg-type]
                actor_origin="operator_token",
                actor_ref="op-1",
                idempotency_key=f"idem-{kind}",
                sequence=sequence,
                status="received",
                payload={"text": f"{kind} please", "run_id": "run-1"},
            )

        async def drain() -> None:
            batch = drain_cycle_order([command("steer", 1), command("pause", 2)])
            for pending in batch:
                assert pending.kind in ("pause", "steer")
                if pending.kind == "pause":
                    effects.append("pause-applied")
                    # the urgent seam is SYNC: the flag and the turn's
                    # suspension happen in this slice, before any steer
                    # effect can interleave
                    assert supervisor.request_urgent("pause", "operator pause applied mid-turn")
                else:
                    effects.append("steer-queued-for-resume")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        outcome = await supervisor.run()

        # the urgent interrupt won the race: it fired BEFORE the steer,
        # the turn was suspended (never completed), and the classification
        # names the operator — never a vendor completion.
        assert effects == ["pause-applied", "steer-queued-for-resume"]
        assert outcome == "suspended:pause"
        assert supervisor.urgent is not None
        assert supervisor.urgent.kind == "pause"
        assert events[0].kind == TERMINAL_SUSPENDED

    async def test_the_suspension_records_the_turns_fallback_timing(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def turn() -> str:
            await asyncio.sleep(10)
            return "never"

        async def drain() -> None:
            await asyncio.sleep(0.02)
            supervisor.request_urgent("pause", "operator pause applied mid-turn")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        await supervisor.run()
        assert events[0].elapsed_s > 0.0  # measured, never invented


class TerminalWriter:
    """A classifier that records every call — the exactly-once witness."""

    def __init__(self) -> None:
        self.calls: list[TerminalEvent] = []

    def __call__(self, event: TerminalEvent) -> str:
        self.calls.append(event)
        return "verdict"


class TestExactlyOnceClassification:
    async def test_the_terminal_outcome_is_written_exactly_once(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)

        async def turn() -> str:
            await asyncio.sleep(0.01)
            return "result"

        async def noisy_drain() -> None:
            # a drain that races the turn with its own completion AND a
            # late urgent request — none of it may produce a second verdict
            await asyncio.sleep(0.001)
            supervisor.request_urgent("pause", "too late — the turn already ended")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(noisy_drain())
        assert await supervisor.run() == "verdict"
        assert len(writer.calls) == 1
        # a second run is refused — one verdict per cycle, observable
        with pytest.raises(RuntimeError, match="already written"):
            await supervisor.run()
        assert len(writer.calls) == 1

    async def test_a_late_urgent_never_rewrites_the_written_verdict(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)

        async def turn() -> str:
            return "already-done"

        supervisor.submit_turn(turn())
        supervisor.submit_drain(_noop_drain())
        assert await supervisor.run() == "verdict"
        assert supervisor.request_urgent("pause", "late") is False
        assert supervisor.urgent is None  # never recorded as a suspension
        assert any("never re-classified" in note for note in supervisor.notes)
        assert len(writer.calls) == 1

    async def test_submissions_are_refused_after_the_verdict(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)

        async def turn() -> str:
            return "done"

        supervisor.submit_turn(turn())
        await supervisor.run()
        leftover = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="already written"):
            supervisor.submit_turn(leftover)
        leftover.close()

    async def test_one_turn_and_one_drain_per_cycle(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)

        async def turn() -> str:
            return "done"

        supervisor.submit_turn(turn())
        supervisor.submit_drain(_noop_drain())
        extra_turn = asyncio.sleep(0)
        extra_drain = asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="one turn per supervisor"):
            supervisor.submit_turn(extra_turn)
        with pytest.raises(RuntimeError, match="one drain per supervisor"):
            supervisor.submit_drain(extra_drain)
        extra_turn.close()
        extra_drain.close()

    async def test_running_without_a_turn_is_a_contract_error(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)
        with pytest.raises(RuntimeError, match="submit the turn"):
            await supervisor.run()

    async def test_an_outside_cancellation_is_not_a_verdict(self) -> None:
        writer = TerminalWriter()
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=writer)

        async def turn() -> str:
            await asyncio.sleep(10)
            return "never"

        supervisor.submit_turn(turn())
        supervisor.submit_drain(_noop_drain())
        task = asyncio.ensure_future(supervisor.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # nothing was classified — teardown is not a terminal event
        assert writer.calls == []
        assert supervisor.classifications == 0


async def _noop_drain() -> None:
    await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------
# The composed path end to end (the delegation in forge.lane_driver)
# ---------------------------------------------------------------------------


class PausableLaneClient:
    """The claude lane client contract over a turn that only a PAUSE ends:
    the vendor interrupt the drain sends is what makes the turn stop
    producing nothing — exactly the lifecycle the supervisor owns."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_session(self, task: str) -> str:
        self.calls.append(("start_session", task))
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("steer", session_id, text))

    async def interrupt(self, session_id: str) -> None:
        self.calls.append(("interrupt", session_id))

    async def query(self, session_id: str) -> list[dict]:
        return []  # the turn never resolves on its own

    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))


class TestDriveLaneDelegation:
    async def test_an_operator_pause_suspends_the_turn_and_classifies_it(self, monkeypatch):
        from forge.lane_driver import drive_lane

        monkeypatch.setenv("FORGE_RUN_ID", "run-1")
        monkeypatch.setenv("FORGE_WORK_ID", "wp-1")
        # No WIP capture in this test: the pause lands paused_partial (the
        # honest absent-capability posture). The capture itself hashes the
        # whole lane checkout — real-lane work this lifecycle test does not
        # target (it has its own coverage in the checkpointing suites).
        monkeypatch.setattr("forge.lane_driver._lane_capture_capability", lambda work_id: None)
        control = OperatorControlService()
        await control.pause("wp-1", "human:op", "pause-once")
        client = PausableLaneClient()

        outcome = await drive_lane(
            client, task="do the thing", budget_s=5.0, poll_s=0.01, control=control
        )

        # the suspension is the lane's verdict: the operator's pause, never
        # a vendor completion and never a silent budget expiry
        assert outcome.exit_status == "failed"
        assert outcome.terminal_reason == "operator_pause"
        assert "pause" in outcome.error
        # the drain APPLIED the pause (the vendor interrupt went out) and
        # the episode still carries every phase key
        assert ("interrupt", "sess-1") in client.calls
        assert client.calls[-1] == ("close", "sess-1")
        assert outcome.episode is not None and outcome.episode["turn_s"] > 0.0
        journal = outcome.steering_journal or []
        assert any(entry["kind"] == "pause" and entry["outcome"] == "applied" for entry in journal)


# ---------------------------------------------------------------------------
# R32-09 (review 0fca1b7): control-consumer health as execution policy
# ---------------------------------------------------------------------------


class TestControlConsumerHealth:
    """A drain that dies while the turn still runs is observed LIVE — the
    verdict the turn then reaches carries ``control_degraded`` (a
    successful turn never masquerades as a controllable one), and under
    the strict contract the loss suspends the turn through the same
    one-verdict machinery an operator pause uses."""

    async def test_a_drain_that_dies_mid_turn_degrades_control_not_the_verdict(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def turn() -> str:
            await asyncio.sleep(0.05)
            return "verdict"

        async def broken_drain() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("control plane exploded")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(broken_drain())
        outcome = await supervisor.run()

        assert outcome == "completed:verdict"  # the turn still reached its verdict
        assert events[0].kind == TERMINAL_COMPLETED
        # ... but the degradation rides the terminal event, never swallowed
        assert "exploded" in events[0].control_degraded
        assert "exploded" in supervisor.control_degraded
        assert any("control_degraded" in note for note in supervisor.notes)
        assert supervisor.classifications == 1

    async def test_the_degradation_is_observed_while_the_turn_still_runs(self) -> None:
        """The acceptance shape: the RUNNING turn itself can see the loss —
        the observation happened before its own verdict, not at teardown."""
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=lambda event: "done")
        observed_mid_turn: list[bool] = []

        async def turn() -> str:
            await asyncio.sleep(0.05)
            observed_mid_turn.append(bool(supervisor.control_degraded))
            return "verdict"

        async def drain() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("steering died early")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        assert await supervisor.run() == "done"

        assert observed_mid_turn == [True]  # recorded BEFORE the turn ended

    async def test_a_clean_drain_return_is_not_degradation(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def turn() -> str:
            await asyncio.sleep(0.02)
            return "done"

        supervisor.submit_turn(turn())
        supervisor.submit_drain(_noop_drain())  # returns cleanly, early
        assert await supervisor.run() == "completed:done"
        assert supervisor.control_degraded == ""
        assert events[0].control_degraded == ""
        assert not any("control_degraded" in note for note in supervisor.notes)

    async def test_strict_mode_suspends_the_turn_when_the_drain_dies(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(
            classify=_classifier(events), strict_control=True
        )
        turn_reached_its_end: list[bool] = []

        async def turn() -> str:
            await asyncio.sleep(10)  # a long vendor wait the suspension must cut
            turn_reached_its_end.append(True)
            return "never-reached"

        async def drain() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("steering lost")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(drain())
        outcome = await supervisor.run()

        assert outcome == "suspended:control_lost"
        assert events[0].kind == TERMINAL_SUSPENDED
        assert supervisor.urgent is not None
        assert supervisor.urgent.kind == "control_lost"
        assert "steering lost" in events[0].reason
        assert "steering lost" in events[0].control_degraded
        assert turn_reached_its_end == []  # the turn never ran to its own end
        assert supervisor.classifications == 1

    async def test_strict_mode_does_not_escalate_a_clean_drain_return(self) -> None:
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(
            classify=_classifier(events), strict_control=True
        )

        async def turn() -> str:
            await asyncio.sleep(0.02)
            return "done"

        supervisor.submit_turn(turn())
        supervisor.submit_drain(_noop_drain())
        assert await supervisor.run() == "completed:done"  # only a FAILURE suspends
        assert supervisor.urgent is None

    async def test_the_strict_contract_reads_the_env_and_the_pin_wins(self, monkeypatch):
        from forge.adaptive.lane_supervisor import STRICT_CONTROL_ENV, strict_control_from_env

        monkeypatch.delenv(STRICT_CONTROL_ENV, raising=False)
        assert strict_control_from_env() is False
        assert strict_control_from_env({}) is False
        assert strict_control_from_env({STRICT_CONTROL_ENV: "1"}) is True
        assert strict_control_from_env({STRICT_CONTROL_ENV: "off"}) is False

        monkeypatch.setenv(STRICT_CONTROL_ENV, "1")
        assert LaneSupervisor(classify=_classifier([])).strict_control is True
        # an explicit pin overrides the env (a cycle may opt out deliberately)
        pinned = LaneSupervisor(classify=_classifier([]), strict_control=False)
        assert pinned.strict_control is False

    async def test_a_drain_failure_after_the_turn_is_teardown_noise_not_degradation(
        self,
    ) -> None:
        """The drain outliving the turn fails during TEARDOWN — recorded as
        a teardown note, never as mid-turn control degradation."""
        events: list[TerminalEvent] = []
        supervisor: LaneSupervisor[str] = LaneSupervisor(classify=_classifier(events))

        async def turn() -> str:
            return "already-done"  # the verdict exists before the drain dies

        async def dying_drain() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("died at teardown")

        supervisor.submit_turn(turn())
        supervisor.submit_drain(dying_drain())
        assert await supervisor.run() == "completed:already-done"
        assert events[0].control_degraded == ""  # control was intact WHILE it ran
        assert supervisor.classifications == 1
