"""Durable flow controller (ADR-0004, ADR-0005).

A small, typed state machine over a SQLAlchemy ``AsyncSession``. It is
DB-agnostic: the same code runs over sqlite (tests) and Postgres (production).

Rules encoded here:

- Every lifecycle transition is validated against ``ALLOWED_TRANSITIONS`` and
  persisted by the caller's transaction; each transition writes exactly one
  ``outbox`` row *in the same transaction* so the state change and its
  announcement commit atomically (ADR-0005).
- ``blocked`` / ``failed`` / ``cancelled`` may be entered from any
  non-terminal state; terminal states (``ready_for_human``, ``blocked``,
  ``failed``, ``cancelled``) have no outgoing transitions.
- External writes are journaled intent-first: ``record_action`` creates a
  ``requested`` row before dispatch, ``complete_action`` records the outcome
  exactly once (ADR-0005).
- Worker leases are compared against their expiry (fencing): a different
  owner cannot renew or take a live lease (ADR-0005).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.models import ActionLog, FlowRun, Outbox, StepRun

#: Outbox event type emitted for every lifecycle transition.
TRANSITION_EVENT_TYPE = "flow.transition"


class FlowStatus(str, Enum):
    """Flow run lifecycle in ADR-0004 order."""

    ACCEPTED = "accepted"
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    PROPOSING = "proposing"
    VALIDATING = "validating"
    COMMITTING = "committing"
    #: ADR-0015: a ci_harness backend job is running in the target project's
    #: CI. Worker-free and durable, like waiting_ci — the reconciler polls it.
    WAITING_HARNESS = "waiting_harness"
    ENSURING_DRAFT_MR = "ensuring_draft_mr"
    WAITING_CI = "waiting_ci"
    EVALUATING_CI = "evaluating_ci"
    REVIEWING = "reviewing"
    READY_FOR_HUMAN = "ready_for_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: States that end a run. Terminal in the operator-facing sense — no
#: automatic leg leaves them — except the explicit revival edges below:
#: a failed/blocked run may be walked back to ``proposing`` by the bounded
#: auto-revive (transient failures only) or by an operator ``/retry``
#: (ADR-0004 amendment: one authorized edge back into the graph, same run,
#: same branch — never a re-plan).
TERMINAL_STATUSES: frozenset[FlowStatus] = frozenset(
    {
        FlowStatus.READY_FOR_HUMAN,
        FlowStatus.BLOCKED,
        FlowStatus.FAILED,
        FlowStatus.CANCELLED,
    }
)

#: Enterable from any non-terminal state (ADR-0004: "at any valid point").
_FROM_ANY_TERMINAL: frozenset[FlowStatus] = frozenset(
    {
        FlowStatus.BLOCKED,
        FlowStatus.FAILED,
        FlowStatus.CANCELLED,
    }
)

#: The ADR-0004 transition graph. Branches of ``evaluating_ci``:
#: repair loop re-enters ``proposing``, checks passed move on to ``reviewing``,
#: infrastructure failure parks the run as ``blocked`` (with a reason).
#: ADR-0015 adds the harness leg: a ci_harness backend parks the run in
#: ``waiting_harness`` (from ``proposing`` after the harness job is started,
#: or from ``committing`` on re-entry); a verified harness change moves it on
#: to ``committing``, where the candidate sha is recorded — forge never
#: commits on the harness's behalf.
ALLOWED_TRANSITIONS: dict[FlowStatus, set[FlowStatus]] = {
    FlowStatus.ACCEPTED: {FlowStatus.PREFLIGHT},
    FlowStatus.PREFLIGHT: {FlowStatus.PLANNING},
    FlowStatus.PLANNING: {FlowStatus.WAITING_APPROVAL},
    FlowStatus.WAITING_APPROVAL: {FlowStatus.PROPOSING},
    FlowStatus.PROPOSING: {FlowStatus.VALIDATING, FlowStatus.WAITING_HARNESS},
    FlowStatus.VALIDATING: {FlowStatus.COMMITTING},
    FlowStatus.COMMITTING: {FlowStatus.ENSURING_DRAFT_MR, FlowStatus.WAITING_HARNESS},
    FlowStatus.WAITING_HARNESS: {FlowStatus.COMMITTING},
    FlowStatus.ENSURING_DRAFT_MR: {FlowStatus.WAITING_CI},
    FlowStatus.WAITING_CI: {FlowStatus.EVALUATING_CI},
    FlowStatus.EVALUATING_CI: {FlowStatus.PROPOSING, FlowStatus.REVIEWING},
    FlowStatus.REVIEWING: {FlowStatus.READY_FOR_HUMAN},
    FlowStatus.READY_FOR_HUMAN: set(),
    # Revival edges (see TERMINAL_STATUSES): the ONLY way out of a terminal
    # state, and only back into ``proposing`` — the run keeps its identity,
    # branch and evidence; planning is never re-run.
    FlowStatus.BLOCKED: {FlowStatus.PROPOSING},
    FlowStatus.FAILED: {FlowStatus.PROPOSING},
    FlowStatus.CANCELLED: set(),
}

for _status in ALLOWED_TRANSITIONS:
    if _status not in TERMINAL_STATUSES:
        ALLOWED_TRANSITIONS[_status] = ALLOWED_TRANSITIONS[_status] | _FROM_ANY_TERMINAL


class ControllerError(Exception):
    """Base class for controller errors."""


class RunNotFound(ControllerError):
    """The referenced flow run does not exist."""


class InvalidTransition(ControllerError):
    """The requested lifecycle transition is not allowed by ADR-0004."""


class ActionLogNotFound(ControllerError):
    """The referenced action log row does not exist."""


class InvalidActionTransition(ControllerError):
    """An action log row may not move to the requested status."""


class StepRunNotFound(ControllerError):
    """The referenced step run does not exist."""


def as_aware_utc(value: datetime) -> datetime:
    """Normalize *value* to timezone-aware UTC.

    SQLite returns naive datetimes while Postgres (asyncpg) returns aware
    ones; comparisons must work with both.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_status(value: FlowStatus | str) -> FlowStatus:
    try:
        return FlowStatus(value)
    except ValueError:
        raise InvalidTransition(f"unknown flow status: {value!r}") from None


