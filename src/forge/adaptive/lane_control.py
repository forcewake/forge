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
:class:`SteeringAction` (applied / refused / ignored / error /
delivery_unknown) — the lane's evidence, distinct from the mailbox
ladder the control plane owns. Applied commands climb the ladder
through the REAL :class:`~forge.adaptive.control.Mailbox` gates
(authorize → CAS apply → checkpoint), so a spent command can never
re-apply.

NXT-12 — intent before effect. The old order marked a command spent
BEFORE the awaited vendor effect, so a crash or a lost response in the
window silently ate the instruction. Every vendor-carrying command now
walks a recorded delivery ladder (:class:`DeliveryRecord`):
``dispatching`` (the intent — on record BEFORE the await) →
``vendor_accepted`` → ``application_observed`` on success, or
``outcome_unknown`` on TimeoutError/ConnectionError — the lost-response
window, NEVER silently retried: the command has left ``pending()`` and
the drain's handled-set refuses redelivery until a reconciler probes.
``uncertain_commands()`` is the reconciler's view;
``delivery_unknown`` is the honest TERMINAL state a probe that cannot
decide closes with (:meth:`mark_delivery_unknown`). The in-memory
mailbox's coarse ``applied`` rung is the INTENT rung here (the durable
mailbox's ``dispatching`` — see :mod:`forge.adaptive.mailbox_db`);
``checkpointed`` remains the application-observed book.

NXT-11 — the lane lifecycle. :mod:`forge.lane_driver` now attaches one
session per driven turn (``FORGE_STEERING_ENABLED``, default OFF this
slice): bind the vendor session id once it exists, run the bounded
mailbox drain CONCURRENTLY with the turn (the async-context seam), and
carry the journal into the lane's meta as ``steering_journal``.

NXT-14 (first half) — urgent pause never waits behind queued slow
guidance: within one drain cycle the interrupt-class commands are
applied first (sequence order still rules inside each class and between
ordinary commands).

R28-11/R28-12 — the drain never blocks the turn's event loop, and no
control-plane I/O is unbounded. Every mailbox ladder call the drain
makes (``pending`` / ``authorize`` / ``apply`` / ``checkpoint`` — over
the remote :class:`~forge.adaptive.lane_channel.LaneControlChannel` each
is a synchronous HTTP round trip) and the pause drain's WIP capture
(filesystem hashing + upload) ride ``asyncio.to_thread`` worker threads,
so the vendor turn sharing this loop is NEVER starved by control-plane
I/O. The fetch and each ack POST stay bounded by the channel's own
``ack_timeout``, and the checkpoint booking — the one ack that runs
AFTER the gate already decided — is additionally fire-and-forget
(:data:`CHECKPOINT_ACK_WAIT_S`): a hung plane can hold the drain at most
that long, and a booking that cannot be confirmed journals the honest
"not confirmed" note while the applied outcome stands on its observed
vendor effect.

R28-29 — every journaled action carries its ``at`` timestamp, and
:meth:`LaneSteeringSession.timeline` projects the journal into the
operator timeline (request / effect / evidence distinguished — see
:mod:`forge.adaptive.operator_timeline`).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
from forge.adaptive.operator_timeline import (
    TimelineEntry,
    timeline_from_journal,
)
from forge.adaptive.wiring import OperatorControlService

