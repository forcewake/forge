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
  ``failed``, ``cancelled``) have no outgoing transitions. The ONE
  exception is the explicit revival edge (:meth:`Controller.revive_transition`):
  a run parked ``blocked``/``failed`` may be walked back to ``proposing`` by
  the operator (``/retry``) or by the Tier-1 auto-revive, never by the plain
  graph.
- External writes are journaled intent-first: ``record_action`` creates a
  ``requested`` row before dispatch, ``complete_action`` records the outcome
  exactly once (ADR-0005).
- Worker leases are compared against their expiry (fencing): a different
  owner cannot renew or take a live lease (ADR-0005).
- ``transition_guarded`` is the versioned form of ``transition`` (R10): the
  whole move happens in ONE conditional ``UPDATE ... WHERE status =
  :expected [AND cancellation_generation = :gen]`` whose rowcount is the
  arbitration — two concurrent actors with the same expectation produce
  exactly one applied transition and one typed :class:`StaleClaimError`.
  ``request_cancel`` bumps the run's cancellation generation in one
  statement with ``cancel_requested``, giving every guard a single value to
  compare (queue ownership implies effect ownership).
- A04: the guarded CAS is THE write path. The plain :meth:`Controller.transition`
  delegates to it (the pre-read only derives the expectation and names the
  illegal-edge error; the WHERE still re-checks at write time), and the
  revival edges (:meth:`Controller.revive_transition`,
  :meth:`Controller.restart_plan_transition`) pin the terminal status they
  read at start the same way — a stale owner's lifecycle write matches 0
  rows and raises :class:`StaleClaimError` instead of overwriting.
