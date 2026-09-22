"""The steering bridge tests — operator ControlCommands drive a RUNNING lane.

Fake driver clients duck-type the adapter Protocols
(ClaudeSDKClient / CodexAppClient / OpenCodeClient); every mapping,
refusal, guard, and the concurrent drain loop are proven against the
REAL control.py structures (OperatorControlService + Mailbox ladder).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from forge.adaptive.adapters import (
    ClaudeSDKAdapter,
    CodexAppAdapter,
    OpenCodeAdapter,
)
from forge.adaptive.lane_control import (
    LANE_CONTROL_SURFACE,
    RESUME_NOTICE,
    STEER_TEXT_CAP,
    LaneSteeringSession,
)
from forge.adaptive.models import ControlCommand
from forge.adaptive.wiring import OperatorControlService


# -- fakes: duck-typed against the adapter Protocols ------------------------


class FakeClaudeClient:
    """Matches forge.adaptive.adapters.ClaudeSDKClient."""

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


class FakeCodexClient:
    """Matches forge.adaptive.adapters.CodexAppClient."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_thread(self, task: str) -> str:
        return "th-1"

    async def send_turn(self, thread_id: str, text: str) -> None:
        self.calls.append(("send_turn", thread_id, text))

    async def steer_active_turn(self, thread_id: str, text: str) -> None:
        self.calls.append(("steer_active_turn", thread_id, text))

    async def interrupt(self, thread_id: str) -> None:
        self.calls.append(("interrupt", thread_id))


