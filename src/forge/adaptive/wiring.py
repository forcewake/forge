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

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forge.adaptive.control import (
    Mailbox,
    PauseState,
    classify_instruction,
    new_publication_epoch,
    request_pause,
    send_interrupt,
)
from forge.adaptive.discovery import DiscoveryRun
from forge.adaptive.models import ControlCommand
from forge.adaptive.workpackage import WorkPackage, compile_dependencies

ChildRunFactory = Callable[[str, str], Awaitable[str]]
"""async (item_id, repository_id) -> child run id — the provider seam."""


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
    the :class:`~forge.adaptive.control.Mailbox` (a redelivered command
    never spends another iteration). Accepted steers are mailbox
    records too (see :meth:`steer`) — the surface a running lane's
    steering bridge consumes.
    """

    mailbox: Mailbox = field(default_factory=Mailbox)
    pause_states: dict[str, PauseState] = field(default_factory=dict)

    def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        """Gateway entry: dedups by idempotency key, enforces sequences."""
        return self.mailbox.submit(command)

    def pause(self, work_id: str, actor: str, idempotency_key: str) -> PauseState:
        """CTL-05: pause_requested FIRST, then the fence, then interrupt."""
        state = self.pause_states.get(work_id, PauseState(work_id=work_id, publication_epoch=0))
        requested = request_pause(state)
        fenced = new_publication_epoch(requested)
        self.pause_states[work_id] = fenced
        self.mailbox.submit(
            ControlCommand(
                schema="forge.proposal.control-command/1",
                command_id=f"cmd-{uuid.uuid4().hex[:12]}",
                work_id=work_id,
                sequence=len(self.mailbox.commands) + 1,
                kind="pause",
                actor_ref=actor,
                actor_origin="server_authenticated_human",
                idempotency_key=idempotency_key,
                status="received",
            )
        )
        return send_interrupt(fenced)

    def resume(self, work_id: str, actor: str, idempotency_key: str) -> bool:
        """Resume only from a confirmed checkpoint (CTL-06)."""
        state = self.pause_states.get(work_id)
        if state is None or not state.checkpoint_captured:
            return False  # no confirmed checkpoint — resume refuses
        self.pause_states.pop(work_id, None)
        self.mailbox.submit(
            ControlCommand(
                schema="forge.proposal.control-command/1",
                command_id=f"cmd-{uuid.uuid4().hex[:12]}",
                work_id=work_id,
                sequence=len(self.mailbox.commands) + 1,
                kind="resume",
                actor_ref=actor,
                actor_origin="server_authenticated_human",
                idempotency_key=idempotency_key,
                status="received",
            )
        )
        return True

    def steer(self, work_id: str, actor: str, text: str, *, run_id: str = "") -> dict[str, Any]:
        """CTL-07: bounded steering — acceptance-policy changes are rejected.

        Steer delivers guidance; it NEVER grants new authority. An
        instruction that weakens acceptance is routed to the revision
        gate with a rejection, not delivered to the agent. An ACCEPTED
        instruction also enters the mailbox as a ``steer`` ControlCommand
        (payload: the text, plus ``run_id`` when the operator scoped the
        note to one lane) — the record a running lane's
        :class:`~forge.adaptive.lane_control.LaneSteeringSession` drains
        and delivers; a rejected one never reaches the mailbox.
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
            sequence=len(self.mailbox.commands) + 1,
            kind="steer",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=f"steer:{uuid.uuid4().hex[:12]}",
            status="received",
            payload=payload,
        )
        stored, created = self.mailbox.submit(command)
        return {
            "status": "accepted",
            "classification": classification,
            "command_id": stored.command_id,
            "created": created,
        }

    def answer(
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
            sequence=len(self.mailbox.commands) + 1,
            kind="answer",
            actor_ref=actor,
            actor_origin="server_authenticated_human",
            idempotency_key=f"answer:{work_id}:{question_id}",
            status="received",
            payload=payload,
        )
        _, created = self.mailbox.submit(command)
        return created

    def pending(self, work_id: str) -> list[ControlCommand]:
        """The work's received/authorized commands in durable sequence order.

        The lane-drain view: the record a
        :class:`~forge.adaptive.lane_control.LaneSteeringSession`
        consumes (passthrough to
        :meth:`~forge.adaptive.control.Mailbox.pending`).
        """
        return self.mailbox.pending(work_id)


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
