"""Advance durable run state under cancellation and worker-claim fences."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
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
    #: ADR-0015: a ci_harness backend job is running in the target project's CI. Worker-free
    #: and durable, like waiting_ci — the reconciler polls it.
    WAITING_HARNESS = "waiting_harness"
    ENSURING_DRAFT_MR = "ensuring_draft_mr"
    WAITING_CI = "waiting_ci"
    EVALUATING_CI = "evaluating_ci"
    REVIEWING = "reviewing"
    READY_FOR_HUMAN = "ready_for_human"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: States that end a run; no outgoing transitions.
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

#: The ADR-0004 transition graph.
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
    FlowStatus.BLOCKED: set(),
    FlowStatus.FAILED: set(),
    FlowStatus.CANCELLED: set(),
}

for _status in ALLOWED_TRANSITIONS:
    if _status not in TERMINAL_STATUSES:
        ALLOWED_TRANSITIONS[_status] = ALLOWED_TRANSITIONS[_status] | _FROM_ANY_TERMINAL


#: The terminal-exit edges: the revival edge (Tier-1 auto-revive / Tier-2 operator
#: ``/retry`` → ``proposing``) and the plan-restart edge (a PRE-GATE ``blocked(config_…)``
#: run → ``preflight``, fenced to runs that never froze a spec).
_REVIVAL_SOURCES: frozenset[FlowStatus] = frozenset({FlowStatus.BLOCKED, FlowStatus.FAILED})


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


class StaleClaimError(ControllerError):
    """A guarded mutation lost the arbitration."""

    def __init__(
        self,
        message: str,
        *,
        expected_status: str | None = None,
        observed_status: str | None = None,
        expected_generation: int | None = None,
        observed_generation: int | None = None,
    ) -> None:
        super().__init__(message)
        self.expected_status = expected_status
        self.observed_status = observed_status
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation


def as_aware_utc(value: datetime) -> datetime:
    """Normalize *value* to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_status(value: FlowStatus | str) -> FlowStatus:
    try:
        return FlowStatus(value)
    except ValueError:
        raise InvalidTransition(f"unknown flow status: {value!r}") from None


def _coerce_expected(
    expected: FlowStatus | str | Collection[FlowStatus | str] | None,
) -> list[FlowStatus] | None:
    """Normalize a guarded transition's expectation to a status list."""
    if expected is None:
        return None
    if isinstance(expected, (FlowStatus, str)):
        return [_coerce_status(expected)]
    values = [_coerce_status(value) for value in expected]
    if not values:
        raise InvalidTransition("guarded transition expects at least one source status")
    return values


ActionOutcome = Literal["succeeded", "failed", "unknown_outcome"]

#: The only statuses a ``requested`` action may end in; nothing is legal after.
_LEGAL_ACTION_OUTCOMES: frozenset[str] = frozenset({"succeeded", "failed", "unknown_outcome"})


