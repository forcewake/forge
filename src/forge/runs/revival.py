"""Terminal-failure classification and revival (Tier 1 auto / Tier 2 operator).

A run that dies terminal must not cost operator attention unless a human is
genuinely needed. Every ``failed`` terminalization is classified first:

- **transient** (dispatch 5xx / network / timeout, rate limits, runner
  startup, an empty harness-start error) — the run parks ``blocked`` with a
  revival stamp in its evidence (``revive_at``-style ``due_at`` +
  ``revive_count``-style ``count``) and the provider reconciler re-dispatches
  the SAME branch after bounded backoff, at most
  ``FORGE_RUN_AUTO_REVIVE_LIMIT`` times. Journaled as ``auto_revive`` actions
  (ADR-0005). No issue comment, no operator.
- **fatal** (driver exit failed — a real quality signal, config errors such
  as 4xx input mismatches or a missing workflow, exhausted cycles) — the run
  parks ``blocked`` with the precise reason. No auto-retry.

Tier 2 is the operator override for everything Tier 1 correctly refuses to
touch: ``/retry [run-id]`` walks ``failed``/``blocked`` back to ``proposing``
through the explicit revival graph edge
(:meth:`forge.durable.controller.Controller.revive_transition`), grants one
extra commit cycle and re-dispatches on the same branch.

A11 makes both tiers a durable, single, idempotent transition: every revival
writes an ATTEMPT record (the ``action_log`` revival row, extended with an
idempotency key, a :class:`Retryability` class and a ``pending → dispatched``
dispatch state) in the SAME transaction as the CAS walk. The key is the
triggering command's delivery id (``/retry`` — a redelivered webhook is a
no-op; a different delivery while an attempt is in flight is refused) or the
recovery-window identity (auto-revive — two reconcilers in one window
collapse to one attempt, one dispatch). A crash between the revive commit
and the dispatch leaves the attempt findable, and
:func:`evaluate_attempt_recovery` — a reconciler pass in the R11 scanner
shape — re-drives the dispatch exactly once, guarded by the dispatch legs'
own journaled actions (an interrupted leg's unknown remote effect is parked
for an operator, never re-dispatched blind).

Classification lives here (one table) so the GitLab, GitHub and Azure DevOps
services cannot drift apart — the same reason text classifies the same way on
every lane, and the limits come from the same settings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, cast

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.controller import TERMINAL_STATUSES, Controller, FlowStatus, as_aware_utc
from forge.durable.intents import OPEN_STATES
from forge.durable.models import ActionLog, FlowRun, PublicationIntent, RunBudget

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the module import-light
    from forge.config import Settings
    from forge.orchestrator.project_config import ConfigReadResult

logger = logging.getLogger(__name__)

#: Failure classes. ``fatal`` is the default — Tier 1 must be conservative,
#: a mis-classified fatal costs two bounded retries, a mis-classified
#: transient hides a real quality signal from the operator.
FailureClass = Literal["transient", "fatal"]
TRANSIENT: FailureClass = "transient"
FATAL: FailureClass = "fatal"


class Retryability(StrEnum):
    """WHY a run is retryable — the typed revival-attempt class (A11).

    Deliberately ORTHOGONAL to the A12 effect-certainty states: the cause
    class says nothing about whether a remote effect is unresolved. An
    UNKNOWN-outcome publication blocks any auto-revive until the intent
    scanner reconciles it, whatever this enum says.
    """

    #: Tier-1: a transient infrastructure death (dispatch 5xx / network /
    #: rate limit / runner startup) the reconciler re-drives on its own.
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    #: Tier-2: an operator explicitly granted the revival (``/retry``).
    OPERATOR_OVERRIDE = "operator_override"
    #: Tier-1: the transient cause was a verification/harness timeout.
    VERIFICATION_TIMEOUT = "verification_timeout"


#: The revival action kinds — these ``action_log`` rows ARE the durable
#: revival-attempt record (written in the SAME transaction as the CAS
#: revival transition, A11).
REVIVAL_ACTION_KINDS: frozenset[str] = frozenset({"retry_requested", "auto_revive"})
RevivalKind = Literal["retry_requested", "auto_revive"]

#: A verification-shaped transient cause (``verification_timeout``): the
#: reason mentions verification AND a timeout — a harness/verification leg
#: that ran out of time is infrastructure pacing, not a quality signal.
_VERIFICATION_TIMEOUT_RE = re.compile(
    r"verification[_ ]?(timed?|timeout)|timed? ?out[^\n]*verification", re.IGNORECASE
)


def classify_retryability(kind: RevivalKind, reason: str = "") -> Retryability:
    """The retryability class for a revival attempt of *kind*.

    An operator ``/retry`` is retryable because an operator said so
    (``operator_override``) — even when the cause was transient. The
    Tier-1 auto-revive records the cause class instead: a verification
    timeout when the terminal reason is one, ``transient_infrastructure``
    for every other transient cause (the only cause Tier-1 ever revives).
    """
    if kind == "retry_requested":
        return Retryability.OPERATOR_OVERRIDE
    if _VERIFICATION_TIMEOUT_RE.search(reason or ""):
        return Retryability.VERIFICATION_TIMEOUT
    return Retryability.TRANSIENT_INFRASTRUCTURE


class RevivalError(Exception):
    """Base class for revival-attempt refusals (A11)."""


class RevivalAlreadyDelivered(RevivalError):
    """The SAME delivery id was already delivered for this run (webhook
    redelivery) — the command is a no-op: no second cycle bump, no second
    dispatch."""


class RevivalInFlight(RevivalError):
    """A DIFFERENT trigger's revival attempt is still open (dispatch
    pending) for this run — a new revival would double-dispatch."""


@dataclass
class RevivalAttempt:
    """The durable revival attempt (the ``action_log`` revival row)."""

    action_id: int
    idempotency_key: str | None
    retryability: Retryability
    #: False = the SAME idempotency key was already delivered (redelivery
    #: no-op — the caller must not bump a cycle, re-walk, or re-dispatch).
    created: bool


#: Default revival budget (``FORGE_RUN_AUTO_REVIVE_LIMIT``) and the base of
#: the bounded backoff ladder (``FORGE_RUN_REVIVE_BACKOFF_SECONDS``).
DEFAULT_REVIVE_LIMIT = 2
DEFAULT_BACKOFF_SECONDS = 60
#: Backoff ceiling: an unrecoverable transient never waits longer than this
#: between attempts.
MAX_BACKOFF_SECONDS = 900

#: Markers that make a terminal reason *transient*. Matched case-insensitively
#: against the whole reason; 4xx status codes are deliberately absent (a 4xx
#: is a config error — exactly the db5408f4 undeclared-input 422).
_TRANSIENT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(500|502|503|504|429)\b",
        r"bad gateway",
        r"service (temporarily )?unavailable",
        r"gateway time-?out",
        r"internal server error",
        r"rate limit",
        r"too many requests",
        r"timed? ?out",
        r"timeout",
        r"\bconnection\b",
        r"\bnetwork\b",
        r"unreachable",
        r"try again",
        r"runner (startup|start|unavailable|failed|failure)",
        r"no (matching )?runner",
    )
)

#: Leg prefixes whose *empty* error detail still counts as transient: a
#: dispatch that died with an empty message (``httpx`` raises bare
#: ``ConnectError``/``ReadError`` with ``str() == ""``) is infrastructure
#: silence, not a quality signal.
_DISPATCH_PREFIXES = ("harness_start_failed",)


def classify_terminal_failure(reason: str) -> FailureClass:
    """Classify a terminal ``failed`` reason as ``transient`` or ``fatal``."""
    text = (reason or "").strip()
    lowered = text.lower()
    if lowered.startswith(_DISPATCH_PREFIXES) and not _detail_of(text):
        return TRANSIENT
    for marker in _TRANSIENT_MARKERS:
        if marker.search(lowered):
            return TRANSIENT
    return FATAL


def _detail_of(reason: str) -> str:
    """The part of ``prefix: detail`` after the first colon ("" when absent)."""
    _, _, detail = reason.partition(":")
    return detail.strip()


def revival_limit(settings: object) -> int:
    """The configured auto-revive budget (minimum 0 — revive can be off)."""
    return max(int(getattr(settings, "FORGE_RUN_AUTO_REVIVE_LIMIT", DEFAULT_REVIVE_LIMIT) or 0), 0)


def revival_backoff_seconds(attempt: int, settings: object | None = None) -> int:
    """Bounded exponential backoff for the *attempt*-th revive (0-based).

    60s → 120s → 240s … capped at :data:`MAX_BACKOFF_SECONDS`.
    """
    base = int(
        getattr(settings, "FORGE_RUN_REVIVE_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)
        or DEFAULT_BACKOFF_SECONDS
    )
    return min(max(base, 1) * (2 ** max(attempt, 0)), MAX_BACKOFF_SECONDS)


def revival_of(run: FlowRun) -> dict:
    """The run's revival stamp from its evidence ({} when none is pending)."""
    stamp = (run.evidence or {}).get("revival")
    return dict(stamp) if isinstance(stamp, dict) else {}