ActionOutcome = Literal["succeeded", "failed", "unknown_outcome"]

#: The only statuses a ``requested`` action may end in; nothing is legal after.
_LEGAL_ACTION_OUTCOMES: frozenset[str] = frozenset({"succeeded", "failed", "unknown_outcome"})


class Controller:
    """Owns flow run lifecycle mutations on top of one ``AsyncSession``.

    All methods flush into the caller's transaction; the caller decides when
    to commit. Nothing here talks to GitLab or an LLM.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Lifecycle transitions (ADR-0004)
    # ------------------------------------------------------------------

    async def transition(
        self,
        run_id: str,
        to_status: FlowStatus | str,
        reason: str | None = None,
    ) -> FlowRun:
        """Move the run to *to_status* and write one outbox row, atomically.

        Raises :class:`RunNotFound` if the run does not exist and
        :class:`InvalidTransition` if ADR-0004 does not allow the move.
        """
        target = _coerce_status(to_status)
        run = await self.session.get(FlowRun, run_id)
        if run is None:
            raise RunNotFound(f"flow run {run_id!r} not found")
        current = FlowStatus(run.status)
        allowed = ALLOWED_TRANSITIONS[current]
        if target not in allowed:
            raise InvalidTransition(
                f"transition {current.value!r} -> {target.value!r} is not allowed; "
                f"allowed targets: {sorted(s.value for s in allowed)}"
            )

        run.status = target.value
        run.status_reason = reason
        run.updated_at = datetime.now(timezone.utc)
        self.session.add(
            Outbox(
                flow_run_id=run_id,
                event_type=TRANSITION_EVENT_TYPE,
                payload={
                    "flow_run_id": run_id,
                    "from": current.value,
                    "to": target.value,
                    "reason": reason,
                },
            )
        )
        await self.session.flush()
        return run

    # ------------------------------------------------------------------
    # External action journal (ADR-0005: intent, then outcome)
    # ------------------------------------------------------------------

    async def record_action(
        self,
        flow_run_id: str | None,
        action_kind: str,
        params_digest: str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        """Journal the *intent* to perform an external write.

        Creates an ``action_log`` row with ``status='requested'`` and returns
        its id. Call :meth:`complete_action` with the outcome afterwards.
        """
        if flow_run_id is not None and await self.session.get(FlowRun, flow_run_id) is None:
            raise RunNotFound(f"flow run {flow_run_id!r} not found")
        action = ActionLog(
            flow_run_id=flow_run_id,
            action_kind=action_kind,
            params_digest=params_digest,
            correlation_id=correlation_id,
            status="requested",
        )
        self.session.add(action)
        await self.session.flush()
        return action.id

    async def complete_action(
        self,
        action_log_id: int,
        status: ActionOutcome,
        remote_result: dict | None = None,
    ) -> ActionLog:
        """Journal the *outcome* of a previously requested external write.

        Only ``requested -> {succeeded, failed, unknown_outcome}`` is legal;
        terminal rows are immutable. ``unknown_outcome`` means the HTTP call
        timed out after possibly executing — reconcile before retrying
        (ADR-0005).
        """
        action = await self.session.get(ActionLog, action_log_id)
        if action is None:
            raise ActionLogNotFound(f"action log {action_log_id} not found")
        if action.status != "requested":
            raise InvalidActionTransition(
                f"action log {action_log_id} is terminal (status={action.status!r}); "
                "no further change is allowed"
            )
        if status not in _LEGAL_ACTION_OUTCOMES:
            raise InvalidActionTransition(
                f"action outcome {status!r} is not one of {sorted(_LEGAL_ACTION_OUTCOMES)}"
            )
        action.status = status
        if remote_result is not None:
            action.remote_result = remote_result
        await self.session.flush()
        return action

    # ------------------------------------------------------------------
    # Worker leases (ADR-0005: atomic, renewed by heartbeat, fenced)
    # ------------------------------------------------------------------

    async def acquire_lease(self, step_run_id: int, owner: str, seconds: int) -> bool:
        """Take the lease on a step run for *owner*.

        Fails (returns ``False``) when a different owner still holds a live
        lease. A lease with no expiry is treated as expired.
        """
        step = await self._get_step(step_run_id)
        if self._lease_is_live(step):
            return False
        self._grant_lease(step, owner, seconds)
        await self.session.flush()
        return True

    async def renew_lease(self, step_run_id: int, owner: str, seconds: int) -> bool:
        """Heartbeat-renew the lease of *owner* on a step run.

        Fails (returns ``False``) when a different owner holds a live lease
        (fencing). When the previous lease has expired, *owner* takes over.
        """
        step = await self._get_step(step_run_id)
        if step.lease_owner != owner and self._lease_is_live(step):
            return False
        self._grant_lease(step, owner, seconds)
        await self.session.flush()
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_step(self, step_run_id: int) -> StepRun:
        step = await self.session.get(StepRun, step_run_id)
        if step is None:
            raise StepRunNotFound(f"step run {step_run_id} not found")
        return step

    @staticmethod
    def _lease_is_live(step: StepRun) -> bool:
        if step.lease_owner is None or step.lease_expires_at is None:
            return False
        return as_aware_utc(step.lease_expires_at) > datetime.now(timezone.utc)

    @staticmethod
    def _grant_lease(step: StepRun, owner: str, seconds: int) -> None:
        step.lease_owner = owner
        step.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