class Controller:
    """Owns flow run lifecycle mutations on top of one ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Lifecycle transitions (ADR-0004)
    #

    async def transition(
        self,
        run_id: str,
        to_status: FlowStatus | str,
        reason: str | None = None,
    ) -> FlowRun:
        """Move the run to *to_status* and write one outbox row, atomically."""
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

        run = await self._guarded_apply(
            run_id,
            target,
            expected=[current],
            expected_cancellation_generation=None,
            reason=reason,
        )
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

    async def transition_guarded(
        self,
        run_id: str,
        to_status: FlowStatus | str,
        *,
        expected_status: FlowStatus | str | Collection[FlowStatus | str] | None = None,
        expected_cancellation_generation: int | None = None,
        reason: str | None = None,
    ) -> FlowRun:
        """Versioned conditional transition: the CAS IS the state machine."""
        target = _coerce_status(to_status)
        expected = _coerce_expected(expected_status)
        if expected is None:
            current = await self.session.get(FlowRun, run_id)
            if current is None:
                raise RunNotFound(f"flow run {run_id!r} not found")
            await self.session.refresh(current)
            expected = [FlowStatus(current.status)]
        for source in expected:
            allowed = ALLOWED_TRANSITIONS[source]
            if target not in allowed:
                raise InvalidTransition(
                    f"transition {source.value!r} -> {target.value!r} is not allowed; "
                    f"allowed targets: {sorted(s.value for s in allowed)}"
                )

        run = await self._guarded_apply(
            run_id,
            target,
            expected=expected,
            expected_cancellation_generation=expected_cancellation_generation,
            reason=reason,
        )
        from_label = (
            expected[0].value if len(expected) == 1 else "/".join(sorted(s.value for s in expected))
        )
        self.session.add(
            Outbox(
                flow_run_id=run_id,
                event_type=TRANSITION_EVENT_TYPE,
                payload={
                    "flow_run_id": run_id,
                    "from": from_label,
                    "to": target.value,
                    "reason": reason,
                    "guarded": True,
                },
            )
        )
        await self.session.flush()
        return run

    async def _guarded_apply(
        self,
        run_id: str,
        target: FlowStatus,
        *,
        expected: list[FlowStatus],
        expected_cancellation_generation: int | None,
        reason: str | None,
    ) -> FlowRun:
        """The ONE conditional UPDATE behind every lifecycle mutation."""
        predicate = (
            FlowRun.status == expected[0].value
            if len(expected) == 1
            else FlowRun.status.in_([s.value for s in expected])
        )
        stmt = update(FlowRun).where(FlowRun.id == run_id, predicate)
        if expected_cancellation_generation is not None:
            stmt = stmt.where(FlowRun.cancellation_generation == expected_cancellation_generation)
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                stmt.values(
                    status=target.value,
                    status_reason=reason,
                    updated_at=datetime.now(timezone.utc),
                ).execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            run = await self.session.get(FlowRun, run_id)
            if run is None:
                raise RunNotFound(f"flow run {run_id!r} not found")
            await self.session.refresh(run)
            observed = FlowStatus(run.status)
            raise StaleClaimError(
                f"guarded transition {run_id[:8]} -> {target.value!r} lost the race: "
                f"run is {observed.value!r} at cancellation generation "
                f"{run.cancellation_generation}, expected "
                f"{[s.value for s in expected]} at generation "
                f"{expected_cancellation_generation}",
                expected_status="/".join(sorted(s.value for s in expected)),
                observed_status=observed.value,
                expected_generation=expected_cancellation_generation,
                observed_generation=int(run.cancellation_generation),
            )

        run = await self.session.get(FlowRun, run_id)
        if run is None:  # pragma: no cover — the UPDATE just matched this row
            raise RunNotFound(f"flow run {run_id!r} not found")
        await self.session.refresh(run)
        return run

    async def request_cancel(self, run_id: str) -> int:
        """Revoke the publication grant and bump the cancel generation."""
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(FlowRun)
                .where(FlowRun.id == run_id)
                .values(
                    cancel_requested=True,
                    cancellation_generation=FlowRun.cancellation_generation + 1,
                    updated_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            raise RunNotFound(f"flow run {run_id!r} not found")
        run = await self.session.get(FlowRun, run_id)
        if run is None:  # pragma: no cover — the UPDATE just matched this row
            raise RunNotFound(f"flow run {run_id!r} not found")
        await self.session.refresh(run)
        await self.session.flush()
        return int(run.cancellation_generation)

    async def revive_transition(
        self,
        run_id: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> FlowRun:
        """Walk a ``blocked``/``failed`` run back to ``proposing`` — the revival edge."""
        run = await self.session.get(FlowRun, run_id)
        if run is None:
            raise RunNotFound(f"flow run {run_id!r} not found")
        current = FlowStatus(run.status)
        if current not in _REVIVAL_SOURCES:
            raise InvalidTransition(
                f"revival edge requires 'blocked' or 'failed', not {current.value!r}"
            )

        run = await self._guarded_apply(
            run_id,
            FlowStatus.PROPOSING,
            expected=[current],
            expected_cancellation_generation=None,
            reason=(reason or "")[:200],
        )
        self.session.add(
            Outbox(
                flow_run_id=run_id,
                event_type=TRANSITION_EVENT_TYPE,
                payload={
                    "flow_run_id": run_id,
                    "from": current.value,
                    "to": FlowStatus.PROPOSING.value,
                    "reason": reason,
                    "revival": authorized_by,
                },
            )
        )
        await self.session.flush()
        return run

    async def restart_plan_transition(
        self,
        run_id: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> FlowRun:
        """Walk a PRE-GATE ``blocked`` run back to ``preflight``."""
        run = await self.session.get(FlowRun, run_id)
        if run is None:
            raise RunNotFound(f"flow run {run_id!r} not found")
        current = FlowStatus(run.status)
        if current is not FlowStatus.BLOCKED:
            raise InvalidTransition(f"plan-restart edge requires 'blocked', not {current.value!r}")
        if run.spec_digest:
            raise InvalidTransition(
                f"run {run_id!r} has a frozen spec — plan restart refused (revive instead)"
            )

        run = await self._guarded_apply(
            run_id,
            FlowStatus.PREFLIGHT,
            expected=[current],
            expected_cancellation_generation=None,
            reason=(reason or "")[:200],
        )
        self.session.add(
            Outbox(
                flow_run_id=run_id,
                event_type=TRANSITION_EVENT_TYPE,
                payload={
                    "flow_run_id": run_id,
                    "from": current.value,
                    "to": FlowStatus.PREFLIGHT.value,
                    "reason": reason,
                    "revival": authorized_by,
                },
            )
        )
        await self.session.flush()
        return run

    # External action journal (ADR-0005: intent, then outcome)
    #

    async def record_action(
        self,
        flow_run_id: str | None,
        action_kind: str,
        params_digest: str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        """Journal the *intent* to perform an external write."""
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
        """Journal the *outcome* of a previously requested external write."""
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

    # Worker leases (ADR-0005: atomic, renewed by heartbeat, fenced)
    #

    async def acquire_lease(self, step_run_id: int, owner: str, seconds: int) -> bool:
        """Take the lease on a step run for *owner*."""
        step = await self._get_step(step_run_id)
        if self._lease_is_live(step):
            return False
        self._grant_lease(step, owner, seconds)
        await self.session.flush()
        return True

    async def renew_lease(self, step_run_id: int, owner: str, seconds: int) -> bool:
        """Heartbeat-renew the lease of *owner* on a step run."""
        step = await self._get_step(step_run_id)
        if step.lease_owner != owner and self._lease_is_live(step):
            return False
        self._grant_lease(step, owner, seconds)
        await self.session.flush()
        return True

    # Internals

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