def revival_due(run: FlowRun, now: datetime) -> bool:
    """Whether *run*'s revival stamp exists and its backoff has elapsed."""
    due_at = revival_of(run).get("due_at")
    if not due_at:
        return False
    try:
        due = as_aware_utc(datetime.fromisoformat(str(due_at)))
    except ValueError:
        return False
    return as_aware_utc(now) >= due


# ----------------------------------------------------------------------
# Tier 1: classification at terminalization + the reconciler revive pass
# ----------------------------------------------------------------------


async def terminalize_failure(
    session_factory: async_sessionmaker,
    settings: object,
    run_id: str,
    *,
    reason: str,
    log: logging.Logger = logger,
) -> None:
    """Park a dead ``failed`` leg as ``blocked`` — reviving it if transient.

    Shared by every provider service's ``_to_terminal(FAILED, …)`` so the
    classification and the budget cannot drift between lanes. The run never
    ends in ``failed`` from here: transient deaths carry a revival stamp the
    reconciler acts on, fatal deaths park ``blocked`` with the precise,
    actionable reason.
    """
    limit = revival_limit(settings)
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            return
        if run.status in {status.value for status in TERMINAL_STATUSES}:
            # A raced double-terminalization: the run is already parked.
            log.warning("Run %s already terminal (%s) — not re-parking", run_id[:8], run.status)
            return
        count = int(revival_of(run).get("count") or 0)
        transient = classify_terminal_failure(reason) is TRANSIENT
        scheduled = transient and count < limit and not run.cancel_requested
        controller = Controller(session)
        if scheduled:
            delay = revival_backoff_seconds(count, settings)
            run.evidence = _merge_evidence(
                run.evidence,
                {
                    "revival": {
                        "count": count + 1,
                        "due_at": (
                            datetime.now(timezone.utc) + timedelta(seconds=delay)
                        ).isoformat(),
                        "reason": reason[:200],
                    }
                },
            )
            await controller.transition(
                run_id,
                FlowStatus.BLOCKED,
                reason=f"transient failure — auto-revive {count + 1}/{limit} in ~{delay}s: "
                f"{reason}"[:200],
            )
            await session.commit()
            log.warning(
                "Run %s -> blocked (transient; auto-revive %d/%d in ~%ds): %s",
                run_id[:8],
                count + 1,
                limit,
                delay,
                reason,
            )
            return
        await controller.transition(run_id, FlowStatus.BLOCKED, reason=reason[:200])
        await session.commit()
    log.warning("Run %s -> blocked (fatal, no auto-retry): %s", run_id[:8], reason)


async def evaluate_revivals(
    session_factory: async_sessionmaker,
    settings: object,
    *,
    provider: str,
    redispatch: Callable[[str], Awaitable[None]],
    now: datetime | None = None,
    log: logging.Logger = logger,
) -> None:
    """One reconciler pass over every run waiting for its auto-revive.

    Scans ``blocked`` runs of *provider* whose revival stamp is due, walks
    each back to ``proposing`` through the revival graph edge (journaled as
    an ``auto_revive`` attempt — the durable revival-attempt record, A11)
    and hands it to *redispatch* — the same ``_advance_harness``/
    ``_advance_proposal`` leg that ``_begin_repair`` uses, on the same
    branch. Not-due runs are skipped: the wait is worker-free, like
    ``waiting_ci``.

    A11 durability: the attempt row carries the window's idempotency key
    (``revive:<run id>:<stamp count>``), so two reconcilers hitting one
    recovery window produce ONE attempt and ONE dispatch; a run holding an
    unresolved (``unknown``/open) publication intent is HELD until the A12
    scanner reconciles it — an unknown remote effect is never re-executed
    blind; and a cancelled grant is never revived.
    """
    now = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == provider,
                        FlowRun.status == FlowStatus.BLOCKED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        due_ids = [run.id for run in runs if revival_due(run, now)]
        holds = {run_id: await revival_hold_reason(session, run_id) for run_id in due_ids}

    for run_id, hold in holds.items():
        if hold is not None:
            log.info("Run %s auto-revive held: %s", run_id[:8], hold)

    for run_id in due_ids:
        if holds.get(run_id) is not None:
            continue
        try:
            attempt = await _begin_auto_revive(session_factory, settings, run_id, now)
        except Exception:
            # Another pass/worker may have taken it; never stall the loop.
            log.exception("Auto-revive walk failed for run %s", run_id[:8])
            continue
        if not attempt.created:
            # Same recovery window, second driver (another reconciler or a
            # re-delivered tick): the idempotency key collapsed it — exactly
            # one attempt, one dispatch.
            continue
        try:
            if not await _claim_attempt_dispatch(session_factory, attempt.action_id, now=now):
                log.info(
                    "Run %s auto-revive dispatch already claimed by another driver",
                    run_id[:8],
                )
                continue
            await redispatch(run_id)
        except Exception as exc:
            log.exception("Auto-revive dispatch failed for run %s", run_id[:8])
            await _complete_auto_revive(
                session_factory, attempt.action_id, "failed", {"error": str(exc)}
            )
        else:
            await _complete_auto_revive(session_factory, attempt.action_id, "succeeded", {})


