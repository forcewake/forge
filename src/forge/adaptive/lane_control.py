"""Steering bridge: operator ControlCommands -> ONE running interactive lane.

The interactive drivers (:mod:`forge.adaptive.drivers`) and the control
substrate (:class:`~forge.adaptive.control.Mailbox`,
:class:`~forge.adaptive.wiring.OperatorControlService`) both exist and
are live-proven, but nothing connects them: an operator could ``/pause``
a WORK while the RUNNING vendor session in the execution lane never
felt it. This module is that connection — one
:class:`LaneSteeringSession` per RUNNING lane, consuming the work's
mailbox commands and applying ONLY the runbook's safe mappings (§3 of
``docs/operations/adaptive-runbook.md``):

- ``pause`` → the driver's interrupt + the lane's
  :class:`~forge.adaptive.control.PauseState`, in CTL-05's order —
  ``pause_requested`` is recorded BEFORE the interrupt is sent, and the
  drain is treated as cooperative (the drivers bind interrupt completion
  to the vendor's own turn-completed signal).
- ``steer`` → mid-turn guidance, guidance TEXT only: ``send()`` on a
  claude session, ``steer_active_turn()`` on a codex thread (EXE-06 —
  pure input injection). While paused the text is QUEUED, not pushed
  into a dead turn; the resume turn delivers it.
- ``answer`` → steered with the answer text (the same guards as steer).
- ``resume`` → a NEW turn carrying the queued operator text, gated by
  :func:`~forge.adaptive.control.resume_check` (confirmed checkpoint,
  snapshot, permissions) — ``send_turn``/``send``/``prompt``.

What is REFUSED, with the documented reason:

- ``amend`` / ``approve-revision`` are NOT steering: they belong to
  plan revision (the ChangeProposal human gate / the CAS-approved
  material revision), never to the vendor session.
- acceptance-weakening text and constraint-introducing text (CTL-07):
  the revision gate, not the steering channel.
- empty or over-cap text; mid-turn steering on a profile that does not
  advertise ``live_input`` (the opencode lane was tested for interrupt
  only — fail closed, never guess a capability); resume while not
  paused; any command scoped to a DIFFERENT run id (ignored, left in
  the mailbox for the owning lane).

The permission posture is untouched STRUCTURALLY: the bridge's entire
vendor surface is :data:`LANE_CONTROL_SURFACE` — the four text-carrying
guidance methods (``steer`` / ``steer_active_turn`` / ``send_turn`` /
``prompt``) plus the interrupt methods (``interrupt`` / ``abort``).
Every one of them takes a vendor id and, at most, text — no method on
the bridge can carry a permission, flag, or configuration argument, so
no command can alter what the lane is allowed to DO (EXE-06).

Every handled command is journaled as an append-only
:class:`SteeringAction` (applied / refused / ignored / error) — the
lane's evidence, distinct from the mailbox ladder the control plane
owns. Applied commands climb the ladder through the REAL
:class:`~forge.adaptive.control.Mailbox` gates (authorize → CAS apply →
checkpoint), so a spent command can never re-apply.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from forge.adaptive.adapters import (
    ClaudeSDKAdapter,
    CodexAppAdapter,
    OpenCodeAdapter,
)
from forge.adaptive.control import (
    PauseState,
    classify_instruction,
    drain_turn,
    request_pause,
    resume_check,
    send_interrupt,
)
from forge.adaptive.models import ControlCommand
from forge.adaptive.wiring import OperatorControlService

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "LANE_CONTROL_SURFACE",
    "LaneSteeringSession",
    "RESUME_NOTICE",
    "STEER_TEXT_CAP",
    "SteeringAction",
]

DriverKind = Literal["claude", "codex", "opencode"]

#: driver kind -> the sdk its adapter profile must carry. The pairing is
#: checked at construction: a "codex" session over a claude adapter is a
#: wiring error, not a runtime surprise.
_KIND_TO_SDK: Final[dict[str, str]] = {
    "claude": "claude-sdk",
    "codex": "codex-app",
    "opencode": "opencode-server",
}

#: The bridge's ENTIRE vendor surface, as adapter method names per driver
#: kind. The guidance methods carry text; the interrupt methods carry
#: only the vendor id. No name here accepts a permission, flag, or
#: configuration argument — that closure IS the EXE-06 guarantee: a
#: steering command cannot re-permission the turn it steers.
LANE_CONTROL_SURFACE: Final[dict[str, frozenset[str]]] = {
    "claude": frozenset({"interrupt", "steer"}),
    "codex": frozenset({"interrupt", "steer_active_turn", "send_turn"}),
    "opencode": frozenset({"abort", "prompt"}),
}

#: Guidance longer than this is refused — bounded steering is a note,
#: not a second brief.
STEER_TEXT_CAP: Final = 4000

#: The turn text a resume with an EMPTY queue carries. A bare "continue"
#: is honest here: the operator resumed without adding guidance.
RESUME_NOTICE: Final = "Operator resumed the lane. Continue from the last checkpoint."

#: Kinds that are NOT steering — refused with the runbook's own reason.
_NOT_STEERING: Final[dict[str, str]] = {
    "amend": (
        "amend promotes a ChangeProposal for the human gate; it does not steer the vendor session"
    ),
    "approve-revision": (
        "approve-revision activates a CAS-approved material revision; it does "
        "not steer the vendor session"
    ),
}

#: The bounded poll cadence of the drain loop (same order as the lane's
#: own drain poll — no busy loop between mailbox checks).
DEFAULT_POLL_INTERVAL_S: Final = 0.25


@dataclass(frozen=True)
class SteeringAction:
    """One journaled control action — the lane's append-only evidence.

    ``outcome`` is one of ``applied`` (the mapping ran), ``refused``
    (a guard or the mailbox gate said no — ``reason`` says which),
    ``ignored`` (the command targets a different run), or ``error``
    (the vendor effect raised — the command has SPENT in the ladder,
    and the journal is the record the reconciler reads).
    """

    command_id: str
    kind: str
    outcome: Literal["applied", "refused", "ignored", "error"]
    sequence: int
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class LaneSteeringSession:
    """One RUNNING interactive lane's control loop over the work mailbox.

    Consumes the work's pending :class:`~forge.adaptive.models.ControlCommand`
    records from the :class:`~forge.adaptive.wiring.OperatorControlService`
    mailbox (in durable sequence order — CTL-07) and applies only the
    safe mappings. Usable two ways:

    Standalone, as an async context (the mailbox is drained concurrently
    with the agent turn, at a bounded poll cadence)::

        async with LaneSteeringSession(
            service=svc, driver=adapter, driver_kind="codex",
            run_id="run-1", work_id="wp-1", vendor_session_id="th-1",
        ) as steer:
            await the_agent_turn()
        evidence = steer.journal

    Or through :meth:`attach` — the documented lane_driver seam (see its
    docstring for the exact future integration).

    The lane keeps its OWN :class:`~forge.adaptive.control.PauseState`
    (the execution-side view; the service's is the control-plane view)
    and its own queue of guidance that arrived while paused — delivered
    as the resume turn's text.
    """

    def __init__(
        self,
        *,
        service: OperatorControlService,
        driver: ClaudeSDKAdapter | CodexAppAdapter | OpenCodeAdapter,
        driver_kind: DriverKind,
        run_id: str,
        work_id: str,
        vendor_session_id: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        plan_revision: int = 1,
        execution_epoch: int = 1,
        snapshot_available: bool = True,
        permissions_valid: bool = True,
        actor_scopes: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Configure one lane bridge; nothing runs until the context (or a drain).

        ``vendor_session_id`` may be supplied later via :meth:`bind` —
        the vendor id does not exist until the lane starts its turn.
        ``plan_revision`` / ``execution_epoch`` are the lane's CURRENT
        world, checked by the mailbox's CAS apply (a command written
        against another world expires). ``snapshot_available`` /
        ``permissions_valid`` are the lane's inputs to
        :func:`~forge.adaptive.control.resume_check`. ``actor_scopes``
        overrides the ladder's authorization view; the default derives
        "this actor, as recorded" per command (the control plane
        accepted the command into the mailbox — the lane does not
        re-decide authorization, it books it).
        """
        if driver_kind not in _KIND_TO_SDK:
            raise ValueError(
                f"driver_kind must be one of {sorted(_KIND_TO_SDK)}, got {driver_kind!r}"
            )
        expected_sdk = _KIND_TO_SDK[driver_kind]
        if driver.profile.sdk != expected_sdk:
            raise ValueError(
                f"driver_kind {driver_kind!r} requires a {expected_sdk} adapter "
                f"profile, got {driver.profile.sdk!r}"
            )
        if not run_id or not work_id:
            raise ValueError("run_id and work_id must be non-empty")
        self.service = service
        self.driver = driver
        self.driver_kind: DriverKind = driver_kind
        self.run_id = run_id
        self.work_id = work_id
        self.vendor_session_id = vendor_session_id
        self.plan_revision = plan_revision
        self.execution_epoch = execution_epoch
        self.snapshot_available = snapshot_available
        self.permissions_valid = permissions_valid
        self._actor_scopes = actor_scopes
        self._poll_interval = poll_interval
        self._pause = PauseState(work_id=work_id)
        self._queued: list[str] = []
        self._journal: list[SteeringAction] = []
        self._handled: dict[str, str] = {}
        self._loop_task: asyncio.Task[None] | None = None

    # -- the async-context surface ------------------------------------------

    async def __aenter__(self) -> LaneSteeringSession:
        self._loop_task = asyncio.create_task(self._poll_loop(), name="lane-steering-drain")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # One final synchronous pass: a command submitted while the turn
        # was ending is still consumed and journaled, never dropped.
        await self.drain_once()

    def attach(self, poll_interval: float = DEFAULT_POLL_INTERVAL_S) -> LaneSteeringSession:
        """The lane_driver integration seam (lane_driver.py itself is NOT touched).

        A future ``forge.lane_driver`` turn would run this bridge
        alongside its ONE driven turn — ``start_session``, then the
        poll-drain loop, then ``close`` — with the SAME client object
        the lane already drives::

            steering = LaneSteeringSession(
                service=control_service,           # the run's OperatorControlService
                driver=ClaudeSDKAdapter(client),   # the SAME client the lane drives
                driver_kind="claude",
                run_id=os.environ["FORGE_RUN_ID"],
                work_id=os.environ["FORGE_WORK_ID"],
            )
            session_id = await client.start_session(task)
            steering.bind(session_id)              # the vendor id exists only now
            async with steering.attach(poll_interval=poll_s):
                result = await _drain_until_result(client, session_id, ...)
            meta["steering_journal"] = [asdict(a) for a in steering.journal]

        ``bind`` after ``start_session`` resolves the ordering: the
        vendor session id does not exist until the turn starts, and any
        command arriving before the bind is refused ("no vendor session
        bound") rather than mis-delivered to a guess.
        """
        self._poll_interval = poll_interval
        return self

    def bind(self, vendor_session_id: str) -> None:
        """Record the vendor session/thread id once the lane's turn started."""
        if not vendor_session_id:
            raise ValueError("vendor_session_id must be non-empty")
        self.vendor_session_id = vendor_session_id

    # -- the drain loop ------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            await self.drain_once()
            await asyncio.sleep(self._poll_interval)

    async def drain_once(self) -> list[SteeringAction]:
        """Apply every owned pending command once, in durable sequence order.

        Each command id is handled at most once per session (redelivery
        must not spend another iteration, CTL-04); a command this lane
        refused or ignored stays in the mailbox at its current rung for
        the control plane's reconciler — only APPLIED commands climb to
        ``checkpointed`` and leave the pending view.
        """
        actions: list[SteeringAction] = []
        for command in self.service.mailbox.pending(self.work_id):
            if command.command_id in self._handled:
                continue
            action = await self._apply(command)
            self._journal.append(action)
            self._handled[command.command_id] = action.outcome
            actions.append(action)
        return actions

    # -- evidence ------------------------------------------------------------

    @property
    def journal(self) -> list[SteeringAction]:
        """The append-only action journal (a copy — evidence is not editable)."""
        return list(self._journal)

    @property
    def pause_state(self) -> PauseState:
        """The lane's execution-side pause book (CTL-05)."""
        return self._pause

    @property
    def queued_guidance(self) -> list[str]:
        """Guidance queued while paused, to be delivered by the resume turn."""
        return list(self._queued)

    # -- one command ---------------------------------------------------------

    async def _apply(self, command: ControlCommand) -> SteeringAction:
        scoped_run = command.payload.get("run_id")
        if isinstance(scoped_run, str) and scoped_run != self.run_id:
            return SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="ignored",
                sequence=command.sequence,
                reason=(
                    f"command targets run {scoped_run!r}; this lane is run "
                    f"{self.run_id!r} — left in the mailbox for its owner"
                ),
            )
        not_steering = _NOT_STEERING.get(command.kind)
        if not_steering is not None:
            return self._refused(command, not_steering)
        handler = {
            "pause": self._do_pause,
            "resume": self._do_resume,
            "steer": self._do_steer,
            "answer": self._do_answer,
        }.get(command.kind)
        if handler is None:
            return self._refused(command, f"unknown command kind {command.kind!r}")
        return await handler(command)

    def _refused(self, command: ControlCommand, reason: str) -> SteeringAction:
        return SteeringAction(
            command_id=command.command_id,
            kind=command.kind,
            outcome="refused",
            sequence=command.sequence,
            reason=reason,
        )

    async def _run(
        self,
        command: ControlCommand,
        *,
        effect: Callable[[dict[str, Any]], Awaitable[None]],
        detail: dict[str, Any],
    ) -> SteeringAction:
        """Gate one command through the mailbox ladder, run the effect, journal.

        The ladder order is the review's: the CAS apply gates the EFFECT
        (a command written against a stale world expires before any
        vendor call), the effect runs at ``applied``, and
        ``checkpoint`` records that the action reached the durable
        journal. An effect that raises leaves the command spent at
        ``applied`` with an ``error`` action — the honest record, never
        a silent retry.
        """
        ok, gate = self._gate(command)
        if not ok:
            return self._refused(command, gate)
        enriched = dict(detail)
        try:
            await effect(enriched)
        except Exception as exc:  # noqa: BLE001 — journal the failure, keep the loop alive
            return SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="error",
                sequence=command.sequence,
                reason=f"vendor effect failed: {exc}",
            )
        status = self._checkpoint(command)
        return SteeringAction(
            command_id=command.command_id,
            kind=command.kind,
            outcome="applied",
            sequence=command.sequence,
            detail={**enriched, "mailbox_status": status},
        )

    # -- the four safe mappings ----------------------------------------------

    async def _do_pause(self, command: ControlCommand) -> SteeringAction:
        if self._pause.pause_requested and self._pause.interrupt_sent:
            # Pause is idempotent: a second pause command journals (it
            # applied — as a no-op) but NEVER sends a second interrupt.
            return await self._run(command, effect=_no_effect, detail={"pause": "already-paused"})
        if not self.vendor_session_id:
            return self._refused(
                command, "no vendor session bound — bind the vendor id before pausing"
            )

        async def effect(detail: dict[str, Any]) -> None:
            # CTL-05's ordering: the pause is on record BEFORE the
            # interrupt is sent; the drain is cooperative (the drivers
            # bind interrupt completion to the vendor's own signal).
            self._pause = request_pause(self._pause)
            await self._interrupt_vendor()
            self._pause = send_interrupt(self._pause)
            self._pause = drain_turn(self._pause, cooperative=True)
            detail["pause"] = "interrupt-sent"
            detail["wip_artifact_id"] = self._pause.wip_artifact_id

        return await self._run(command, effect=effect, detail={})

    async def _do_resume(self, command: ControlCommand) -> SteeringAction:
        if not self._pause.pause_requested:
            return self._refused(command, "nothing to resume: the lane is not paused")
        ok, why = resume_check(
            self._pause,
            self.snapshot_available,
            self.plan_revision,
            self.permissions_valid,
        )
        if not ok:
            return self._refused(command, why)
        if not self.vendor_session_id:
            return self._refused(
                command, "no vendor session bound — bind the vendor id before resuming"
            )
        text = "\n\n".join(self._queued) if self._queued else RESUME_NOTICE

        async def effect(detail: dict[str, Any]) -> None:
            await self._start_turn(text)
            self._queued.clear()
            self._pause = replace(
                self._pause,
                pause_requested=False,
                interrupt_sent=False,
                checkpoint_captured=False,
                wip_artifact_id=None,
            )
            detail["new_turn"] = True
            detail["text_chars"] = len(text)

        return await self._run(command, effect=effect, detail={})

    async def _do_steer(self, command: ControlCommand) -> SteeringAction:
        return await self._deliver(command)

    async def _do_answer(self, command: ControlCommand) -> SteeringAction:
        return await self._deliver(command)

    async def _deliver(self, command: ControlCommand) -> SteeringAction:
        """steer/answer share one bounded delivery path (CTL-07)."""
        text = str(command.payload.get("text", ""))
        question_id = str(command.payload.get("question_id", "") or "")
        if not text.strip():
            return self._refused(command, "empty guidance text")
        if len(text) > STEER_TEXT_CAP:
            return self._refused(
                command, f"guidance text exceeds the {STEER_TEXT_CAP}-character cap"
            )
        classification = classify_instruction(text)
        if classification == "acceptance_change":
            return self._refused(command, "acceptance policy change requires the revision gate")
        if classification == "amend":
            return self._refused(
                command,
                "constraint changes are promoted to a ChangeProposal at the "
                "revision gate, not delivered as guidance",
            )
        deliver = f"[operator answer to question {question_id}] {text}" if question_id else text
        if self._pause.pause_requested:
            # The turn is dead (interrupted); guidance queued now is
            # delivered BY the resume turn — never pushed into a void.
            async def queue(detail: dict[str, Any]) -> None:
                self._queued.append(deliver)
                detail["queued_for_resume"] = True

            return await self._run(command, effect=queue, detail={})
        if not self.vendor_session_id:
            return self._refused(
                command, "no vendor session bound — bind the vendor id before steering"
            )
        if not self.driver.profile.supports("live_input"):
            return self._refused(
                command,
                f"driver profile {self.driver.profile.profile_id!r} does not "
                "advertise live_input; mid-turn steering is untested for this lane",
            )

        async def send(detail: dict[str, Any]) -> None:
            await self._steer_running(deliver)
            detail["delivered"] = "mid-turn"

        return await self._run(command, effect=send, detail={})

    # -- the ladder ----------------------------------------------------------

    def _gate(self, command: ControlCommand) -> tuple[bool, str]:
        """Walk authorize + CAS-apply; ``(ok, status-or-reason)``."""
        try:
            current = command
            if current.status == "received":
                current = self.service.mailbox.authorize(
                    current.command_id, self._scopes_for(current)
                )
            if current.status == "authorized":
                current = self.service.mailbox.apply(
                    current.command_id,
                    current_plan_revision=self.plan_revision,
                    current_execution_epoch=self.execution_epoch,
                )
            if current.status == "expired":
                return False, (
                    "expired: the command was written against a plan revision / "
                    "execution epoch this lane no longer holds"
                )
            return True, current.status
        except (ValueError, PermissionError, KeyError) as exc:
            return False, f"mailbox gate refused: {exc}"

    def _checkpoint(self, command: ControlCommand) -> str:
        try:
            return self.service.mailbox.checkpoint(command.command_id).status
        except (ValueError, KeyError) as exc:
            return f"checkpoint failed: {exc}"

    def _scopes_for(self, command: ControlCommand) -> dict[str, tuple[str, ...]]:
        if self._actor_scopes is not None:
            return self._actor_scopes
        return {command.actor_origin: (command.actor_ref,)}

    # -- the ONLY vendor calls (see LANE_CONTROL_SURFACE) --------------------

    async def _interrupt_vendor(self) -> None:
        if self.driver_kind == "opencode":
            await self.driver.abort(self.vendor_session_id)
        else:
            await self.driver.interrupt(self.vendor_session_id)

    async def _steer_running(self, text: str) -> None:
        if self.driver_kind == "claude":
            await self.driver.steer(self.vendor_session_id, text)
        elif self.driver_kind == "codex":
            await self.driver.steer_active_turn(self.vendor_session_id, text)
        else:  # unreachable: _deliver refuses without live_input, which opencode lacks
            raise RuntimeError("unreachable: this driver profile has no live_input")

    async def _start_turn(self, text: str) -> None:
        """The resume mapping: a NEW turn carrying the queued operator text.

        Per driver kind: ``send_turn`` on a codex thread (a queued new
        turn IS the vendor's conversation model), ``send`` on a claude
        session (a send into a quiescent session begins the next turn),
        ``prompt`` on an opencode session.
        """
        if self.driver_kind == "codex":
            await self.driver.send_turn(self.vendor_session_id, text)
        elif self.driver_kind == "claude":
            await self.driver.steer(self.vendor_session_id, text)
        else:
            await self.driver.prompt(self.vendor_session_id, text)


async def _no_effect(_detail: dict[str, Any]) -> None:
    """The idempotent-pause effect: the command applied as a no-op."""
