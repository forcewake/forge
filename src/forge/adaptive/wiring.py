"""Service wiring for the adaptive substrate (v0.18.0 campaign).

Connects the v0.17.0 contracts to the EXISTING production machinery —
the same RunService/harness-backend/gateway seams the classic /implement
path uses. Three wirings:

- :class:`DiscoveryService` — creates and dispatches a durable
  :class:`~forge.adaptive.discovery.DiscoveryRun` through the EXISTING
  CI-harness backend (``dispatch_target()`` is the CI execution profile,
  never the privileged API process). A completed discovery's evidence
  bundle is durable in the run's evidence; restart resumes it.
- :class:`OperatorControlService` — the mailbox-backed command surface
  (/pause /steer /answer /resume) that the provider gateways route into.
  The state ladder and CAS semantics come from
  :mod:`forge.adaptive.control`; this service owns persistence and the
  pause fence against the publication epoch.
- :class:`WorkPackageCoordination` — a parent WorkPackage creating and
  driving child runs phase-by-phase via a caller-supplied child-run
  factory (the provider-agnostic seam: each child is whatever the
  existing RunService already starts).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from forge.adaptive.control import (
    BroadcastCommand,
    Mailbox,
    PauseState,
    classify_instruction,
    recorded_pause_async,
    send_interrupt,
)
from forge.adaptive.discovery import DiscoveryRun
from forge.adaptive.mailbox_bridge import AsyncMailboxSurface, control_surface_for
from forge.adaptive.models import ControlCommand
from forge.adaptive.workpackage import WorkPackage, compile_dependencies

logger = logging.getLogger(__name__)

ChildRunFactory = Callable[[str, str], Awaitable[str]]
"""async (item_id, repository_id) -> child run id — the provider seam."""

#: The env var that mounts the durable Postgres control mailbox
#: (``forge.adaptive.mailbox_db.PostgresMailbox``) behind the service.
#: Default (unset / any other value): the in-memory reference mailbox —
#: the flag-gated honest rollout; nothing changes until it is set.
FORGE_CONTROL_MAILBOX_ENV: Final = "FORGE_CONTROL_MAILBOX"

#: The one mounted spelling. Anything else (typo included) is memory.
_CONTROL_MAILBOX_POSTGRES: Final = "postgres"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DiscoveryService — DiscoveryRun through the existing harness backend
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryService:
    """Creates, dispatches, and completes durable discovery stages.

    The dispatch target is the caller's harness-start callable — the SAME
    ``_advance_harness``/``backend.start`` leg the classic implement path
    uses (the review's DSC-01: dispatch through an existing CI execution
    profile, not inside the privileged API process). Discovery creates NO
    source-control branch, commit, or PR: the brief is a READ-ONLY
    research task over the authorized snapshot set.
    """

    harness_start: Callable[[str, str, str], Awaitable[None]]
    """async (discovery_id, work_id, brief) — the existing dispatch leg."""

    async def begin_discovery(
        self, work_id: str, snapshot_set_digest: str, brief: str
    ) -> DiscoveryRun:
        """Create a pending DiscoveryRun and dispatch it through the lane."""
        discovery = DiscoveryRun(
            discovery_id=f"disc-{uuid.uuid4().hex[:12]}",
            work_id=work_id,
            snapshot_set_digest=snapshot_set_digest,
        ).start()
        await self.harness_start(discovery.discovery_id, work_id, brief)
        return discovery

    async def redispatch_after_question(self, discovery: DiscoveryRun, brief: str) -> DiscoveryRun:
        """Re-dispatch a waiting_question discovery after the answer."""
        if discovery.status != "waiting_question":
            raise ValueError(
                f"discovery {discovery.discovery_id} is {discovery.status}, "
                "not waiting_question — nothing to redispatch"
            )
        resolved = discovery.resolve_question(
            discovery.open_questions[0] if discovery.open_questions else ""
        )
        await self.harness_start(discovery.discovery_id, discovery.work_id, brief)
        return resolved

    def resume_completed(self, discovery: DiscoveryRun) -> DiscoveryRun:
        """A restart resumes the durable bundle — the probes are NOT repaid."""
        return discovery.resume(discovery) if discovery.status == "complete" else discovery

    def block_on_critical(self, discovery: DiscoveryRun, reason: str) -> DiscoveryRun:
        """Unresolved critical questions block planning, never defaults."""
        return discovery.block(reason)


# ---------------------------------------------------------------------------
# OperatorControlService — mailbox-backed /pause /steer /answer /resume
# ---------------------------------------------------------------------------


@dataclass
class OperatorControlService:
    """The durable operator-command surface behind the provider gateways.

    The gateway parses a note into a ControlCommand and calls
    :meth:`submit`; this service owns the mailbox, the pause fence (the
    publication epoch closes BEFORE the interrupt is sent — CTL-05's
    ordering is the guarantee), and the answer routing to the waiting
    discovery. Sequence enforcement and idempotency-key dedup come from
    the mailbox (a redelivered command never spends another iteration).
    Accepted steers are mailbox records too (see :meth:`steer`) — the
    surface a running lane's steering bridge consumes.

    The mailbox seam is :attr:`surface` — the unified ASYNC surface of
    :mod:`forge.adaptive.mailbox_bridge`, satisfied verbatim by BOTH
    implementations: the in-memory reference :class:`Mailbox` wrapped in
    :class:`~forge.adaptive.mailbox_bridge.AsyncMailboxAdapter`, and the
    durable :class:`~forge.adaptive.mailbox_db.PostgresMailbox` used
    directly (mounted by :func:`control_service_from_env` behind
    ``FORGE_CONTROL_MAILBOX=postgres``). Every method below awaits that
    seam, so the same service code runs over either store. The
    construction field :attr:`mailbox` stays the RAW implementation
    object: over memory it remains the sync reference view the lane
    steering bridge and the tests read (``mailbox.commands``,
    ``mailbox.pending``); over Postgres it is the mailbox itself.
    """

    mailbox: Mailbox | AsyncMailboxSurface = field(default_factory=Mailbox)
    pause_states: dict[str, PauseState] = field(default_factory=dict)
    #: The unified async seam every service method awaits (CTL-04). Built
    #: in ``__post_init__`` — the adapter over the in-memory mailbox, or
    #: an already-async mailbox (PostgresMailbox) as itself.
    surface: AsyncMailboxSurface = field(init=False)

    def __post_init__(self) -> None:
        self.surface = control_surface_for(self.mailbox)

    async def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        """Gateway entry: dedups by idempotency key, enforces sequences."""
        return await self.surface.submit(command)

    async def pause(
        self,
        work_id: str,
        actor: str,
        idempotency_key: str,
        *,
        work_scoped: bool = False,
        recipients: Sequence[str] = (),
    ) -> PauseState | BroadcastCommand:
        """CTL-05 + NXT-09: the durable command row FIRST, then the fence.

        ``recorded_pause_async`` submits the command BEFORE any state
        mutation: a redelivered pause (same idempotency key) is refused
        with the pause state UNCHANGED — the publication epoch is not
        bumped a second time. Only a NEW command row sets
        ``pause_requested``, bumps the fence, and then the interrupt goes
        out (CTL-05's ordering preserved: pause on record before the
        interrupt).

        ``work_scoped=True`` (NXT-13) fans the SAME parent intent out to
        a FIXED recipient set — the work's lanes, snapshotted by the
        caller at submission: the parent is one mailbox row, every lane
        gets its own acknowledgement row through the broadcast fan-out,
        and the returned :class:`BroadcastCommand` is the parent barrier
        (``completed`` only when EVERY lane acknowledged; uncertain lanes
        individually listed). The dedup-first gate is the same one: only
        a NEW command row fences the epoch. The single-lane path
        (default) is unchanged.
        """
        if work_scoped:
            return await self._pause_work_scoped(work_id, actor, idempotency_key, recipients)
        command = ControlCommand(
            schema="forge.proposal.control-command/1",
            command_id=f"cmd-{uuid.uuid4().hex[:12]}",
            work_id=work_id,
            sequence=await self.surface.next_sequence(work_id),
            kind="pause",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=idempotency_key,
            status="received",
        )
        fenced, _stored, _created = await recorded_pause_async(
            self.pause_states.get(work_id, PauseState(work_id=work_id, publication_epoch=0)),
            command,
            self.surface.submit,
        )
        self.pause_states[work_id] = fenced
        return send_interrupt(fenced)

    async def _pause_work_scoped(
        self,
        work_id: str,
        actor: str,
        idempotency_key: str,
        recipients: Sequence[str],
    ) -> BroadcastCommand:
        """The work-wide pause: one parent row, per-lane acknowledgements.

        The same ``recorded_pause_async`` gate wrapped around the
        broadcast submit: a redelivered work-wide pause is refused by the
        parent row's idempotency key BEFORE the epoch moves, and the
        frozen recipient set of the WINNER is authoritative (a replay's
        lane list is discarded with its other bytes).
        """
        command = ControlCommand(
            schema="forge.proposal.control-command/1",
            command_id=f"cmd-{uuid.uuid4().hex[:12]}",
            work_id=work_id,
            sequence=await self.surface.next_sequence(work_id),
            kind="pause",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=idempotency_key,
            status="received",
        )
        captured: list[BroadcastCommand] = []

        async def _broadcast_submit(
            parent: ControlCommand,
        ) -> tuple[ControlCommand, bool]:
            broadcast, created = await self.surface.submit_broadcast(parent, tuple(recipients))
            captured.append(broadcast)
            return broadcast.parent, created

        fenced, _stored, created = await recorded_pause_async(
            self.pause_states.get(work_id, PauseState(work_id=work_id, publication_epoch=0)),
            command,
            _broadcast_submit,
        )
        if created:  # only a NEW parent row fences the epoch and interrupts
            self.pause_states[work_id] = send_interrupt(fenced)
        return captured[0]

    async def acknowledge(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand:
        """One lane's acknowledgement of a work-wide command (NXT-13)."""
        return await self.surface.acknowledge(command_id, recipient, note=note)

    async def mark_uncertain(self, command_id: str, recipient: str, note: str) -> BroadcastCommand:
        """Record a lane whose outcome could not be proven — visible, not guessed."""
        return await self.surface.mark_uncertain(command_id, recipient, note)

    async def resolve_uncertain(
        self, command_id: str, recipient: str, *, note: str = ""
    ) -> BroadcastCommand:
        """Settle an uncertain lane with the evidence that later arrived."""
        return await self.surface.resolve_uncertain(command_id, recipient, note=note)

    async def pending_for(self, work_id: str, recipient: str) -> list[ControlCommand]:
        """The per-recipient lane view (NXT-13): the work's ordinary pending
        commands PLUS this recipient's still-pending broadcast views, in
        durable sequence order. One lane's consumption removes nothing
        from another lane's view."""
        combined = await self.surface.pending(work_id) + await self.surface.pending_for(
            work_id, recipient
        )
        return sorted(combined, key=lambda command: command.sequence)

    async def broadcast(self, command_id: str) -> BroadcastCommand | None:
        """The work-wide barrier state of one broadcast command."""
        return await self.surface.broadcast(command_id)

    async def broadcasts(self, work_id: str) -> list[BroadcastCommand]:
        """The work's broadcasts in sequence order (the operator's view)."""
        return await self.surface.broadcasts(work_id)

    async def fence_active(self, work_id: str) -> bool:
        """True while a work-wide pause awaits a lane decision — a child
        created during the pause must not start through the fence."""
        return await self.surface.fence_active(work_id)

    async def resume(self, work_id: str, actor: str, idempotency_key: str) -> bool:
        """Resume only from a confirmed checkpoint (CTL-06).

        LIVE-found (wave C→D): the pause ran in the LANE process; this
        service runs in the APP process — its in-memory pause_states can
        never hold the lane's pause. The DURABLE truth is the checkpoint
        on the control plane: when the store has one for this work, the
        resume stands (the checkpoint IS the confirmed capture).
        """
        state = self.pause_states.get(work_id)
        in_memory = state is not None and state.checkpoint_captured
        if not in_memory and not self._server_has_checkpoint(work_id):
            return False  # no confirmed checkpoint — resume refuses
        self.pause_states.pop(work_id, None)
        await self.surface.submit(
            ControlCommand(
                schema="forge.proposal.control-command/1",
                command_id=f"cmd-{uuid.uuid4().hex[:12]}",
                work_id=work_id,
                sequence=await self.surface.next_sequence(work_id),
                kind="resume",
                actor_ref=actor,
                actor_origin="server_authenticated_human",
                idempotency_key=idempotency_key,
                status="received",
            )
        )
        return True

    def _server_has_checkpoint(self, work_id: str) -> bool:
        """The durable checkpoint store answers for the LANE's pause."""
        try:
            from forge.api_checkpoint_channel import CheckpointStore, _store_dir

            index = CheckpointStore(_store_dir())._load_index(work_id)
            return bool(index.get("checkpoints"))
        except Exception:  # noqa: BLE001 — no store configured, no resume
            return False

    async def steer(
        self,
        work_id: str,
        actor: str,
        text: str,
        *,
        run_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """CTL-07: bounded steering — acceptance-policy changes are rejected.

        Steer delivers guidance; it NEVER grants new authority. An
        instruction that weakens acceptance is routed to the revision
        gate with a rejection, not delivered to the agent. An ACCEPTED
        instruction also enters the mailbox as a ``steer`` ControlCommand
        (payload: the text, plus ``run_id`` when the operator scoped the
        note to one lane) — the record a running lane's
        :class:`~forge.adaptive.lane_control.LaneSteeringSession` drains
        and delivers; a rejected one never reaches the mailbox.

        ``idempotency_key`` (R28-09): the NATIVE event identity the
        caller derived (verb + work + the provider delivery id). With it,
        a redelivered native steer lands on the SAME mailbox row — one
        command, one logical effect — even when the first attempt died
        between the mailbox commit and the operator reply. Without it a
        fresh random key is generated per call, kept only for direct
        programmatic callers with no native identity to derive.
        """
        classification = classify_instruction(text)
        if classification == "acceptance_change":
            return {
                "status": "rejected",
                "reason": "acceptance policy change requires the revision gate",
            }
        payload: dict[str, Any] = {"text": text}
        if run_id:
            payload["run_id"] = run_id
        command = ControlCommand(
            schema="forge.proposal.control-command/1",
            command_id=f"cmd-{uuid.uuid4().hex[:12]}",
            work_id=work_id,
            sequence=await self.surface.next_sequence(work_id),
            kind="steer",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=idempotency_key or f"steer:{uuid.uuid4().hex[:12]}",
            status="received",
            payload=payload,
        )
        stored, created = await self.surface.submit(command)
        return {
            "status": "accepted",
            "classification": classification,
            "command_id": stored.command_id,
            "created": created,
        }

    async def answer(
        self,
        work_id: str,
        actor: str,
        question_id: str,
        text: str,
        *,
        run_id: str = "",
    ) -> bool:
        """The /answer surface: an authorized answer unblocks the wait.

        ``run_id`` optionally scopes the answer to one lane; a lane
        session draining another run ignores it.
        """
        payload: dict[str, Any] = {"question_id": question_id, "text": text}
        if run_id:
            payload["run_id"] = run_id
        command = ControlCommand(
            schema="forge.proposal.control-command/1",
            command_id=f"cmd-{uuid.uuid4().hex[:12]}",
            work_id=work_id,
            sequence=await self.surface.next_sequence(work_id),
            kind="answer",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=f"answer:{work_id}:{question_id}",
            status="received",
            payload=payload,
        )
        _, created = await self.surface.submit(command)
        return created

    async def pending(self, work_id: str) -> list[ControlCommand]:
        """The work's received/authorized commands in durable sequence order.

        The lane-drain view: the record a
        :class:`~forge.adaptive.lane_control.LaneSteeringSession`
        consumes (the awaited passthrough to the mailbox surface).
        """
        return await self.surface.pending(work_id)


def control_service_from_env(
    env: Mapping[str, str] | None = None,
    session_factory: Any | None = None,
) -> OperatorControlService:
    """The control service with its mailbox mounted from the environment.

    The flag-gated honest rollout (NXT-09/12's durable leg): with
    ``FORGE_CONTROL_MAILBOX=postgres`` AND a session factory available —
    passed in, or derived from ``DATABASE_URL`` exactly the way the app
    builds its own (``forge.database.get_session_factory``, the cached
    engine/pool the process already shares) — the service's mailbox is
    the durable :class:`~forge.adaptive.mailbox_db.PostgresMailbox`.
    Anything else (the default, a typo, no factory, no DATABASE_URL)
    stays on the in-memory reference mailbox, with a WARNING when the
    operator asked for postgres and did not get it — the flag never
    silently degrades in the other direction.
    """
    source = os.environ if env is None else env
    if str(source.get(FORGE_CONTROL_MAILBOX_ENV, "")).strip().lower() != (
        _CONTROL_MAILBOX_POSTGRES
    ):
        return OperatorControlService()
    factory = session_factory
    if factory is None:
        url = str(source.get("DATABASE_URL", "")).strip()
        if url:
            from forge.database import get_session_factory

            factory = get_session_factory(url)
    if factory is None:
        logger.warning(
            "FORGE_CONTROL_MAILBOX=postgres without a session factory or DATABASE_URL — "
            "the control mailbox stays IN MEMORY; the flag did not take effect"
        )
        return OperatorControlService()
    from forge.adaptive.mailbox_db import PostgresMailbox

    return OperatorControlService(mailbox=PostgresMailbox(factory))


# ---------------------------------------------------------------------------
# WorkPackageCoordination — a parent WorkPackage driving child runs
# ---------------------------------------------------------------------------


@dataclass
class WorkPackageCoordination:
    """Phase-by-phase child-run creation under a parent WorkPackage.

    The child-run factory is the provider seam: each writable item
    becomes whatever the existing RunService already starts (one writer
    per child lane — MRP-03). Phases come from
    :func:`~forge.adaptive.workpackage.compile_dependencies` (a cycle
    raises the phasing message); a failed child in a phase holds the
    later phases (bounded execution — no partial cascade).
    """

    child_run_factory: ChildRunFactory

    async def start(self, package: WorkPackage, *, task_brief: str) -> dict[str, Any]:
        """Validate, phase, and launch phase 1; return the coordination state."""
        violations = package.validate()
        if violations:
            raise ValueError(f"work package invalid: {'; '.join(violations)}")
        phase_ids = compile_dependencies(list(package.items))
        state: dict[str, Any] = {
            "schema": "forge.workpackage.coordination/1",
            "package_id": package.package_id,
            "phases": [list(ids) for ids in phase_ids],
            "current_phase": 0,
            "child_runs": {},
            "state": "running",
        }
        first = [item for item in package.items if item.item_id in phase_ids[0]]
        await self._launch_phase(state, package, first, task_brief)
        return state

    async def advance(
        self, state: dict[str, Any], package: WorkPackage, *, task_brief: str
    ) -> dict[str, Any]:
        """Phase completed cleanly — launch the next (or finish)."""
        if state["state"] != "running":
            return state
        nxt = state["current_phase"] + 1
        if nxt >= len(state["phases"]):
            state["state"] = "complete"
            return state
        ids = state["phases"][nxt]
        phase_items = [item for item in package.items if item.item_id in ids]
        state["current_phase"] = nxt
        await self._launch_phase(state, package, phase_items, task_brief)
        return state

    async def child_failed(
        self, state: dict[str, Any], item_id: str, reason: str
    ) -> dict[str, Any]:
        """A failed child holds the later phases — no partial cascade."""
        state["state"] = "failed"
        state["failed_item"] = item_id
        state["failure_reason"] = reason
        return state

    async def _launch_phase(
        self,
        state: dict[str, Any],
        package: WorkPackage,
        phase: list[Any],
        task_brief: str,
    ) -> None:
        for item in phase:
            run_id = await self.child_run_factory(item.item_id, item.repository_id)
            state["child_runs"][item.item_id] = {
                "repository_id": item.repository_id,
                "run_id": run_id,
                "phase": state["current_phase"],
                "brief": task_brief,
            }