"""

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
    FlowStatus.BLOCKED: set(),
    FlowStatus.FAILED: set(),
    FlowStatus.CANCELLED: set(),
}

for _status in ALLOWED_TRANSITIONS:
    if _status not in TERMINAL_STATUSES:
        ALLOWED_TRANSITIONS[_status] = ALLOWED_TRANSITIONS[_status] | _FROM_ANY_TERMINAL


#: The terminal-exit edges: the revival edge (Tier-1 auto-revive / Tier-2
#: operator ``/retry`` → ``proposing``) and the A13 plan-restart edge
#: (a PRE-GATE ``blocked(config_…)`` run → ``preflight``, fenced to runs
#: that never froze a spec). Deliberately NOT part of ``ALLOWED_TRANSITIONS``
#: — ``transition`` must never leave a terminal state on its own;
#: :meth:`Controller.revive_transition` and
#: :meth:`Controller.restart_plan_transition` are the explicit, journaled
#: exceptions, and ``cancelled``/``ready_for_human`` are excluded from them
#: on purpose (a revoked publication grant stays revoked).
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
    """A guarded mutation lost the arbitration (R10).

    Raised by :meth:`Controller.transition_guarded` when the row no longer
    matches the caller's expectation (status moved, or the cancellation
    generation was bumped by a cancel): exactly one concurrent actor applies
    its transition — this actor is the loser and must record superseded
    evidence, never retry blindly. Carries the observed state for the
    superseded-evidence record.
    """

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


def _coerce_expected(
    expected: FlowStatus | str | Collection[FlowStatus | str] | None,
) -> list[FlowStatus] | None:
    """Normalize a guarded transition's expectation to a status list.

    ``None`` means "derive from the stored row" (the WHERE still re-checks at
    write time); a single status coerces to a one-element list; an empty
    collection is a caller bug, not a match-nothing predicate.
    """
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

        A04: the write is the guarded CAS — this entry derives the
        expectation from a pre-read (kept so an illegal edge still fails with
        the typed :class:`InvalidTransition` BEFORE any statement) and routes
        through the SAME conditional
        ``UPDATE ... WHERE status = :expected`` as
        :meth:`transition_guarded`. The WHERE re-checks at write time, so a
        concurrent actor that moved the row between the read and the write
        makes this caller the stale loser: 0 rows match and
        :class:`StaleClaimError` is raised instead of a silent overwrite
        (P07). Raises :class:`RunNotFound` if the run does not exist and
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
        """Versioned conditional transition (R10): the CAS IS the state machine.

        The move happens in ONE statement::

            UPDATE flow_runs SET status = :to, ...
            WHERE id = :run_id
              AND status IN (:expected...)          -- the version predicate
              [AND cancellation_generation = :gen]  -- the cancel fence

        so two concurrent actors holding the same expectation arbitrate at the
        row: exactly one UPDATE matches, the loser matches 0 rows. The outbox
        row is written in the same transaction as the applied UPDATE — the
        announcement can never exist without the state change, and a stale
        actor writes no announcement at all.

        *expected_status* is the caller's belief about the current status
        (one status, or several for the enter-from-anywhere edges);
        ``None`` derives it from a read — still safe, because the WHERE
        re-checks at write time. *expected_cancellation_generation* pins the
        publication-grant generation: if a cancel bumped it since the caller
        minted its :class:`~forge.durable.claims.ExecutionClaim`, the UPDATE
        matches 0 rows.

        Returns the refreshed run. Raises :class:`RunNotFound` when the run
        does not exist, :class:`InvalidTransition` when the expected→target
        edge is not in ADR-0004's graph, and :class:`StaleClaimError` when
        the predicate matched 0 rows — the loser's signal to record
        superseded evidence, never to retry blindly. Call on a session with
        no pending changes to the run row: the Core UPDATE bypasses the ORM
        unit of work on purpose (the rowcount is the whole decision).
        """
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
        """The ONE conditional UPDATE behind every lifecycle mutation (R10/A04).

        ``UPDATE flow_runs SET status = :to ... WHERE id = :run_id AND
        status IN (:expected) [AND cancellation_generation = :gen]`` — the
        rowcount is the arbitration: 0 rows means a concurrent actor moved
        the row and this caller is the stale loser (typed
        :class:`StaleClaimError` carrying the observed state; record
        superseded evidence, never retry blindly). The Core UPDATE bypasses
        the ORM unit of work on purpose; the instance is re-read and
        refreshed afterwards so callers see the applied values on their own
        session object.
        """
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
        """Revoke the publication grant and bump the cancel generation (R10).

        ONE statement sets ``cancel_requested`` and increments
        ``cancellation_generation`` — the flag and the fence value can never
        disagree, and every :class:`~forge.durable.claims.ExecutionClaim`
        minted before this call is fenced out of publishing and guarded
        transitions the moment it lands. Returns the NEW generation.
        Raises :class:`RunNotFound` when the run does not exist. The terminal
        ``cancelled`` transition stays the caller's; so does step
        withdrawal (the service's cancel machinery owns both today).
        """
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
        """Walk a ``blocked``/``failed`` run back to ``proposing`` — the revival edge.

        The explicit, operator-authorized (``/retry``) or machine-journaled
        (``auto_revive``) exception to "terminal states have no outgoing
        transitions": same run id, same branch, same candidate history. The
        outbox row carries ``authorized_by`` so the walk is auditable.
        Raises :class:`InvalidTransition` for any other source status.
        A04: the walk is the guarded CAS — the terminal status read at revive
        start is the expectation, so two racing revivals (or a revival losing
        to any other concurrent move) produce exactly one applied walk and
        one typed :class:`StaleClaimError`; a stale operator's revive can
        never reopen a run that moved on.
        """
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
        """Walk a PRE-GATE ``blocked`` run back to ``preflight`` (A13).

        The plan-restart edge — beside :meth:`revive_transition`, one of
        only two walks out of a terminal status, and this one is fenced:
        the run must never have frozen a RunSpec (``spec_digest`` empty —
        the gate was never opened, nothing was approved). It exists for the
        A13 config gate: a run parked ``blocked(config_…)`` before its
        first paid call re-enters planning once the config read recovers,
        exactly as a crashed ``/implement`` would. A post-freeze run can
        only revive forward to ``proposing`` (:meth:`revive_transition`) —
        an approved input is never re-planned past its gate. Journaled like
        the revival edge; raises :class:`InvalidTransition` otherwise.
        A04: same CAS discipline as the revival edge — the ``blocked``
        status read at start is the write-time expectation.
        """
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