__all__ = [
    "CHECKPOINT_ACK_WAIT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DELIVERY_STATES",
    "DeliveryRecord",
    "INTERRUPT_KINDS",
    "LANE_CONTROL_SURFACE",
    "LaneSteeringSession",
    "RESUME_NOTICE",
    "STEER_TEXT_CAP",
    "SteeringAction",
    "drain_cycle_order",
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

#: The interrupt-class commands — applied FIRST within a drain cycle so
#: an urgent pause never waits behind queued slow guidance (NXT-14's
#: first half: a mid-turn ``send`` can sit behind a drain timeout, so
#: head-of-line blocking must not delay the interrupt).
INTERRUPT_KINDS: Final = frozenset({"pause"})

#: The bounded wait for the checkpoint booking — the ONE ladder ack that
#: runs after the gate already decided (R28-11: "fire-and-forget if the
#: gate already decided"). The transport already bounds the POST itself
#: (the channel's ``ack_timeout``); this bound caps how long a hung or
#: slow plane may hold the DRAIN for bookkeeping the vendor effect does
#: not depend on. On elapse the action stands applied — its observed
#: vendor effect is the truth — and the journal carries the honest
#: "not confirmed" note for the reconciler to re-book from.
CHECKPOINT_ACK_WAIT_S: Final = 5.0


def drain_cycle_order(commands: Iterable[ControlCommand]) -> list[ControlCommand]:
    """One drain cycle's application order (NXT-14/R28-11, pinned).

    :data:`INTERRUPT_KINDS` commands come FIRST — regardless of sequence
    — so an urgent pause in the same pending batch never waits behind
    queued slow guidance; inside each class, and among all ordinary
    commands, durable sequence order rules (CTL-07).
    """
    return sorted(
        commands, key=lambda command: (command.kind not in INTERRUPT_KINDS, command.sequence)
    )


#: NXT-12's delivery ladder around every vendor call: the intent is
#: recorded at ``dispatching`` BEFORE the await; a clean vendor return
#: reaches ``vendor_accepted`` and then — once the mailbox checkpoint
#: books the application — ``application_observed``; a lost response
#: (TimeoutError / ConnectionError) parks at ``outcome_unknown`` for the
#: reconciler, and ``delivery_unknown`` is the honest TERMINAL state a
#: probe that cannot decide closes with. Never in this tuple: "applied"
#: — application is OBSERVED, never assumed from an intent.
DELIVERY_STATES: Final = (
    "dispatching",
    "vendor_accepted",
    "outcome_unknown",
    "application_observed",
    "delivery_unknown",
)

DeliveryState = Literal[
    "dispatching",
    "vendor_accepted",
    "outcome_unknown",
    "application_observed",
    "delivery_unknown",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DeliveryRecord:
    """One command's journey through :data:`DELIVERY_STATES` (NXT-12).

    The vendor correlation this record carries (the vendor session id +
    the execution epoch the dispatch ran under) is what a recovery pass
    probes — ``outcome_unknown`` is a question this record makes
    answerable, not a shrug.
    """

    command_id: str
    kind: str
    sequence: int
    state: DeliveryState
    vendor_session_id: str
    execution_epoch: int
    note: str = ""


@dataclass(frozen=True)
class SteeringAction:
    """One journaled control action — the lane's append-only evidence.

    ``outcome`` is one of ``applied`` (the mapping ran), ``refused``
    (a guard or the mailbox gate said no — ``reason`` says which),
    ``ignored`` (the command targets a different run), ``error`` (the
    vendor effect raised decisively — the command has SPENT in the
    ladder, and the journal is the record the reconciler reads), or
    ``delivery_unknown`` (the vendor response was LOST — timeout /
    connection reset / cancelled mid-dispatch: distinct from both
    refusal and error, never silently retried without a probe).

    ``delivery`` is the :data:`DELIVERY_STATES` rung the command's
    effect ended on — ``""`` when no vendor-carrying effect ran
    (refused / ignored). The intent-vs-outcome pair IS NXT-12's journal
    extension: one row says what was INTENDED and what was OBSERVED.
    ``at`` (R28-29) is when the action was journaled — the timestamp the
    operator timeline orders by and the lane meta sidecar carries.
    """

    command_id: str
    kind: str
    outcome: Literal["applied", "refused", "ignored", "error", "delivery_unknown"]
    sequence: int
    delivery: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_now_iso)


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

    Or through :meth:`attach` — the lane_driver seam (LIVE since
    NXT-11: ``forge.lane_driver`` binds, attaches, and carries the
    journal into the lane meta; see :meth:`attach` for the exact shape).

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
        capture: "Callable[[], Any] | None" = None,
        checkpoint_ack_wait: float = CHECKPOINT_ACK_WAIT_S,
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
        # NXT-15: the capture capability — when present, the cooperative
        # pause drain runs the full checkpoint transaction (verified
        # content-addressed WIP); when absent, the pause lands
        # paused_partial honestly (nothing was invented).
        self._capture = capture
        if checkpoint_ack_wait <= 0:
            raise ValueError("checkpoint_ack_wait must be positive")
        self._checkpoint_ack_wait = checkpoint_ack_wait
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
        self._delivery: dict[str, DeliveryRecord] = {}
        self._closing = False
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
        # The closing flag arms the teardown guard: the final drain below
        # still CONSUMES late commands, but it can never start a NEW resume
        # turn into a lane that is packing up (NXT-11's teardown hazard) —
        # a resume seen now is refused for the next lane epoch / reconciler.
        self._closing = True
        # One final synchronous pass: a command submitted while the turn
        # was ending is still consumed and journaled, never dropped.
        await self.drain_once()

    def attach(self, poll_interval: float = DEFAULT_POLL_INTERVAL_S) -> LaneSteeringSession:
        """The lane_driver integration seam (LIVE since NXT-11).

        :mod:`forge.lane_driver` runs this bridge alongside each driven
        turn — ``start_session`` / ``start_thread``, ``bind`` the vendor
        id, the poll-drain loop via the async context, then ``close`` —
        with the SAME client object the lane already drives (gated by
        ``FORGE_STEERING_ENABLED``, default OFF)::

            steering = LaneSteeringSession(
                service=control_service,           # the run's OperatorControlService
                driver=ClaudeSDKAdapter(client),   # the SAME client the lane drives
                driver_kind="claude",
                run_id=os.environ["FORGE_RUN_ID"],
                work_id=os.environ.get("FORGE_WORK_ID") or os.environ["FORGE_RUN_ID"],
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

        Ordering within one cycle is pause-first (NXT-14's first half,
        pinned by :func:`drain_cycle_order`): :data:`INTERRUPT_KINDS`
        commands are applied before queued slow guidance REGARDLESS of
        sequence, so an urgent pause never head-of-line blocks behind a
        steer. Sequence order still rules inside each class and between
        ordinary commands (CTL-07).

        R28-12: the ``pending`` read rides a worker thread — over the
        remote channel it may run one bounded synchronous fetch, and the
        turn sharing this loop must never wait on it.

        Cancellation discipline (NXT-12, extended to the thread hops): a
        command that has LEFT the mailbox view when the drain is torn
        down always leaves a journaled row before the cancellation
        propagates — the consumption's fate is unproven from the point
        of view of the control plane, so it is booked ``delivery_unknown``
        for a probing reconciler, never silently dropped.
        """
        actions: list[SteeringAction] = []
        pending = await self._pending()
        in_flight: ControlCommand | None = None
        try:
            for command in drain_cycle_order(pending):
                if command.command_id in self._handled:
                    continue
                in_flight = command
                action = await self._apply(command)
                self._journal.append(action)
                self._handled[command.command_id] = action.outcome
                actions.append(action)
                in_flight = None
        except asyncio.CancelledError:
            # The mid-effect and post-effect windows journal inside
            # :meth:`_run` (their fate is known); reaching here means the
            # cancellation landed on a BOOKKEEPING await (the gate walk /
            # the checkpoint POST) — the command left the mailbox view,
            # so its effect is unproven: record the intent, journal the
            # honest unknown, and let the cancellation propagate.
            if in_flight is not None and in_flight.command_id not in self._handled:
                self._record_intent(in_flight)
                self._mark_delivery(
                    in_flight,
                    "outcome_unknown",
                    "drain cancelled mid-bookkeeping — probe before any retry",
                )
                torn = SteeringAction(
                    command_id=in_flight.command_id,
                    kind=in_flight.kind,
                    outcome="delivery_unknown",
                    sequence=in_flight.sequence,
                    delivery="outcome_unknown",
                    reason=(
                        "drain torn down between consumption and journaling — "
                        "the effect's fate is unproven; a probe must decide"
                    ),
                )
                self._journal.append(torn)
                self._handled[in_flight.command_id] = torn.outcome
            raise
        return actions

    async def _pending(self) -> list[ControlCommand]:
        """The mailbox's pending view, read off the loop (R28-12).

        The fetch runs on a worker thread and is SHIELDED: if the drain is
        torn down mid-read, the thread's result is still taken (the
        channel may already have consumed its delivery buffer and advanced
        the cursor) and every command in it that this session never
        journaled is booked ``delivery_unknown`` — a cancelled read is
        never a silent loss.
        """
        fetch = asyncio.ensure_future(asyncio.to_thread(self.service.mailbox.pending, self.work_id))
        try:
            return await asyncio.shield(fetch)
        except asyncio.CancelledError:
            fetched: list[ControlCommand] = []
            with contextlib.suppress(Exception):
                fetched = list(await fetch)  # the thread runs to completion
            for command in fetched:
                if command.command_id in self._handled:
                    continue
                self._record_intent(command)
                self._mark_delivery(
                    command,
                    "outcome_unknown",
                    "drain cancelled while the fetch was consuming the mailbox view",
                )
                torn = SteeringAction(
                    command_id=command.command_id,
                    kind=command.kind,
                    outcome="delivery_unknown",
                    sequence=command.sequence,
                    delivery="outcome_unknown",
                    reason=(
                        "drain torn down mid-fetch — the command left the "
                        "mailbox view unprocessed; a probe must decide"
                    ),
                )
                self._journal.append(torn)
                self._handled[command.command_id] = torn.outcome
            raise

    # -- evidence ------------------------------------------------------------

    @property
    def journal(self) -> list[SteeringAction]:
        """The append-only action journal (a copy — evidence is not editable)."""
        return list(self._journal)

    @property
    def timeline(self) -> list[TimelineEntry]:
        """The operator timeline projected from this lane's journal (R28-29).

        The same projection applies to the ``steering_journal`` rows the
        lane meta sidecar carries (the actions, with their ``at``
        timestamps, plus the channel's evidence rows) — see
        :func:`forge.adaptive.operator_timeline.timeline_from_journal`.
        """
        return timeline_from_journal(self._journal)

    def delivery_state(self, command_id: str) -> str:
        """The command's :data:`DELIVERY_STATES` rung, or ``""`` if never dispatched."""
        record = self._delivery.get(command_id)
        return record.state if record is not None else ""

    def uncertain_commands(self) -> list[DeliveryRecord]:
        """The commands whose vendor-effect outcome is UNPROVEN (NXT-12).

        Every record here sits at ``outcome_unknown`` — the request may
        or may not have landed. This is the reconciler's worklist: probe
        the recorded vendor correlation before ANY retry, and when no
        probe can decide, close the command with
        :meth:`mark_delivery_unknown` (the honest terminal state). The
        drain itself never redelivers them.
        """
        return sorted(
            (record for record in self._delivery.values() if record.state == "outcome_unknown"),
            key=lambda record: record.sequence,
        )

    def mark_delivery_unknown(self, command_id: str, note: str = "") -> DeliveryRecord:
        """Close an ``outcome_unknown`` command as ``delivery_unknown``.

        The reconciler's honest give-up: the probe could not decide, so
        the record SAYS unknown — it never rounds up to applied (an
        unproven effect) and never down to refused (a decision nobody
        made). Terminal: only an ``outcome_unknown`` command may be
        closed this way; anything else raises (a decided command cannot
        be un-decided).
        """
        record = self._delivery.get(command_id)
        if record is None:
            raise KeyError(f"unknown command_id {command_id!r} — nothing was dispatched")
        if record.state != "outcome_unknown":
            raise ValueError(
                f"command {command_id!r} is {record.state!r}; only an "
                "outcome_unknown command can be closed as delivery_unknown"
            )
        closed = DeliveryRecord(
            command_id=record.command_id,
            kind=record.kind,
            sequence=record.sequence,
            state="delivery_unknown",
            vendor_session_id=record.vendor_session_id,
            execution_epoch=record.execution_epoch,
            note=note or record.note,
        )
        self._delivery[command_id] = closed
        return closed

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

        NXT-12's intent-before-effect order. The ladder walk first (on a
        worker thread — R28-12: over the remote channel each rung is a
        synchronous HTTP POST, and the turn sharing this loop must never
        wait on it): the CAS apply gates the EFFECT (a command written
        against a stale world expires before any vendor call). Then the
        dispatch INTENT is recorded at ``dispatching`` — BEFORE the
        await — so a crash or a lost response in the window leaves an
        honest record, never an ``applied`` claim the bridge cannot
        support. Outcomes:

        - clean return → ``vendor_accepted``, then the mailbox
          ``checkpoint`` books the application → ``application_observed``
          (the journal row carries the intent-vs-outcome pair). The
          booking is fire-and-forget-bounded (R28-11): the gate already
          decided, so a plane that cannot confirm within
          :data:`CHECKPOINT_ACK_WAIT_S` journals "not confirmed" and the
          applied outcome STANDS — never blocked, never un-applied;
        - TimeoutError / ConnectionError → ``outcome_unknown`` and the
          action outcome ``delivery_unknown`` — the request may or may
          not have landed. The command has left ``pending()`` (the
          coarse mailbox sits at its intent rung) and the handled-set
          bars the drain from redelivering it: NEVER a silent retry —
          :meth:`uncertain_commands` hands it to a probing reconciler;
        - cancellation between intent and effect → the same unknown
          booking, journaled in place (the drain cannot finish), then
          the cancellation propagates;
        - any other exception → ``error`` — the adapter contract: a
          decisive raise means the call did NOT go through (transport
          uncertainty arrives as Timeout/ConnectionError), so the rung
          stays ``dispatching`` and the journal row is the spent record.
        """
        ok, gate = await asyncio.to_thread(self._gate_walk, command)
        if not ok:
            return self._refused(command, gate)
        enriched = dict(detail)
        self._record_intent(command)
        try:
            await effect(enriched)
        except asyncio.CancelledError:
            # The crash-between-intent-and-effect window, in-process:
            # teardown or a bounding wait_for cancelled the dispatch.
            # The effect's fate is unproven — book it, journal it (the
            # drain loop unwinds past its own append), and re-raise.
            self._mark_delivery(
                command,
                "outcome_unknown",
                "cancelled between intent and vendor effect — probe before any retry",
            )
            cancelled = SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="delivery_unknown",
                sequence=command.sequence,
                delivery="outcome_unknown",
                reason="dispatch cancelled mid-flight — the vendor effect's fate is unproven",
            )
            self._journal.append(cancelled)
            self._handled[command.command_id] = cancelled.outcome
            raise
        except (TimeoutError, ConnectionError) as exc:
            self._mark_delivery(
                command,
                "outcome_unknown",
                f"vendor response lost ({exc!r}) — reconcile before any retry",
            )
            return SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="delivery_unknown",
                sequence=command.sequence,
                delivery="outcome_unknown",
                reason=(
                    f"vendor response lost: {exc!r} — the effect may or may not "
                    "have landed; a probe must decide, never a blind retry"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — journal the failure, keep the loop alive
            return SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="error",
                sequence=command.sequence,
                delivery="dispatching",
                reason=f"vendor effect failed: {exc}",
            )
        self._mark_delivery(command, "vendor_accepted", "the vendor took the effect")
        booking = asyncio.ensure_future(self._checkpoint(command))
        try:
            status = await asyncio.shield(booking)
        except asyncio.CancelledError:
            # Teardown landed on the post-effect booking (a bookkeeping
            # await since R28-12): the effect IS observed, and the booking
            # is already in flight and BOUNDED by its own wait — let it
            # land, journal the applied action with whichever status it
            # produced, then let the cancellation propagate.
            status = "checkpoint not confirmed (drain cancelled post-effect)"
            with contextlib.suppress(asyncio.CancelledError):
                status = await booking
            self._mark_delivery(
                command,
                "application_observed",
                f"drain cancelled after the observed effect — booking: {status}",
            )
            landed = SteeringAction(
                command_id=command.command_id,
                kind=command.kind,
                outcome="applied",
                sequence=command.sequence,
                delivery="application_observed",
                detail={**enriched, "mailbox_status": status},
            )
            self._journal.append(landed)
            self._handled[command.command_id] = landed.outcome
            raise
        self._mark_delivery(command, "application_observed", f"mailbox status: {status}")
        return SteeringAction(
            command_id=command.command_id,
            kind=command.kind,
            outcome="applied",
            sequence=command.sequence,
            delivery="application_observed",
            detail={**enriched, "mailbox_status": status},
        )

    def _record_intent(self, command: ControlCommand) -> None:
        """Put the dispatch intent on record BEFORE the vendor call.

        The vendor correlation (session id + execution epoch) travels
        with the record — the pair a recovery pass probes. Over the
        in-memory mailbox the coarse ``applied`` rung IS this intent
        (the durable mailbox names it ``dispatching``).
        """
        self._delivery[command.command_id] = DeliveryRecord(
            command_id=command.command_id,
            kind=command.kind,
            sequence=command.sequence,
            state="dispatching",
            vendor_session_id=self.vendor_session_id,
            execution_epoch=self.execution_epoch,
            note="effect intended — recorded before the vendor call",
        )

    def _mark_delivery(self, command: ControlCommand, state: DeliveryState, note: str) -> None:
        """Advance the command's delivery record to *state*."""
        record = self._delivery.get(command.command_id)
        if record is None:  # unreachable: _record_intent precedes every effect
            self._record_intent(command)
            record = self._delivery[command.command_id]
        self._delivery[command.command_id] = DeliveryRecord(
            command_id=record.command_id,
            kind=record.kind,
            sequence=record.sequence,
            state=state,
            vendor_session_id=record.vendor_session_id,
            execution_epoch=record.execution_epoch,
            note=note,
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
            # NXT-14: typed interrupt outcome + measured latency. A clean
            # vendor return is ONLY an acknowledgment — the pause's truth
            # stays the quiescence/checkpoint state below, never this ack
            # (a timed-out interrupt cannot produce paused success).
            import time as _time

            started = _time.monotonic()
            try:
                await self._interrupt_vendor()
                detail["interrupt_outcome"] = "acknowledged"
            except (TimeoutError, ConnectionError) as exc:
                # The vendor never proved receipt — unknown, honestly.
                detail["interrupt_outcome"] = "unknown"
                detail["interrupt_error"] = f"{type(exc).__name__}"
            detail["interrupt_latency_s"] = round(_time.monotonic() - started, 3)
            self._pause = send_interrupt(self._pause)
            # R28-12: the cooperative drain may run the REAL capture
            # capability (hash the working tree, upload blobs) — blocking
            # filesystem work that rides a worker thread so the turn's
            # own loop is never held by it.
            self._pause = await asyncio.to_thread(
                drain_turn, self._pause, cooperative=True, capture=self._capture
            )
            detail["pause"] = "interrupt-sent"
            detail["pause_status"] = self._pause.pause_status
            if self._pause.failure_reason:
                detail["pause_failure"] = self._pause.failure_reason[:300]
            detail["wip_artifact_id"] = self._pause.wip_artifact_id
            if self._pause.checkpoint_receipt is not None:
                detail["checkpoint_receipt"] = {
                    "checkpoint_id": getattr(self._pause.checkpoint_receipt, "checkpoint_id", None),
                    "verified": getattr(self._pause.checkpoint_receipt, "verified", None),
                    "remote_ref": getattr(self._pause.checkpoint_receipt, "remote_ref", ""),
                }
            # NXT-14: queued guidance is RETAINED explicitly when an
            # urgent control wins — never silently dropped nor lost in
            # ordering; the resume turn carries it.
            detail["retained_guidance"] = list(self.queued_guidance)

        return await self._run(command, effect=effect, detail={})

    async def _do_resume(self, command: ControlCommand) -> SteeringAction:
        if self._closing:
            return self._refused(
                command,
                "the lane is closing — a resume turn belongs to the next lane "
                "epoch or the reconciler, never to teardown",
            )
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

    def _gate_walk(self, command: ControlCommand) -> tuple[bool, str]:
        """Walk authorize + CAS-apply (sync); ``(ok, status-or-reason)``.

        Runs on a worker thread (:meth:`_run`) — each rung over the
        remote channel is a bounded synchronous POST whose RESULT the
        vendor effect is gated on, so it is awaited, but never on the
        turn's own loop.
        """
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

    async def _checkpoint(self, command: ControlCommand) -> str:
        """Book the application — bounded, fire-and-forget (R28-11/R28-12).

        The gate already decided and the vendor effect is observed, so
        this booking is best-effort I/O the applied outcome never hangs
        on: the POST rides a worker thread and waits at most
        ``checkpoint_ack_wait``. A booking that cannot be confirmed (the
        bound elapsed, or the plane refused) returns the honest note —
        the reconciler re-books from the journal; the action stays
        ``applied`` on its OBSERVED effect, and a late server-side
        completion is harmless (the rung walk is idempotent per command).
        """
        try:
            booked = await asyncio.wait_for(
                asyncio.to_thread(self.service.mailbox.checkpoint, command.command_id),
                timeout=self._checkpoint_ack_wait,
            )
        except TimeoutError:
            return "checkpoint not confirmed (bounded wait elapsed) — re-book from the journal"
        except (ValueError, KeyError, PermissionError) as exc:
            return f"checkpoint failed: {exc}"
        return booked.status

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