async def revival_hold_reason(session: AsyncSession, run_id: str) -> str | None:
    """Why auto-revive must NOT re-dispatch *run* yet, or ``None``.

    - a cancelled grant is never revived (R10: ``cancel_requested`` fences
      every effect, revivals included);
    - an UNKNOWN-outcome (or still-open) publication intent must be
      reconciled by the A12 scanner FIRST — a revival here could double-
      publish over an effect whose existence forge has not proven (A12:
      reconcile, never re-execute).
    """
    run = await session.get(FlowRun, run_id)
    if run is None:
        return "run vanished"
    if run.cancel_requested:
        return "the publication grant was cancelled"
    unresolved = (
        await session.execute(
            select(PublicationIntent.id)
            .where(
                PublicationIntent.run_id == run_id,
                PublicationIntent.status.in_(("unknown", *OPEN_STATES)),
            )
            .limit(1)
        )
    ).scalar()
    if unresolved is not None:
        return "an unresolved publication outcome must be reconciled first (A12)"
    return None


async def _begin_auto_revive(
    session_factory: async_sessionmaker, settings: object, run_id: str, now: datetime
) -> RevivalAttempt:
    """Journal the revival ATTEMPT, mark the stamp dispatched and re-open the run.

    The attempt row (``auto_revive``) is written in the SAME transaction as
    the CAS revival transition — crash-proof by construction (A11). Its
    idempotency key is the recovery-window identity
    (``revive:<run id>:<stamp count>``): two reconcilers in one window
    derive the same key and the second gets ``created=False``.
    """
    async with session_factory() as session:
        controller = Controller(session)
        run = await session.get(FlowRun, run_id)
        if run is None or run.status != FlowStatus.BLOCKED.value:
            raise LookupError(f"run {run_id} is no longer a parked revival")
        if run.cancel_requested:
            raise LookupError(f"run {run_id} was cancelled — revival refused")
        if await has_active_run(
            session,
            provider=run.provider,
            project_id=run.project_id,
            issue_iid=run.issue_iid,
            repo_full_name=run.github_repo_full_name,
            exclude_run_id=run.id,
        ):
            # ADR-0017: one active run per subject — a fresh /implement wins
            # over a stale revival stamp (the run stays parked and due).
            raise LookupError(f"run {run_id} has a live sibling run — revival superseded")
        revival = revival_of(run)
        count = int(revival.get("count") or 0)
        attempt = await begin_revival_attempt(
            session,
            run_id=run_id,
            kind="auto_revive",
            idempotency_key=auto_revive_key(run_id, count),
            retryability=classify_retryability("auto_revive", str(revival.get("reason") or "")),
        )
        if not attempt.created:
            await session.commit()
            return attempt
        # Consume the due stamp before dispatching: a second pass must never
        # re-dispatch while this leg is in flight.
        run.evidence = _merge_evidence(
            run.evidence,
            {"revival": {"count": count, "dispatched_at": now.isoformat()}},
        )
        await controller.revive_transition(
            run_id,
            reason=f"auto-revive {count + 1}/{revival_limit(settings)}: "
            f"{revival.get('reason') or 'transient failure'}"[:200],
            authorized_by="auto_revive",
        )
        await session.commit()
        return attempt


async def _complete_auto_revive(
    session_factory: async_sessionmaker, action_id: int, status: str, result: dict
) -> None:
    async with session_factory() as session:
        controller = Controller(session)
        await controller.complete_action(action_id, status, result or None)  # type: ignore[arg-type]
        await session.commit()


# ----------------------------------------------------------------------
# A11: the revival attempt record (the action journal's revival rows)
# ----------------------------------------------------------------------


def retry_delivery_key(delivery_id: str | None) -> str | None:
    """The attempt idempotency key for a ``/retry`` delivery id.

    ``delivery:<id>``; ``None`` when the caller has no delivery identity
    (direct/test invocations) — those keep the pre-A11 no-dedup behavior.
    """
    text = (delivery_id or "").strip()
    return f"delivery:{text}" if text else None


def auto_revive_key(run_id: str, stamp_count: int) -> str:
    """The attempt idempotency key for one auto-revive recovery window.

    ``revive:<run id>:<stamp count>`` — every reconciler tick driving the
    SAME window derives the same key, so the window is driven once.
    """
    return f"revive:{run_id}:{int(stamp_count)}"