class FakeOpenCodeClient:
    """Matches forge.adaptive.adapters.OpenCodeClient."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_session(self, task: str) -> str:
        return "oc-1"

    async def prompt(self, session_id: str, text: str) -> None:
        self.calls.append(("prompt", session_id, text))

    async def events(self, session_id: str) -> list[dict]:
        return []

    async def abort(self, session_id: str) -> None:
        self.calls.append(("abort", session_id))


class _Seq:
    """Per-test strictly increasing command sequences (one work)."""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _cmd(
    kind: str,
    seq: int,
    *,
    work_id: str = "wp-1",
    payload: dict | None = None,
    run_id: str | None = None,
    expected_execution_epoch: int | None = None,
    id_prefix: str = "",
) -> ControlCommand:
    full_payload = dict(payload or {})
    if run_id is not None:
        full_payload["run_id"] = run_id
    return ControlCommand(
        schema="forge.proposal.control-command/1",
        command_id=f"cmd-{id_prefix}{seq}",
        work_id=work_id,
        sequence=seq,
        kind=kind,
        actor_ref="human:op",
        actor_origin="server_authenticated_human",
        idempotency_key=f"key-{id_prefix}{seq}",
        status="received",
        payload=full_payload,
        expected_execution_epoch=expected_execution_epoch,
    )


async def _submit(service: OperatorControlService, command: ControlCommand) -> None:
    _, created = await service.submit(command)
    assert created


_ADAPTERS = {
    "claude": ClaudeSDKAdapter,
    "codex": CodexAppAdapter,
    "opencode": OpenCodeAdapter,
}


def _session(
    service: OperatorControlService,
    kind: str,
    client: object,
    *,
    run_id: str = "run-1",
    work_id: str = "wp-1",
    vendor: str = "sess-1",
    **kwargs: object,
) -> LaneSteeringSession:
    return LaneSteeringSession(
        service=service,
        driver=_ADAPTERS[kind](client=client),
        driver_kind=kind,
        run_id=run_id,
        work_id=work_id,
        vendor_session_id=vendor,
        **kwargs,
    )


# -- construction pairing ----------------------------------------------------


class TestConstruction:
    def test_a_mismatched_driver_kind_is_refused(self):
        with pytest.raises(ValueError, match="requires a codex-app adapter"):
            LaneSteeringSession(
                service=OperatorControlService(),
                driver=ClaudeSDKAdapter(client=FakeClaudeClient()),  # claude profile...
                driver_kind="codex",  # ...under a codex kind — a wiring error
                run_id="run-1",
                work_id="wp-1",
            )

    def test_an_unknown_driver_kind_is_refused(self):
        with pytest.raises(ValueError, match="driver_kind must be one of"):
            LaneSteeringSession(
                service=OperatorControlService(),
                driver=ClaudeSDKAdapter(client=FakeClaudeClient()),
                driver_kind="cursor",
                run_id="run-1",
                work_id="wp-1",
            )


# -- pause -------------------------------------------------------------------


class TestPause:
    async def test_pause_interrupts_and_records_the_pause_state(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))

        actions = await session.drain_once()

        assert client.calls == [("interrupt", "sess-1")]
        assert actions[0].outcome == "applied"
        assert session.pause_state.pause_requested is True
        assert session.pause_state.interrupt_sent is True
        assert session.pause_state.checkpoint_captured is True

    async def test_pause_is_idempotent(self):
        svc = OperatorControlService()
        client = FakeCodexClient()
        session = _session(svc, "codex", client, vendor="th-1")
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("pause", 2))

        actions = await session.drain_once()

        # ONE interrupt, two journaled commands — the second applied as a no-op
        assert client.calls == [("interrupt", "th-1")]
        assert [a.outcome for a in actions] == ["applied", "applied"]
        assert actions[1].detail["pause"] == "already-paused"

    async def test_pause_without_a_bound_vendor_session_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, vendor="")
        await _submit(svc, _cmd("pause", 1))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "no vendor session bound" in actions[0].reason
        assert client.calls == []
        assert session.pause_state.pause_requested is False


# -- steer -------------------------------------------------------------------


class TestSteer:
    async def test_claude_steer_delivers_mid_turn_guidance(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "fix the assertion first"}))

        actions = await session.drain_once()

        assert client.calls == [("steer", "sess-1", "fix the assertion first")]
        assert actions[0].outcome == "applied"
        assert actions[0].detail["delivered"] == "mid-turn"

    async def test_codex_steer_targets_the_active_turn(self):
        svc = OperatorControlService()
        client = FakeCodexClient()
        session = _session(svc, "codex", client, vendor="th-1")
        await _submit(svc, _cmd("steer", 1, payload={"text": "tighten the retry bounds"}))

        await session.drain_once()

        # EXE-06: guidance reaches the ACTIVE TURN, never a new turn
        assert client.calls == [("steer_active_turn", "th-1", "tighten the retry bounds")]

    async def test_opencode_mid_turn_steer_is_refused_without_live_input(self):
        svc = OperatorControlService()
        client = FakeOpenCodeClient()
        session = _session(svc, "opencode", client, vendor="oc-1")
        await _submit(svc, _cmd("steer", 1, payload={"text": "look at the parser next"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "live_input" in actions[0].reason
        assert client.calls == []  # fail closed — no guessed capability

    async def test_steer_while_paused_is_queued_not_delivered(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("steer", 2, payload={"text": "start from the tests"}))

        actions = await session.drain_once()

        assert client.calls == [("interrupt", "sess-1")]  # no send into a dead turn
        assert actions[1].outcome == "applied"
        assert actions[1].detail["queued_for_resume"] is True
        assert session.queued_guidance == ["start from the tests"]

    async def test_two_steers_apply_in_sequence_order(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "first note"}))
        await _submit(svc, _cmd("steer", 2, payload={"text": "second note"}))

        await session.drain_once()

        # CTL-07: sequenced order, never arrival order
        assert [call[2] for call in client.calls] == ["first note", "second note"]


# -- answer ------------------------------------------------------------------


class TestAnswer:
    async def test_answer_steers_the_answer_text(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("answer", 1, payload={"question_id": "Q1", "text": "use option A"}))

        await session.drain_once()

        assert client.calls == [
            ("steer", "sess-1", "[operator answer to question Q1] use option A")
        ]

    async def test_answer_while_paused_queues_for_the_resume_turn(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("answer", 2, payload={"question_id": "Q1", "text": "use option A"}))

        await session.drain_once()

        assert client.calls == [("interrupt", "sess-1")]
        assert session.queued_guidance == ["[operator answer to question Q1] use option A"]


# -- resume ------------------------------------------------------------------


class TestResume:
    async def test_resume_without_a_pause_is_refused(self):
        svc = OperatorControlService()
        client = FakeCodexClient()
        session = _session(svc, "codex", client, vendor="th-1")
        await _submit(svc, _cmd("resume", 1))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "not paused" in actions[0].reason
        assert client.calls == []

    async def test_resume_starts_a_new_turn_with_the_queued_operator_text(self):
        svc = OperatorControlService()
        client = FakeCodexClient()
        session = _session(svc, "codex", client, vendor="th-1")
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("steer", 2, payload={"text": "start from the tests"}))
        await _submit(svc, _cmd("resume", 3))

        await session.drain_once()

        assert client.calls == [
            ("interrupt", "th-1"),
            ("send_turn", "th-1", "start from the tests"),
        ]
        assert session.pause_state.pause_requested is False
        assert session.queued_guidance == []

    async def test_resume_with_an_empty_queue_carries_the_notice(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("resume", 2))

        await session.drain_once()

        assert client.calls == [
            ("interrupt", "sess-1"),
            ("steer", "sess-1", RESUME_NOTICE),
        ]

    async def test_resume_refused_when_the_snapshot_is_unavailable(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, snapshot_available=False)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("resume", 2))

        actions = await session.drain_once()

        assert actions[1].outcome == "refused"
        assert "snapshot set unavailable" in actions[1].reason
        assert all(call[0] == "interrupt" for call in client.calls)

    async def test_opencode_full_cycle_queues_and_resumes_via_prompt(self):
        svc = OperatorControlService()
        client = FakeOpenCodeClient()
        session = _session(svc, "opencode", client, vendor="oc-1")
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("steer", 2, payload={"text": "check the migration first"}))
        await _submit(svc, _cmd("resume", 3))

        await session.drain_once()

        assert client.calls == [
            ("abort", "oc-1"),
            ("prompt", "oc-1", "check the migration first"),
        ]


# -- refusals ----------------------------------------------------------------


class TestRefusals:
    async def test_amend_is_refused_as_plan_revision(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("amend", 1, payload={"text": "use RabbitMQ instead"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "human gate" in actions[0].reason
        assert client.calls == []
        # refused commands stay at their ladder rung for the reconciler
        assert svc.mailbox.commands["cmd-1"].status == "received"

    async def test_approve_revision_is_refused_as_plan_revision(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("approve-revision", 1))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "material revision" in actions[0].reason
        assert client.calls == []

    async def test_acceptance_weakening_text_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "just skip the tests for now"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "revision gate" in actions[0].reason
        assert client.calls == []

    async def test_constraint_introducing_text_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "do not introduce a new broker"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "ChangeProposal" in actions[0].reason
        assert client.calls == []

    async def test_over_cap_text_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "x" * (STEER_TEXT_CAP + 1)}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "cap" in actions[0].reason
        assert client.calls == []

    async def test_empty_text_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "   "}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "empty" in actions[0].reason


# -- guards: cross-run / cross-work isolation --------------------------------


class TestIsolation:
    async def test_a_command_for_another_run_is_ignored(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, run_id="run-1")
        await _submit(svc, _cmd("steer", 1, payload={"text": "for the other lane"}, run_id="run-9"))

        actions = await session.drain_once()

        assert actions[0].outcome == "ignored"
        assert client.calls == []
        # left in the mailbox, untouched, for the OWNING lane
        assert svc.mailbox.commands["cmd-1"].status == "received"

    async def test_the_owning_lane_still_applies_the_ignored_command(self):
        svc = OperatorControlService()
        client_a = FakeClaudeClient()
        client_b = FakeClaudeClient()
        lane_a = _session(svc, "claude", client_a, run_id="run-1")
        lane_b = _session(svc, "claude", client_b, run_id="run-9")
        await _submit(svc, _cmd("steer", 1, payload={"text": "for lane B only"}, run_id="run-9"))

        await lane_a.drain_once()
        await lane_b.drain_once()

        assert client_a.calls == []
        assert client_b.calls == [("steer", "sess-1", "for lane B only")]

    async def test_a_command_without_run_scoping_applies(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "unscoped note"}))

        await session.drain_once()

        assert client.calls == [("steer", "sess-1", "unscoped note")]

    async def test_another_work_commands_are_never_seen(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, work_id="wp-1")
        await _submit(svc, _cmd("pause", 1, work_id="wp-2"))

        actions = await session.drain_once()

        assert actions == []  # pending() is per-work: wp-2 is not ours


# -- ladder + journal ---------------------------------------------------------


class TestLadderAndJournal:
    async def test_applied_commands_climb_to_checkpointed_and_leave_pending(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "one delivery"}))

        first = await session.drain_once()
        second = await session.drain_once()

        assert first[0].detail["mailbox_status"] == "checkpointed"
        assert svc.mailbox.commands["cmd-1"].status == "checkpointed"
        assert await svc.pending("wp-1") == []
        assert second == []  # a spent command can never re-apply

    async def test_a_stale_epoch_command_expires_before_any_effect(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, execution_epoch=1)
        await _submit(svc, _cmd("pause", 1, expected_execution_epoch=7))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "expired" in actions[0].reason
        assert svc.mailbox.commands["cmd-1"].status == "expired"
        assert client.calls == []

    async def test_an_actor_outside_the_scopes_is_refused(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, actor_scopes={})
        await _submit(svc, _cmd("steer", 1, payload={"text": "hello"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "refused"
        assert "mailbox gate refused" in actions[0].reason
        assert client.calls == []

    async def test_the_journal_is_append_only_evidence(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("steer", 2, payload={"text": "queued note"}))
        await _submit(svc, _cmd("resume", 3))

        await session.drain_once()
        snapshot = session.journal
        await _submit(svc, _cmd("steer", 4, payload={"text": "late note"}))
        await session.drain_once()

        assert len(session.journal) == 4
        assert session.journal[:3] == snapshot  # earlier entries never change
        assert [a.sequence for a in session.journal] == [1, 2, 3, 4]
        assert [a.kind for a in session.journal] == ["pause", "steer", "resume", "steer"]
        assert all(a.outcome == "applied" for a in session.journal)


# -- the concurrent drain loop (the async-context seam) ----------------------


class TestConcurrentDrain:
    async def test_the_mailbox_is_drained_during_a_long_turn(self):
        svc = OperatorControlService()
        timeline: list[tuple[str, float]] = []
        loop = asyncio.get_running_loop()

        class TimedClient(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                timeline.append(("steered", loop.time()))

        session = _session(svc, "claude", TimedClient())
        async with session:
            await svc.steer("wp-1", "human:op", "mid-turn guidance", run_id="run-1")
            await asyncio.sleep(0.05)  # the agent turn runs...
            timeline.append(("turn-end", loop.time()))

        assert timeline[0][0] == "steered"
        assert timeline[0][1] < timeline[-1][1]  # delivered DURING the turn
        assert [a.outcome for a in session.journal] == ["applied"]

    async def test_the_loop_polls_bounded_no_busy_loop(self):
        svc = OperatorControlService()

        class Counting(LaneSteeringSession):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self.drains = 0

            async def drain_once(self):
                self.drains += 1
                return await super().drain_once()

        session = Counting(
            service=svc,
            driver=ClaudeSDKAdapter(client=FakeClaudeClient()),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
            poll_interval=0.01,
        )
        async with session:
            await asyncio.sleep(0.05)  # idle — nothing in the mailbox

        # ~5 polls for a 50ms idle turn: sleeping between drains, not spinning
        assert 1 <= session.drains <= 20

    async def test_a_command_submitted_late_is_consumed_by_the_final_drain(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, poll_interval=5.0)

        async def late_submit() -> None:
            await asyncio.sleep(0.01)
            await svc.steer("wp-1", "human:op", "last word", run_id="run-1")

        async with session:
            await late_submit()  # submitted between the slow polls

        assert client.calls == [("steer", "sess-1", "last word")]
        assert [a.outcome for a in session.journal] == ["applied"]


# -- the structural permission-posture guarantee ------------------------------


class TestPermissionPosture:
    async def test_every_vendor_call_is_text_only_and_within_the_surface(self):
        svc = OperatorControlService()
        vendors = {"claude": "sess-1", "codex": "th-1", "opencode": "oc-1"}
        clients: dict[str, object] = {
            "claude": FakeClaudeClient(),
            "codex": FakeCodexClient(),
            "opencode": FakeOpenCodeClient(),
        }
        for kind, client in clients.items():
            work = f"wp-{kind}"
            session = _session(svc, kind, client, vendor=vendors[kind], work_id=work)
            seq = _Seq()
            await _submit(svc, _cmd("pause", seq.next(), work_id=work, id_prefix=f"{kind}-"))
            await _submit(
                svc,
                _cmd(
                    "steer",
                    seq.next(),
                    payload={"text": "note"},
                    work_id=work,
                    id_prefix=f"{kind}-",
                ),
            )
            await _submit(svc, _cmd("resume", seq.next(), work_id=work, id_prefix=f"{kind}-"))
            await session.drain_once()

            allowed = LANE_CONTROL_SURFACE[kind]
            assert client.calls, kind
            for call in client.calls:
                assert call[0] in allowed
                for arg in call[1:]:
                    assert isinstance(arg, str)  # ids and text — nothing else

    def test_the_surface_names_only_guidance_and_interrupt_methods(self):
        assert set(LANE_CONTROL_SURFACE) == {"claude", "codex", "opencode"}
        for kind, adapter in _ADAPTERS.items():
            for name in LANE_CONTROL_SURFACE[kind]:
                method = getattr(adapter, name, None)
                assert callable(method), (kind, name)
                params = set(inspect.signature(method).parameters) - {"self"}
                # a vendor id and, at most, text: no permission can ride along
                assert params <= {"session_id", "thread_id", "text"}, (kind, name)


# -- NXT-12: the delivery ladder (intent before effect) ------------------------


class TestDeliveryLadder:
    async def test_the_intent_is_on_record_before_the_vendor_call(self):
        svc = OperatorControlService()
        probes: list[str] = []

        class Probing(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                # mid-vendor-call probe: the dispatch intent PRECEDES the send
                probes.append(steering.delivery_state("cmd-1"))
                self.calls.append(("steer", session_id, text))

        client = Probing()
        steering = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "probe me"}))

        actions = await steering.drain_once()

        assert probes == ["dispatching"]
        assert actions[0].outcome == "applied"
        assert actions[0].delivery == "application_observed"
        assert steering.delivery_state("cmd-1") == "application_observed"
        assert actions[0].detail["mailbox_status"] == "checkpointed"

    async def test_a_timeout_is_outcome_unknown_never_applied(self):
        svc = OperatorControlService()

        class Lagging(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                raise TimeoutError("claude gateway did not answer in 30s")

        client = Lagging()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "slow note"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "delivery_unknown"
        assert actions[0].delivery == "outcome_unknown"
        assert "probe" in actions[0].reason or "reconcile" in actions[0].reason
        assert session.delivery_state("cmd-1") == "outcome_unknown"
        # NEVER applied: the coarse mailbox sits at its intent rung
        # ("applied" ≙ the durable ladder's dispatching), checkpoint never ran
        assert svc.mailbox.commands["cmd-1"].status == "applied"
        assert svc.mailbox.commands["cmd-1"].status != "checkpointed"
        # and the command has left pending(): the drain cannot re-send it
        assert await svc.pending("wp-1") == []

    async def test_a_connection_error_is_uncertain_delivery(self):
        svc = OperatorControlService()

        class Dropping(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                raise ConnectionError("connection reset mid-request")

        client = Dropping()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "dropped note"}))

        actions = await session.drain_once()

        assert actions[0].outcome == "delivery_unknown"
        assert session.delivery_state("cmd-1") == "outcome_unknown"

    async def test_an_uncertain_command_is_never_silently_retried(self):
        svc = OperatorControlService()

        class OnceLagging(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                raise TimeoutError("first attempt lost")

        client = OnceLagging()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "uncertain"}))

        first = await session.drain_once()
        second = await session.drain_once()

        assert first[0].outcome == "delivery_unknown"
        assert second == []  # redelivery is barred: a probe must decide, not a retry
        assert len(client.calls) == 1

    async def test_a_definitive_vendor_error_is_error_not_uncertain(self):
        svc = OperatorControlService()

        class Refusing(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                raise RuntimeError("session is not connected")

        client = Refusing()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "doomed"}))

        actions = await session.drain_once()

        # a decisive adapter raise is a KNOWN failure — distinct from the
        # lost-response window, never surfaced to the reconciler as uncertain
        assert actions[0].outcome == "error"
        assert actions[0].delivery == "dispatching"
        assert session.uncertain_commands() == []

    async def test_uncertain_commands_expose_the_reconciler_view(self):
        svc = OperatorControlService()

        class Lagging(FakeCodexClient):
            async def steer_active_turn(self, thread_id: str, text: str) -> None:
                self.calls.append(("steer_active_turn", thread_id, text))
                raise TimeoutError("codex turn steer lost its response")

        client = Lagging()
        session = _session(svc, "codex", client, vendor="th-9")
        await _submit(svc, _cmd("steer", 1, payload={"text": "probe me"}))

        await session.drain_once()

        (record,) = session.uncertain_commands()
        assert record.command_id == "cmd-1"
        assert record.kind == "steer"
        assert record.sequence == 1
        assert record.state == "outcome_unknown"
        assert record.vendor_session_id == "th-9"
        assert record.execution_epoch == 1  # the correlation a recovery pass probes
        assert "reconcile" in record.note or "lost" in record.note

    async def test_mark_delivery_unknown_is_the_honest_terminal_close(self):
        svc = OperatorControlService()

        class Lagging(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                raise TimeoutError("no answer")

        session = _session(svc, "claude", Lagging())
        await _submit(svc, _cmd("steer", 1, payload={"text": "gone"}))
        await session.drain_once()

        closed = session.mark_delivery_unknown("cmd-1", "probe found no session evidence")

        assert closed.state == "delivery_unknown"
        assert session.delivery_state("cmd-1") == "delivery_unknown"
        assert session.uncertain_commands() == []  # decided-as-unknown: off the worklist
        # terminal: a decided command cannot be un-decided
        with pytest.raises(ValueError, match="only an outcome_unknown command"):
            session.mark_delivery_unknown("cmd-1")
        with pytest.raises(KeyError):
            session.mark_delivery_unknown("cmd-never-dispatched")

    async def test_a_crash_between_intent_and_effect_books_outcome_unknown(self):
        svc = OperatorControlService()

        class Hanging(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                self.calls.append(("steer", session_id, text))
                await asyncio.sleep(3600)  # the dispatch never completes

        client = Hanging()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "in flight"}))

        # the process-death window, in-process: the bounding wait cancels the
        # dispatch between the intent record and the vendor's answer
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(session.drain_once(), timeout=0.05)

        assert session.delivery_state("cmd-1") == "outcome_unknown"
        assert svc.mailbox.commands["cmd-1"].status == "applied"  # intent rung
        assert svc.mailbox.commands["cmd-1"].status != "checkpointed"  # never applied
        # the journal carries the crash-window record even though the drain
        # itself unwound — evidence survives the cancellation
        assert session.journal[-1].outcome == "delivery_unknown"
        assert session.journal[-1].delivery == "outcome_unknown"
        assert (session.uncertain_commands()[0].command_id) == "cmd-1"


# -- NXT-14 (first half): pause priority within a drain cycle ------------------


class TestPausePriorityDrain:
    async def test_pause_preempts_queued_slow_guidance(self):
        svc = OperatorControlService()
        timeline: list[str] = []

        class SlowSteer(FakeClaudeClient):
            async def send(self, session_id: str, text: str) -> None:
                timeline.append("steer-start")
                await asyncio.sleep(0.05)  # a guidance send can sit behind a drain timeout
                timeline.append("steer-end")
                self.calls.append(("steer", session_id, text))

        client = SlowSteer()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "slow note"}))  # sequenced FIRST
        await _submit(svc, _cmd("pause", 2))  # ...but urgent

        actions = await session.drain_once()

        # the interrupt never waited behind the slow steer: the steer's
        # mid-turn send never even started — it queued for the resume turn
        assert timeline == []
        assert client.calls == [("interrupt", "sess-1")]
        assert [a.kind for a in actions] == ["pause", "steer"]
        assert actions[1].detail["queued_for_resume"] is True
        assert session.queued_guidance == ["slow note"]
        assert session.pause_state.pause_requested is True

    async def test_resume_does_not_jump_the_queue(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "ordinary note"}))
        await _submit(svc, _cmd("pause", 2))
        await _submit(svc, _cmd("resume", 3))

        actions = await session.drain_once()

        # pause first (interrupt class), then steer→queue, then resume LAST —
        # the resume turn must run after the guidance it delivers has queued
        assert [a.kind for a in actions] == ["pause", "steer", "resume"]
        assert client.calls == [
            ("interrupt", "sess-1"),
            ("steer", "sess-1", "ordinary note"),
        ]


# -- NXT-11: the teardown guard ------------------------------------------------


class TestTeardownGuard:
    async def test_the_final_drain_never_starts_a_resume_turn(self):
        svc = OperatorControlService()
        client = FakeCodexClient()
        session = _session(svc, "codex", client, vendor="th-1", poll_interval=5.0)
        await _submit(svc, _cmd("pause", 1))
        await session.drain_once()  # paused: a resume would otherwise be eligible
        await _submit(svc, _cmd("resume", 2))  # seen only by the final drain

        async with session:
            pass  # the turn ends here

        # teardown consumed the late command but refused to START a new turn
        assert client.calls == [("interrupt", "th-1")]
        last = session.journal[-1]
        assert last.outcome == "refused"
        assert "closing" in last.reason
        assert svc.mailbox.commands["cmd-2"].status == "received"  # left for the reconciler

    async def test_the_final_drain_still_consumes_a_late_steer(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, poll_interval=5.0)
        await _submit(svc, _cmd("steer", 1, payload={"text": "last word"}))

        async with session:
            pass

        assert client.calls == [("steer", "sess-1", "last word")]
        assert [a.outcome for a in session.journal] == ["applied"]


class TestNxt14UrgentControlPath:
    """The review's second half: typed interrupt outcomes, measured
    latency, explicit guidance retention when an urgent control wins."""

    async def test_pause_records_acknowledged_outcome_and_latency(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        session.bind("sess-1")
        await _submit(svc, _cmd("pause", 1))

        actions = await session.drain_once()

        detail = actions[0].detail
        assert detail["interrupt_outcome"] == "acknowledged"
        assert isinstance(detail["interrupt_latency_s"], float)
        assert detail["retained_guidance"] == []

    async def test_timed_out_interrupt_is_unknown_never_success(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()

        async def hang(session_id):
            raise TimeoutError("interrupt never acked")

        client.interrupt = hang
        session = _session(svc, "claude", client)
        session.bind("sess-1")
        await _submit(svc, _cmd("pause", 1))

        actions = await session.drain_once()

        # The vendor outcome is honestly unknown — never an inferred
        # clean pause (the pause state's checkpoint truth is separate).
        assert actions[0].detail["interrupt_outcome"] == "unknown"
        assert actions[0].detail["interrupt_error"] == "TimeoutError"

    async def test_queued_guidance_is_retained_when_pause_wins(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        session.bind("sess-1")
        # Guidance queued (as the bridge does while paused) before the
        # urgent pause lands in the same cycle.
        session._queued.append("go west")
        await _submit(svc, _cmd("pause", 1))

        actions = await session.drain_once()

        assert actions[0].detail["retained_guidance"] == ["go west"]


# -- R28-11: the drain cycle's pinned ordering rule -----------------------------


class TestDrainCycleOrderPin:
    def test_interrupt_class_first_regardless_of_sequence(self):
        from forge.adaptive.lane_control import drain_cycle_order

        steer1 = _cmd("steer", 1, payload={"text": "one"})
        steer2 = _cmd("steer", 2, payload={"text": "two"})
        pause3 = _cmd("pause", 3)  # the HIGHEST sequence still goes FIRST

        ordered = drain_cycle_order([steer1, steer2, pause3])

        assert [command.kind for command in ordered] == ["pause", "steer", "steer"]
        assert [command.sequence for command in ordered] == [3, 1, 2]

    def test_sequence_still_rules_inside_each_class(self):
        from forge.adaptive.lane_control import drain_cycle_order

        ordered = drain_cycle_order(
            [
                _cmd("pause", 5),
                _cmd("steer", 2, payload={"text": "b"}),
                _cmd("pause", 4),
                _cmd("steer", 1, payload={"text": "a"}),
            ]
        )

        assert [(c.kind, c.sequence) for c in ordered] == [
            ("pause", 4),
            ("pause", 5),
            ("steer", 1),
            ("steer", 2),
        ]

    async def test_pause_at_a_higher_sequence_fires_its_vendor_call_first(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("steer", 1, payload={"text": "slow guidance"}))
        await _submit(svc, _cmd("steer", 2, payload={"text": "more guidance"}))
        await _submit(svc, _cmd("pause", 3))  # sequenced LAST, applied FIRST

        actions = await session.drain_once()

        # the pause's vendor call won the cycle even at the higher sequence
        assert client.calls[0] == ("interrupt", "sess-1")
        assert [call[0] for call in client.calls[1:]] == []  # no send into a dead turn
        assert session.queued_guidance == ["slow guidance", "more guidance"]
        assert [action.kind for action in actions] == ["pause", "steer", "steer"]
        assert [action.sequence for action in actions] == [3, 1, 2]
        assert session.pause_state.pause_requested is True


# -- R28-12: no control-plane or filesystem work on the turn's event loop --------


class _UnverifiedReceipt:
    """A capture result whose upload could not be read back verified."""

    artifact_id = "artifact:test"
    digest = "artifact:test"
    sequence = 1
    verified = False


class TestBlockingWorkRidesWorkerThreads:
    async def test_a_slow_wip_capture_never_blocks_the_turns_event_loop(self):
        import time as _time

        svc = OperatorControlService()
        client = FakeClaudeClient()
        loop = asyncio.get_running_loop()

        def slow_capture():
            _time.sleep(0.3)  # hashing a working tree: blocking filesystem work
            return _UnverifiedReceipt()

        session = _session(svc, "claude", client, capture=slow_capture)
        await _submit(svc, _cmd("pause", 1))

        beats: list[float] = []
        done = asyncio.Event()
        drain = asyncio.create_task(session.drain_once())

        async def heartbeat() -> None:
            while not done.is_set():
                beats.append(loop.time())
                await asyncio.sleep(0.01)

        pacer = asyncio.create_task(heartbeat())
        await asyncio.wait_for(drain, timeout=5.0)
        done.set()
        await pacer

        # the capture ran (the pause drained, honestly unverified), AND the
        # loop kept beating through it — a blocked loop would show one
        # >= 0.3s gap instead of the ~10ms cadence
        assert len(beats) >= 10
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert max(gaps) < 0.15, gaps
        assert client.calls == [("interrupt", "sess-1")]
        assert actions_capture_landed(session)

    async def test_a_hung_checkpoint_booking_is_bounded_not_blocking(self):
        import time as _time

        from forge.adaptive.control import Mailbox

        class StalledMailbox(Mailbox):
            def checkpoint(self, command_id: str):
                _time.sleep(0.4)  # the plane took the POST and never answered
                return super().checkpoint(command_id)

        svc = OperatorControlService(mailbox=StalledMailbox())
        client = FakeClaudeClient()
        session = _session(svc, "claude", client, checkpoint_ack_wait=0.1)
        await _submit(svc, _cmd("steer", 1, payload={"text": "book me"}))

        loop = asyncio.get_running_loop()
        started = loop.time()
        actions = await asyncio.wait_for(session.drain_once(), timeout=5.0)
        elapsed = loop.time() - started

        # fire-and-forget (R28-11): the gate already decided, so the drain
        # never waits out the hung booking — the honest note rides the journal
        assert elapsed < 0.35, elapsed
        assert actions[0].outcome == "applied"
        assert actions[0].delivery == "application_observed"
        assert "not confirmed" in actions[0].detail["mailbox_status"]
        assert client.calls == [("steer", "sess-1", "book me")]

    async def test_every_journaled_action_carries_its_timestamp(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = _session(svc, "claude", client)
        await _submit(svc, _cmd("pause", 1))
        await _submit(svc, _cmd("steer", 2, payload={"text": "note"}))

        await session.drain_once()

        # R28-29: the sidecar's rows are timeline-projectable as-is
        assert [action.kind for action in session.journal] == ["pause", "steer"]
        assert all(action.at.endswith("+00:00") for action in session.journal)


def actions_capture_landed(session: LaneSteeringSession) -> bool:
    """The pause's detail books the (unverified) capture's honest status."""
    (action,) = session.journal
    return action.detail["pause_status"] in ("paused_failed", "paused_partial", "paused")