async def find_revival_attempt(
    session: AsyncSession,
    *,
    run_id: str,
    kind: RevivalKind,
    idempotency_key: str,
) -> ActionLog | None:
    """The (already-delivered) attempt for this exact trigger identity."""
    return (
        (
            await session.execute(
                select(ActionLog)
                .where(
                    ActionLog.flow_run_id == run_id,
                    ActionLog.action_kind == kind,
                    ActionLog.idempotency_key == idempotency_key,
                )
                .order_by(ActionLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def open_revival_attempt(session: AsyncSession, *, run_id: str) -> ActionLog | None:
    """The run's OPEN (dispatch-pending) revival attempt, newest first."""
    return (
        (
            await session.execute(
                select(ActionLog)
                .where(
                    ActionLog.flow_run_id == run_id,
                    ActionLog.action_kind.in_(REVIVAL_ACTION_KINDS),
                    ActionLog.status == "requested",
                )
                .order_by(ActionLog.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def begin_revival_attempt(
    session: AsyncSession,
    *,
    run_id: str,
    kind: RevivalKind,
    idempotency_key: str | None,
    retryability: Retryability,
) -> RevivalAttempt:
    """Write the revival attempt row into the CALLER's transaction (A11).

    The row is the attempt record: ``requested`` + ``dispatch_state='pending'``
    until a driver claims the dispatch. Call it in the SAME transaction as
    :meth:`Controller.revive_transition` — the attempt and the walk commit
    atomically, so a crash between them leaves a findable, recoverable
    record instead of a lost redispatch.

    - the SAME ``idempotency_key`` already delivered → ``created=False``
      (a webhook redelivery: no-op for the caller);
    - a DIFFERENT trigger's attempt still open → :class:`RevivalInFlight`
      (the partial unique index ``uq_revival_attempt_inflight`` is the race
      arbiter — a concurrent insert that loses raises it too).
    """
    if idempotency_key:
        existing = await find_revival_attempt(
            session, run_id=run_id, kind=kind, idempotency_key=idempotency_key
        )
        if existing is not None:
            return RevivalAttempt(
                action_id=existing.id,
                idempotency_key=idempotency_key,
                retryability=retryability,
                created=False,
            )
        in_flight = await open_revival_attempt(session, run_id=run_id)
        if in_flight is not None:
            raise RevivalInFlight(
                f"run {run_id[:8]} has an in-flight {in_flight.action_kind} attempt "
                f"({in_flight.idempotency_key or 'unkeyed'}) — a new revival would "
                "double-dispatch"
            )
    action = ActionLog(
        flow_run_id=run_id,
        action_kind=kind,
        status="requested",
        idempotency_key=idempotency_key,
        retryability=retryability.value,
        dispatch_state="pending",
    )
    session.add(action)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two drivers raced past the lookups; the index picked one winner.
        raise RevivalInFlight(
            f"run {run_id[:8]} already has an in-flight revival attempt (index arbiter)"
        ) from exc
    return RevivalAttempt(
        action_id=action.id,
        idempotency_key=idempotency_key,
        retryability=retryability,
        created=True,
    )


async def claim_attempt_dispatch(
    session: AsyncSession, action_id: int, *, now: datetime | None = None
) -> bool:
    """Claim the attempt's dispatch leg — the ``pending → dispatched`` CAS.

    ``UPDATE … WHERE dispatch_state IS NULL-or-'pending'``: the rowcount is
    the ownership decision, so two drivers (a live flow and a recovery scan,
    or two scans) produce exactly one claimer and one dispatch. The claim
    instant is stamped into ``correlation_id`` as ``claim:<iso>`` (unused
    otherwise on revival rows) — the recovery scan's re-arm compares it
    lexicographically, so a scan can never re-arm an attempt whose claim is
    younger than the recovery bound. Flushes into the caller's transaction.
    """
    stamp = f"claim:{(now or datetime.now(timezone.utc)).isoformat()}"
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ActionLog)
            .where(
                ActionLog.id == action_id,
                or_(ActionLog.dispatch_state.is_(None), ActionLog.dispatch_state == "pending"),
            )
            .values(dispatch_state="dispatched", correlation_id=stamp)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


async def _claim_attempt_dispatch(
    session_factory: async_sessionmaker, action_id: int, *, now: datetime | None = None
) -> bool:
    """Own-session wrapper around :func:`claim_attempt_dispatch`."""
    async with session_factory() as session:
        claimed = await claim_attempt_dispatch(session, action_id, now=now)
        await session.commit()
        return claimed


# ----------------------------------------------------------------------
# A11: the revival-attempt recovery scan (the R11 scanner pattern)
# ----------------------------------------------------------------------

#: How long a ``pending`` attempt may sit before the recovery scan presumes
#: its driver died between the revive commit and the dispatch claim. The
#: live flow's pending window is one note-post call wide (provider HTTP
#: timeouts are far below this).
DISPATCH_RECOVERY_PENDING_SECONDS = 120

#: How long a ``dispatched`` (claimed, never completed) attempt may sit
#: before the recovery scan resolves it. Generous on purpose: a builtin-lane
#: dispatch leg legitimately spends minutes inside model calls before its
#: first journaled write, and a live leg must never be mistaken for a crash.
DISPATCH_RECOVERY_DISPATCHED_SECONDS = 1800

#: Action kinds whose journal row proves a dispatch leg ENTERED (the legs
#: journal intent-first, ADR-0005) — the double-dispatch guard: an OPEN row
#: of one of these kinds created at/after the attempt means the leg was
#: interrupted mid-flight and its remote effect is UNKNOWN, so the scan
#: resolves the journal and parks the run instead of ever re-dispatching.
_DISPATCH_LEG_ACTION_KINDS: frozenset[str] = frozenset(
    {"harness_start", "harness_fallback", "commit", "create_merge_request", "update_merge_request"}
)


async def due_revival_attempts(
    session: AsyncSession,
    *,
    provider: str,
    now: datetime,
    pending_bound: int = DISPATCH_RECOVERY_PENDING_SECONDS,
    dispatched_bound: int = DISPATCH_RECOVERY_DISPATCHED_SECONDS,
) -> list[tuple[ActionLog, FlowRun]]:
    """The stranded revival attempts of *provider*, oldest first.

    A ``pending`` attempt older than *pending_bound* never reached its
    dispatch claim (crash between the revive commit and the dispatch). A
    ``dispatched`` attempt older than *dispatched_bound* was claimed but its
    journal was never completed (crash inside the dispatch leg). Pre-017
    rows (``dispatch_state`` NULL) count as pending.
    """
    now = as_aware_utc(now)
    rows = (
        await session.execute(
            select(ActionLog, FlowRun)
            .join(FlowRun, FlowRun.id == ActionLog.flow_run_id)
            .where(
                FlowRun.provider == provider,
                ActionLog.action_kind.in_(REVIVAL_ACTION_KINDS),
                ActionLog.status == "requested",
                or_(
                    and_(
                        or_(
                            ActionLog.dispatch_state.is_(None),
                            ActionLog.dispatch_state == "pending",
                        ),
                        ActionLog.created_at < now - timedelta(seconds=pending_bound),
                    ),
                    and_(
                        ActionLog.dispatch_state == "dispatched",
                        ActionLog.created_at < now - timedelta(seconds=dispatched_bound),
                    ),
                ),
            )
            .order_by(ActionLog.id.asc())
        )
    ).all()
    return [(action, run) for action, run in rows]


async def evaluate_attempt_recovery(
    session_factory: async_sessionmaker,
    *,
    provider: str,
    redispatch: Callable[[str], Awaitable[None]],
    now: datetime | None = None,
    pending_bound: int = DISPATCH_RECOVERY_PENDING_SECONDS,
    dispatched_bound: int = DISPATCH_RECOVERY_DISPATCHED_SECONDS,
    log: logging.Logger = logger,
) -> int:
    """One recovery pass over stranded revival attempts (A11).

    Mirrors the R11 intent-scanner shape: a reconciler pass over the
    durable attempt rows whose driver may have died, re-driving each
    stranded dispatch EXACTLY ONCE —

    - ``pending`` (crash between the revive commit and the dispatch): the
      claim CAS is the once-arbiter; the winner re-drives the dispatch leg
      and completes the attempt.
    - ``dispatched`` but never completed (crash inside the leg): resolved
      by inspection, never blind — a run that moved on finishes the journal
      as succeeded; a run still ``proposing`` with an OPEN journaled
      dispatch-leg action (the leg's own intent-first journal is the
      double-dispatch guard) completes the attempt ``unknown_outcome`` and
      parks the run ``blocked(revival_dispatch_interrupted)`` for an
      operator (an unknown remote effect is reconciled, never re-executed —
      A12/ADR-0005); a run still ``proposing`` with NO journaled leg action
      is re-armed to ``pending`` and re-driven through the same claim.

    Returns the number of attempts whose dispatch was (re-)driven.
    """
    now = as_aware_utc(now) if now is not None else datetime.now(timezone.utc)
    async with session_factory() as session:
        stranded = await due_revival_attempts(
            session,
            provider=provider,
            now=now,
            pending_bound=pending_bound,
            dispatched_bound=dispatched_bound,
        )
    re_driven = 0
    for action, run in stranded:
        run_id = str(action.flow_run_id)
        try:
            if await _recover_one_attempt(
                session_factory,
                redispatch,
                action,
                run,
                now=now,
                dispatched_bound=dispatched_bound,
                log=log,
            ):
                re_driven += 1
        except Exception:
            # One broken attempt must not stall the recovery pass.
            log.exception("Revival-attempt recovery failed for run %s", run_id[:8])
    return re_driven


async def _recover_one_attempt(
    session_factory: async_sessionmaker,
    redispatch: Callable[[str], Awaitable[None]],
    action: ActionLog,
    run: FlowRun,
    *,
    now: datetime,
    dispatched_bound: int,
    log: logging.Logger,
) -> bool:
    """Resolve ONE stranded attempt. True when its dispatch was re-driven."""
    run_id = str(action.flow_run_id)
    if run.status != FlowStatus.PROPOSING.value:
        # The dispatch demonstrably happened and the run moved on (or another
        # decision parked it): finish the journal, never re-drive.
        outcome = "succeeded" if action.dispatch_state == "dispatched" else "failed"
        await _complete_auto_revive(
            session_factory,
            action.id,
            outcome,
            {"recovered": "run_moved_on", "run_status": run.status},
        )
        log.info(
            "Run %s stranded %s attempt resolved (%s): run is %s",
            run_id[:8],
            action.action_kind,
            outcome,
            run.status,
        )
        return False
    if action.dispatch_state == "dispatched":
        if await _dispatch_leg_open(session_factory, run_id, since=action.created_at):
            # The dispatch leg ENTERED (its intent-first journal row is open)
            # and its outcome is unknown: resolve the journal, park the run —
            # never a blind re-dispatch (A12/ADR-0005).
            await _complete_auto_revive(
                session_factory,
                action.id,
                "unknown_outcome",
                {"recovered": "dispatch_leg_interrupted"},
            )
            await _park_interrupted_revival(session_factory, action, log=log)
            return False
        # Claimed but the leg never journaled anything: nothing was started —
        # re-arm to pending and fall through to the same claim path. The
        # re-arm is fenced by the claim stamp: an attempt whose claim is
        # younger than *dispatched_bound* is a live driver's window and is
        # left alone (a concurrent scan mid-leg is never double-driven).
        if not await _rearm_attempt(
            session_factory, action.id, now=now, bound_seconds=dispatched_bound
        ):
            return False  # a live driver's claim — not ours to recover
    if not await _claim_attempt_dispatch(session_factory, action.id, now=now):
        return False  # another scan/flow claimed the dispatch
    try:
        await redispatch(run_id)
    except Exception as exc:
        await _complete_auto_revive(
            session_factory, action.id, "failed", {"recovered": True, "error": str(exc)}
        )
        log.exception("Recovery re-dispatch failed for run %s", run_id[:8])
        return False
    await _complete_auto_revive(session_factory, action.id, "succeeded", {"recovered": True})
    log.warning("Run %s stranded revival attempt re-driven exactly once (A11)", run_id[:8])
    return True


async def _dispatch_leg_open(
    session_factory: async_sessionmaker, run_id: str, *, since: datetime
) -> bool:
    """Whether a dispatch-leg journal row opened at/after *since* is OPEN.

    The journaled dispatch action is the re-drive guard: an open row means
    the leg was interrupted mid-flight and its remote effect is unknown.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                select(ActionLog.id)
                .where(
                    ActionLog.flow_run_id == run_id,
                    ActionLog.action_kind.in_(_DISPATCH_LEG_ACTION_KINDS),
                    ActionLog.status == "requested",
                    ActionLog.created_at >= as_aware_utc(since),
                )
                .limit(1)
            )
        ).scalar()
    return row is not None


async def _rearm_attempt(
    session_factory: async_sessionmaker, action_id: int, *, now: datetime, bound_seconds: int
) -> bool:
    """``dispatched → pending`` for a claimed-but-never-started attempt.

    Fenced by the claim stamp (``correlation_id = 'claim:<iso>'``): only a
    claim older than *bound_seconds* is presumed dead — a concurrent
    driver's fresh claim is never stolen mid-leg.
    """
    cutoff = f"claim:{(as_aware_utc(now) - timedelta(seconds=bound_seconds)).isoformat()}"
    async with session_factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(ActionLog)
                .where(
                    ActionLog.id == action_id,
                    ActionLog.dispatch_state == "dispatched",
                    ActionLog.correlation_id.is_not(None),
                    ActionLog.correlation_id < cutoff,
                )
                .values(dispatch_state="pending", correlation_id=None)
                .execution_options(synchronize_session=False)
            ),
        )
        await session.commit()
        return result.rowcount == 1


async def _park_interrupted_revival(
    session_factory: async_sessionmaker, action: ActionLog, *, log: logging.Logger
) -> None:
    """Park a run whose revival dispatch leg was interrupted mid-flight.

    The reason prefix classifies FATAL on purpose: an unknown remote effect
    must be reconciled/inspected by an operator, never auto-revived into a
    blind re-dispatch (``/retry`` remains the explicit re-drive).
    """
    run_id = str(action.flow_run_id)
    reason = (
        f"revival_dispatch_interrupted: the {action.action_kind} dispatch leg was interrupted "
        "mid-flight — its remote effect is unknown, so forge will not re-dispatch blind. "
        "Inspect the provider for an orphan job, then /retry."
    )[:200]
    async with session_factory() as session:
        controller = Controller(session)
        await controller.transition(run_id, FlowStatus.BLOCKED, reason=reason)
        await session.commit()
    log.warning(
        "Run %s revival dispatch leg interrupted — parked blocked for operator inspection",
        run_id[:8],
    )


# ----------------------------------------------------------------------
# A13: the config-block recovery pass
# ----------------------------------------------------------------------

#: Reason prefixes marking a run parked by the A13 config gate
#: (``blocked(config_unreadable: …)`` / ``blocked(config_invalid: …)``).
#: The reconciler retries those reads; a run whose config reads again (or
#: is provider-confirmed absent) re-enters planning — nothing was paid or
#: committed while it waited, and its scope never widened meanwhile.
CONFIG_BLOCK_PREFIXES = ("config_unreadable", "config_invalid")


async def evaluate_config_blocks(
    session_factory: async_sessionmaker,
    *,
    provider: str,
    reread: Callable[[int], Awaitable["ConfigReadResult"]],
    replan: Callable[[str, dict], Awaitable[None]],
    log: logging.Logger = logger,
) -> int:
    """One reconciler pass over runs parked ``blocked(config_…)`` (A13).

    For every blocked run of *provider* whose reason is a config gate:

    - the read is retried through *reread* (a typed
      :class:`~forge.orchestrator.project_config.ConfigReadResult`) — a
      still-failing read leaves the run parked (the retry is free);
    - a recovered read (``valid`` or provider-confirmed absence) walks the
      run back to ``preflight`` through the journaled plan-restart edge
      (:meth:`Controller.restart_plan_transition` — fenced to runs that
      never froze a spec, so an approved input is never re-planned past
      its gate) and hands it to *replan* with the stashed start context.

    A run with a live sibling run stays parked: a fresh ``/implement``
    wins over a parked run (ADR-0017, same as the revival stamps).

    Returns the number of runs re-entering planning.
    """
    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun).where(
                        FlowRun.provider == provider,
                        FlowRun.status == FlowStatus.BLOCKED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates: list[tuple[str, int, dict]] = [
            (
                run.id,
                run.project_id,
                dict((run.evidence or {}).get("config_block") or {}),
            )
            for run in runs
            if str(run.status_reason or "").startswith(CONFIG_BLOCK_PREFIXES)
        ]

    resumed = 0
    for run_id, project_id, stash in candidates:
        try:
            result = await reread(project_id)
        except Exception:
            # A retry pass must never kill the reconciler task.
            log.exception("Config-block retry read failed for run %s", run_id[:8])
            continue
        if result.needs_block:
            continue  # still unreadable/invalid — stay parked, stay unpaid
        try:
            if not await _begin_config_recovery(session_factory, provider, run_id, log=log):
                continue
        except Exception:
            # Another pass/worker may have taken it; never stall the loop.
            log.exception("Config-recovery walk failed for run %s", run_id[:8])
            continue
        try:
            await replan(run_id, stash)
        except Exception:
            log.exception("Config-recovery replan failed for run %s", run_id[:8])
        else:
            resumed += 1
    return resumed


async def _begin_config_recovery(
    session_factory: async_sessionmaker,
    provider: str,
    run_id: str,
    *,
    log: logging.Logger,
) -> bool:
    """Walk one config-blocked run back to ``preflight`` (guarded).

    Consumes the parked state exactly once: a second pass finds the run no
    longer ``blocked`` and does nothing. ``False`` when the run was taken
    (terminal, or superseded by a live sibling run).
    """
    async with session_factory() as session:
        controller = Controller(session)
        run = await session.get(FlowRun, run_id)
        if run is None or run.status != FlowStatus.BLOCKED.value:
            return False
        if await has_active_run(
            session,
            provider=provider,
            project_id=run.project_id,
            issue_iid=run.issue_iid,
            repo_full_name=run.github_repo_full_name,
            exclude_run_id=run.id,
        ):
            log.info(
                "Run %s config recovered but a sibling run is active — stays parked",
                run_id[:8],
            )
            return False
        await controller.restart_plan_transition(
            run_id,
            reason="project config readable again — re-entering planning (A13)",
            authorized_by="config_recovery",
        )
        await session.commit()
    log.info("Run %s config recovered — re-entering planning", run_id[:8])
    return True


# ----------------------------------------------------------------------
# Tier 2: the operator /retry target resolution and guards
# ----------------------------------------------------------------------

#: Statuses a run must be parked in for ``/retry`` to touch it.
_RETRYABLE_STATUSES = frozenset({FlowStatus.FAILED.value, FlowStatus.BLOCKED.value})


async def has_active_run(
    session: AsyncSession,
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    repo_full_name: str | None = None,
    exclude_run_id: str | None = None,
) -> bool:
    """Whether another NON-terminal run is live for the subject.

    The durable one-active-run-per-subject invariant (ADR-0017) is enforced
    by a partial unique index — a revival walk that ignored it would blow up
    on commit; both revival tiers check before walking instead.
    """
    query = select(FlowRun.id).where(
        FlowRun.provider == provider,
        FlowRun.project_id == project_id,
        FlowRun.issue_iid == issue_iid,
        FlowRun.status.not_in([status.value for status in TERMINAL_STATUSES]),
    )
    if repo_full_name:
        query = query.where(FlowRun.github_repo_full_name == repo_full_name)
    if exclude_run_id:
        query = query.where(FlowRun.id != exclude_run_id)
    return (await session.execute(query.limit(1))).scalar() is not None


def _subject_query(
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    repo_full_name: str | None,
) -> Select:
    """The subject-scoped base query EVERY target resolution starts from (A07).

    Provider + connection + project + repo + issue — the full subject — is
    mandatory for every id form (bare, 8-char prefix, and the full 32-char id
    alike): a run from another project that happens to share the issue iid is
    invisible here, exactly like the provider-scoped ``/cancel`` lookups.
    """
    query = select(FlowRun).where(
        FlowRun.provider == provider,
        FlowRun.project_id == project_id,
        FlowRun.issue_iid == issue_iid,
    )
    if repo_full_name is not None:
        query = query.where(FlowRun.github_repo_full_name == repo_full_name)
    return query


async def resolve_retry_target(
    session: AsyncSession,
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    requested: str,
    repo_full_name: str | None = None,
) -> FlowRun | None:
    """The run ``/retry [run-id]`` refers to — latest dead run when bare.

    Mirrors ``/cancel``'s resolution: an explicit 32-char id, a unique 8-char
    prefix among the issue's runs, or — bare — the most recent
    ``failed``/``blocked`` run for the issue. ``None`` when nothing matches.
    Every form resolves through the SAME subject-scoped query (A07) — the
    full id no longer takes a looser lookup.
    """
    query = _subject_query(
        provider=provider,
        project_id=project_id,
        issue_iid=issue_iid,
        repo_full_name=repo_full_name,
    )

    if not requested:
        dead = query.where(FlowRun.status.in_(sorted(_RETRYABLE_STATUSES))).order_by(
            FlowRun.updated_at.desc()
        )
        return (await session.execute(dead)).scalars().first()

    if len(requested) == 32:
        exact = query.where(FlowRun.id == requested)
        return (await session.execute(exact.limit(1))).scalars().first()

    matches = (
        (
            await session.execute(
                query.where(FlowRun.id.like(f"{requested}%")).order_by(FlowRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return matches[0] if len(matches) == 1 else None


def retry_rejection(run: FlowRun | None, *, other_active: bool = False) -> str:
    """Why ``/retry`` refuses *run* — an actionable message, or "" when fine."""
    if run is None:
        return (
            "`/retry` found no retryable run on this issue. "
            "Start a fresh run with `@forge /implement`."
        )
    if other_active:
        return (
            f"Run `{run.id[:8]}` cannot be retried: another run is already in flight on this "
            "subject — forge keeps one active run per subject. Let it finish or `/cancel` it "
            "first."
        )
    status = run.status
    if status not in _RETRYABLE_STATUSES:
        return (
            f"Run `{run.id[:8]}` is `{status}`, not `failed`/`blocked` — there is nothing to "
            "retry. Cancelled runs and fresh work need `@forge /implement`."
        )
    if run.cancel_requested:
        return (
            f"Run `{run.id[:8]}` was cancelled by an operator — retrying a revoked publication "
            "grant is not allowed. Start fresh with `@forge /implement`."
        )
    if not list(run.candidate_shas or []):
        return (
            f"Run `{run.id[:8]}` died before it committed a candidate, so there is no work to "
            "retry in place. Start fresh with `@forge /implement`."
        )
    return ""


def retry_in_flight_rejection(run_id: str) -> str:
    """Why a DIFFERENT delivery's ``/retry`` is refused mid-revival (A11).

    An attempt is already open (dispatch pending/in flight): a second
    revival of the same run would double-dispatch. The existing rejection-
    note machinery carries the refusal.
    """
    return (
        f"Run `{run_id[:8]}` already has a revival in flight — its dispatch is being driven. "
        "A second retry now would dispatch twice; wait for the current attempt to land, or "
        "`/cancel` the run first."
    )


# ----------------------------------------------------------------------
# R29: operator surface around dead/stuck runs (/status, /why-blocked,
# /reconcile) — shared by the GitLab, GitHub and Azure DevOps services so
# the reply content and the target resolution cannot drift per lane.
# ----------------------------------------------------------------------

#: ``/status [run-id]`` — bare reports the issue's LATEST run, any state.
STATUS_RE = re.compile(r"/status(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)
#: ``/why-blocked [run-id]`` — bare targets the issue's latest run, any state
#: (the reply says precisely when the run is NOT blocked, too).
WHY_BLOCKED_RE = re.compile(r"/why-blocked(?:\s+([0-9a-f]{8,32})\b)?", re.IGNORECASE)
#: ``/reconcile <run-id>`` — the run id is REQUIRED: recovery is targeted,
#: never guessed off the issue's latest run.
RECONCILE_RE = re.compile(r"/reconcile\s+([0-9a-f]{8,32})\b", re.IGNORECASE)


async def resolve_status_target(
    session: AsyncSession,
    *,
    provider: str,
    project_id: int,
    issue_iid: int | None,
    requested: str,
    repo_full_name: str | None = None,
) -> FlowRun | None:
    """The run ``/status [run-id]`` / ``/why-blocked [run-id]`` refers to.

    Same resolution discipline as :func:`resolve_retry_target` — explicit
    32-char id, unique 8-char prefix among the issue's runs, or — bare — the
    most recent run of ANY state for the issue (a status question is about
    the latest run, alive or dead). ``None`` when nothing matches. Every
    form resolves through the SAME subject-scoped query (A07) — the full id
    no longer takes a looser lookup.
    """
    query = _subject_query(
        provider=provider,
        project_id=project_id,
        issue_iid=issue_iid,
        repo_full_name=repo_full_name,
    )

    if not requested:
        latest = query.order_by(FlowRun.updated_at.desc())
        return (await session.execute(latest)).scalars().first()

    if len(requested) == 32:
        exact = query.where(FlowRun.id == requested)
        return (await session.execute(exact.limit(1))).scalars().first()

    matches = (
        (
            await session.execute(
                query.where(FlowRun.id.like(f"{requested}%")).order_by(FlowRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return matches[0] if len(matches) == 1 else None


async def intents_for_run(session: AsyncSession, run_id: str) -> list[PublicationIntent]:
    """Every publication intent of *run*, newest first (R11 surface).

    Shared by ``/status`` (intent states as evidence) and ``/reconcile``
    (the intents the operator-driven probe pass resolves).
    """
    return list(
        (
            await session.execute(
                select(PublicationIntent)
                .where(PublicationIntent.run_id == run_id)
                .order_by(PublicationIntent.created_at.desc(), PublicationIntent.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def collect_status_snapshot(session: AsyncSession, run: FlowRun) -> dict[str, Any]:
    """Everything ``/status`` reports, read in ONE session — no side effects.

    Pure reads over the durable row plus its satellite tables (budget,
    publication intents, revive/retry action counters). No transitions, no
    model calls, no provider I/O: the reply is composed from persisted state
    only (R29).
    """
    from forge.durable.budgets import budget_for_run  # lazy: budgets is a sibling module

    budget = await budget_for_run(session, run.id)
    intents = await intents_for_run(session, run.id)
    revive_counts: dict[str, int] = {
        kind: count
        for kind, count in (
            await session.execute(
                select(ActionLog.action_kind, func.count())
                .where(
                    ActionLog.flow_run_id == run.id,
                    ActionLog.action_kind.in_(("retry_requested", "auto_revive")),
                )
                .group_by(ActionLog.action_kind)
            )
        ).all()
    }
    verification = (run.evidence or {}).get("verification")
    return {
        "run_id": run.id,
        "status": run.status,
        "status_reason": run.status_reason or "",
        "commit_cycle": int(run.commit_cycle or 1),
        "candidate_shas": [str(sha) for sha in (run.candidate_shas or [])],
        "mr_iid": run.mr_iid,
        "budget": _budget_view(budget),
        "verification": verification if isinstance(verification, dict) else {},
        "intents": [
            {
                "id": intent.id,
                "status": intent.status,
                "target_ref": intent.target_ref,
                "operation_key": intent.operation_key,
                "updated_at": _iso(intent.updated_at),
            }
            for intent in intents
        ],
        "revival": revival_of(run),
        "retry_count": int(revive_counts.get("retry_requested") or 0),
        "auto_revive_count": int(revive_counts.get("auto_revive") or 0),
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _budget_view(budget: RunBudget | None) -> dict[str, Any] | None:
    """The remaining budget headroom (exposure = limit − spent − held), if any."""
    if budget is None:
        return None

    def remaining(limit: int | None, *counters: int | None) -> int | None:
        if limit is None:
            return None
        return max(int(limit) - sum(int(c or 0) for c in counters), 0)

    return {
        "status": budget.status,
        "max_calls": budget.max_calls,
        "calls_remaining": remaining(
            budget.max_calls,
            budget.consumed_calls,
            budget.reserved_calls,
            budget.unresolved_calls,
        ),
        "max_tokens": budget.max_tokens,
        "tokens_remaining": remaining(
            budget.max_tokens,
            budget.consumed_tokens,
            budget.reserved_tokens,
            budget.unresolved_tokens,
        ),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def format_status_reply(snapshot: dict[str, Any]) -> str:
    """The ``/status`` note: the durable facts, nothing else (read-only)."""
    run_id = str(snapshot["run_id"])
    lines = [f"## Forge — run `{run_id[:8]}` status", ""]
    status = str(snapshot["status"])
    reason = str(snapshot["status_reason"] or "")
    lines.append(f"- **Status:** `{status}`" + (f" — {reason}" if reason else ""))
    lines.append(f"- **Commit cycle:** {snapshot['commit_cycle']}")
    candidates = list(snapshot["candidate_shas"])
    if candidates:
        lines.append("- **Candidates:** " + ", ".join(f"`{sha[:8]}`" for sha in candidates))
    else:
        lines.append("- **Candidates:** none yet")

    budget = snapshot["budget"]
    if budget is None:
        lines.append("- **Budget:** no budget row (no forge-side budget was opened)")
    else:
        parts = [f"status `{budget['status']}`"]
        if budget["max_calls"] is not None:
            parts.append(f"calls {budget['calls_remaining']}/{budget['max_calls']} left")
        if budget["max_tokens"] is not None:
            parts.append(f"tokens {budget['tokens_remaining']}/{budget['max_tokens']} left")
        lines.append("- **Budget:** " + ("; ".join(parts) if parts else "unlimited"))

    verification = dict(snapshot["verification"] or {})
    if verification:
        bound = str(verification.get("tested_oid") or verification.get("candidate_sha") or "")
        lines.append(
            f"- **Verification:** `{verification.get('status')}`"
            + (f" on `{bound[:8]}`" if bound else "")
            + (f" — {verification.get('summary')}" if verification.get("summary") else "")
        )
    else:
        lines.append("- **Verification:** none recorded yet")

    intents = list(snapshot["intents"])
    if intents:
        lines.append("- **Publication intents:**")
        lines.extend(
            f"  - `{intent['id'][:8]}` **{intent['status']}** → `{intent['target_ref']}`"
            for intent in intents
        )
    else:
        lines.append("- **Publication intents:** none")

    revival = dict(snapshot["revival"] or {})
    lines.append(
        f"- **Revive/retry:** auto-revives {snapshot['auto_revive_count']}"
        + (
            f" (last stamp: {int(revival.get('count') or 0)}, due {revival.get('due_at')})"
            if revival
            else ""
        )
        + f", operator retries {snapshot['retry_count']}"
    )
    lines.append(
        f"- **Created:** {snapshot['created_at']}  •  **Updated:** {snapshot['updated_at']}"
    )
    lines.append("")
    lines.append("*This is an automated message.*")
    return "\n".join(lines)


def why_blocked_reply(run: FlowRun, *, other_active: bool = False) -> str:
    """The ``/why-blocked`` note: the precise cause + the honest revival paths.

    READ-ONLY: the parked reason, its Tier-1 classification, and the
    revive/retry verdict — :func:`retry_rejection` is THE one rejection
    table (R29), so this reply can never promise a revival ``/retry``
    would refuse.
    """
    status = run.status
    reason = (run.status_reason or "").strip()
    lines = [f"## Forge — run `{run.id[:8]}`: why blocked", ""]
    if status in _RETRYABLE_STATUSES:
        lines.append(f"- **Status:** `{status}` — the run is parked, not in flight.")
        if reason:
            classification = classify_terminal_failure(reason)
            why = (
                "transient (infrastructure) failure — Tier 1 auto-revives it with bounded backoff"
                if classification is TRANSIENT
                else "fatal failure — auto-revive will not touch it (a real signal, not noise)"
            )
            lines.append(f"- **Cause:** {reason} *(classified {classification}: {why})*")
        else:
            lines.append("- **Cause:** no reason was recorded (the row parks silently).")
        revival = revival_of(run)
        if revival:
            lines.append(
                f"- **Auto-revive stamp:** attempt {revival.get('count')}, "
                f"due {revival.get('due_at') or '—'}, "
                f"dispatched {revival.get('dispatched_at') or '—'}"
            )
        rejection = retry_rejection(run, other_active=other_active)
        if rejection:
            lines.append(f"- **Not /retry-eligible:** {rejection}")
        else:
            lines.append(
                f"- **This run is /retry-eligible:** `@forge /retry {run.id[:8]}` continues it "
                "in place (one extra commit cycle, same branch, no re-planning)."
            )
    elif status == FlowStatus.WAITING_APPROVAL.value:
        lines.append(
            f"- **Status:** `{status}` — not blocked: the plan is waiting for a human gate. "
            f"Approve it with `@forge /go {run.id}`."
        )
    elif status == FlowStatus.READY_FOR_HUMAN.value:
        lines.append(
            f"- **Status:** `{status}` — not blocked: the run finished and waits for a human "
            "merge decision."
        )
    elif status == FlowStatus.CANCELLED.value:
        lines.append(
            f"- **Status:** `{status}` — the run was cancelled"
            + (f": {reason}" if reason else "")
            + ". Fresh work needs `@forge /implement`."
        )
    else:
        lines.append(f"- **Status:** `{status}` — the run is in flight, not blocked.")
        if reason:
            lines.append(f"- **Last noted reason:** {reason}")
    lines.append("")
    lines.append("*This is an automated message.*")
    return "\n".join(lines)


def format_reconcile_reply(run_id: str, intents: list[PublicationIntent]) -> str:
    """The ``/reconcile`` resolution note: what the probe pass proved.

    One line per intent — ``adopted`` (the lost publication is now the
    run's candidate), ``duplicated`` (someone else owns the ref),
    ``unknown`` (+ the manual-inspection instruction — never guessed), or
    still open (nothing had landed; the run's own publish leg re-dispatches
    with the SAME key).
    """
    lines = [f"## Forge — reconcile of run `{run_id[:8]}`", ""]
    if not intents:
        lines.append("No publication intents were found — nothing to reconcile.")
    for intent in intents:
        summary = f"- Intent `{intent.id[:8]}` on `{intent.target_ref}`: **{intent.status}**"
        result = dict(intent.remote_result or {})
        if intent.status == "adopted":
            sha = str(intent.provider_object_id or result.get("sha") or "")
            summary += f" — the landed commit `{sha[:8]}` was adopted as the run's candidate."
        elif intent.status == "duplicated":
            why = str(result.get("reason") or result.get("branch") or "")
            summary += (
                " — the branch moved away from the intent; forge never adopted it"
                + (f" ({why})" if why else "")
                + "."
            )
        elif intent.status == "unknown":
            summary += (
                " — outcome UNRESOLVED. An operator must inspect branch "
                f"`{intent.target_ref}` and reconcile manually; forge will not "
                "re-publish over an unknown outcome."
            )
        elif intent.status in OPEN_STATES:
            summary += (
                " — still open: the probe proved nothing had landed and the head is intact; "
                "the run's own publish leg re-dispatches with the same key."
            )
        else:
            summary += "."
        lines.append(summary)
    lines.append("")
    lines.append("*This is an automated message.*")
    return "\n".join(lines)


def _merge_evidence(evidence: dict | None, patch: dict) -> dict:
    """Shallow-merge *patch* into the run's evidence blob (as the services do)."""
    merged = dict(evidence or {})
    merged.update(patch)
    return merged


def build_retry_context(settings: Settings, status_reason: str, evidence: dict) -> str:
    """Bounded ``/retry`` brief: why the run stopped + its last verification evidence.

    Shared by every provider so a retried lane gets the same shape of context.
    Redacted like all CI-derived context (F23) and capped like all repair
    briefs (ADR-0013).
    """
    from forge.policy.evidence import EvidencePolicy
    from forge.runs.service import REPAIR_CONTEXT_MAX_CHARS  # lazy: avoids the import cycle

    sections: list[str] = []
    if status_reason:
        sections.append(f"Why the previous attempt stopped: {status_reason}")
    pipeline = evidence.get("pipeline")
    if isinstance(pipeline, dict) and pipeline:
        sections.append(
            "Last pipeline: {status} (sha {sha}) — {url}".format(
                status=pipeline.get("status"),
                sha=str(pipeline.get("sha") or "")[:8],
                url=pipeline.get("url"),
            )
        )
    review = evidence.get("review")
    if isinstance(review, dict) and review:
        sections.append(f"Last review verdict: {review.get('verdict')} — {review.get('summary')}")
        for finding in (review.get("findings") or [])[:5]:
            sections.append(
                "- {severity}: {file}: {note}".format(
                    severity=finding.get("severity"),
                    file=finding.get("file"),
                    note=finding.get("note"),
                )
            )
    redacted, _ = EvidencePolicy.from_settings(settings).apply_policy("\n\n".join(sections))
    return redacted[-REPAIR_CONTEXT_MAX_CHARS:]
