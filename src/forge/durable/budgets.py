"""Run budgets: reserve before dispatch, reconcile against actuals (F22).

Implements ADR-0018 §5 on top of the ADR-0013 rule *reserve, then reconcile*:
a dispatch reserves calls/tokens BEFORE the provider is contacted, and the
provider receipt (or the harness candidate's usage meta) reconciles the hold
against actuals afterwards. A refusal means the provider is never contacted —
:class:`forge.factory.llm.LLMClient` turns it into
``LLMError("budget_exhausted")``.

Every counter move is a single conditional UPDATE against the ``run_budgets``
row — the current counters are read inside the UPDATE predicate, never from a
previously loaded ORM object — so a burst of parallel attempts cannot
overshoot a limit between measurement points (no read-modify-write races,
portable across SQLite and Postgres).

A reservation is granted only while the budget's *exposure* — known spend,
outstanding holds, unresolved liability — can absorb the request:
``consumed + unresolved + reserved + requested <= limit``. Counting known
spend alone would ignore what is already committed and re-lend it.

Unknown usage is never counted as zero: an unknown token figure leaves the
consumed counters untouched (never a fabricated figure), and the completeness
fact travels on the ``llm_calls`` ledger row the caller writes. The hold that
covered the dispatch is not silently dropped either — it moves to the
``unresolved_*`` liability counters, which the exposure predicate counts, so
a receipt the provider never costed cannot open hard-budget capacity as if it
had been free. A refusal to grant a reservation marks the budget
``exhausted`` — a budget that cannot satisfy a reservation must not keep
accepting work.

Wiring (ADR-0018 §5): RunService opens the budget when the RunSpec carries
limits — :func:`open_budget_from_spec` in the same session that freezes the
spec at plan acceptance — and passes a :class:`BudgetGuard` (built with
:func:`load_budget_guard`) to the planner/implementer/reviewer ``LLMClient``
as its optional ``budget`` handle. The harness path reconciles the candidate
meta usage receipt with :func:`reconcile_harness_receipt`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.controller import RunNotFound
from forge.durable.models import BudgetReservation, FlowRun, RunBudget

logger = logging.getLogger(__name__)

#: The refusal reason surfaced to callers (LLMClient raises
#: ``LLMError("budget_exhausted")`` without contacting the provider).
BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class BudgetLimits:
    """The three F22 budget dimensions; ``None`` = unlimited on that axis."""

    wallclock_s: int | None = None
    max_calls: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class Reservation:
    """Value receipt of one granted hold — the reconcile-time identity.

    Carries everything :func:`reconcile_actual` needs, so it survives the
    session that granted it (the guard commits between reserve and reconcile).
    """

    id: str
    run_budget_id: str
    attempt_id: str | None
    reserved_calls: int
    reserved_tokens: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _known_token_total(usage: Any) -> int | None:
    """Sum the KNOWN token parts of a usage receipt (input + output).

    ``input_tokens`` is the inclusive figure (cached tokens ride inside it —
    ADR-0013 normalization), so ``cached_input_tokens`` is never added on top.
    A part that is not a non-negative int is not a figure: it is ignored, so a
    negative count cannot subtract its way into budget capacity. Nothing known
    → ``None`` (unknown stays unknown, never zero).
    """
    parts = (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )
    known = [
        part
        for part in parts
        if isinstance(part, int) and not isinstance(part, bool) and part >= 0
    ]
    if not known:
        return None
    return sum(known)


def _limit_or_none(value: object) -> int | None:
    """A usable positive budget limit, or ``None`` (absent/invalid = unset)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _exposure(consumed: Any, unresolved: Any, reserved: Any, requested: Any = 0) -> Any:
    """The quantity a limit has to absorb: known spend + liability + holds.

    Arguments are either ``run_budgets`` columns (the predicate reads the row
    live) or plain ints; ``requested`` is the size being granted on top.
    """
    return consumed + unresolved + reserved + requested


def _require_non_negative(name: str, value: int) -> None:
    """Reject a non-int/negative counter move before it reaches the row.

    A negative hold or a negative actual would *mint* budget capacity (the
    UPDATE arithmetic would move the counters backwards), so this is a caller
    bug, not a budget state — it raises instead of refusing the reservation
    (a refusal would durably exhaust an innocent budget).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"budget {name} must be a non-negative int, got {value!r}")


def budget_limits_from_spec(spec_document: object) -> BudgetLimits | None:
    """Extract F22 budget limits from a RunSpec document's ``budgets`` block.

    Returns ``None`` when the spec carries none of the enforceable dimensions
    (``wallclock_s`` / ``max_calls`` / ``max_tokens``) — the run is then
    unlimited on those axes and no budget row is needed. Lifecycle limits that
    already live in the block (``commit_cycles``, ``harness_timeout``) are not
    budget dimensions and are ignored here.
    """
    budgets = spec_document.get("budgets") if isinstance(spec_document, dict) else None
    if not isinstance(budgets, dict):
        return None
    limits = BudgetLimits(
        wallclock_s=_limit_or_none(budgets.get("wallclock_s")),
        max_calls=_limit_or_none(budgets.get("max_calls")),
        max_tokens=_limit_or_none(budgets.get("max_tokens")),
    )
    if limits == BudgetLimits():
        return None
    return limits


async def budget_for_run(session: AsyncSession, run_id: str) -> RunBudget | None:
    """The run's budget row, whatever its status, or ``None``."""
    return (
        (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
        .scalars()
        .first()
    )


async def open_budget(
    session: AsyncSession,
    *,
    run_id: str,
    spec_digest: str | None = None,
    wallclock_s: int | None = None,
    max_calls: int | None = None,
    max_tokens: int | None = None,
) -> RunBudget:
    """Open the run's budget — idempotent per run (UNIQUE ``run_id``).

    Re-opening an already-budgeted run returns the existing row unchanged:
    limits are frozen at open time and mid-run mutations would silently move
    an approved budget. A concurrent opener that lost the INSERT race is
    folded onto the winner's row (SAVEPOINT rollback, outer transaction
    intact).
    """
    if await session.get(FlowRun, run_id) is None:
        raise RunNotFound(f"flow run {run_id!r} not found")
    existing = await budget_for_run(session, run_id)
    if existing is not None:
        return existing
    budget = RunBudget(
        run_id=run_id,
        spec_digest=spec_digest,
        wallclock_s=wallclock_s,
        max_calls=max_calls,
        max_tokens=max_tokens,
    )
    session.add(budget)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        existing = await budget_for_run(session, run_id)
        if existing is None:
            raise
        return existing
    return budget


async def open_budget_from_spec(
    session: AsyncSession,
    *,
    run_id: str,
    spec_document: object,
    spec_digest: str | None = None,
) -> RunBudget | None:
    """Open the run's budget when its RunSpec carries budget limits.

    The plan-acceptance hook (ADR-0018 §5): call in the same session that
    freezes the RunSpec. Returns ``None`` (and opens nothing) when the spec
    declares no F22 budget dimensions.
    """
    limits = budget_limits_from_spec(spec_document)
    if limits is None:
        return None
    return await open_budget(
        session,
        run_id=run_id,
        spec_digest=spec_digest,
        wallclock_s=limits.wallclock_s,
        max_calls=limits.max_calls,
        max_tokens=limits.max_tokens,
    )


async def reserve(
    session: AsyncSession,
    budget: RunBudget,
    *,
    calls: int = 1,
    tokens: int = 0,
    attempt_id: str | None = None,
) -> Reservation | None:
    """Reserve calls/tokens before a dispatch; ``None`` = refused.

    One atomic conditional UPDATE: the grant happens only when the budget is
    still ``open`` AND both (possibly unlimited) dimensions can absorb the
    request on top of their current *exposure* — consumed actuals plus
    outstanding holds plus unresolved liability — with the counters read
    inside the UPDATE predicate, so *budget*'s in-memory snapshot is never
    trusted. A refused reservation marks an ``open`` budget ``exhausted``
    (it cannot serve the standard reservation shape; ``closed``/``exhausted``
    budgets just refuse).

    The grant is audited as a ``budget_reservations`` row whose ``released``
    flag is what makes :func:`reconcile_actual` exactly-once.

    Raises :class:`ValueError` on a negative request: that would mint
    capacity, and the budget is not at fault.
    """
    _require_non_negative("calls", calls)
    _require_non_negative("tokens", tokens)
    granted = await session.execute(
        update(RunBudget)
        .where(
            RunBudget.id == budget.id,
            RunBudget.status == "open",
            or_(
                RunBudget.max_calls.is_(None),
                _exposure(
                    RunBudget.consumed_calls,
                    RunBudget.unresolved_calls,
                    RunBudget.reserved_calls,
                    calls,
                )
                <= RunBudget.max_calls,
            ),
            or_(
                RunBudget.max_tokens.is_(None),
                _exposure(
                    RunBudget.consumed_tokens,
                    RunBudget.unresolved_tokens,
                    RunBudget.reserved_tokens,
                    tokens,
                )
                <= RunBudget.max_tokens,
            ),
        )
        .values(
            reserved_calls=RunBudget.reserved_calls + calls,
            reserved_tokens=RunBudget.reserved_tokens + tokens,
            updated_at=_utcnow(),
        )
    )
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    if granted.rowcount != 1:  # type: ignore[attr-defined]
        # Refused: either already not open (nothing to do), or the limits
        # cannot absorb the request — durably mark the budget exhausted.
        await session.execute(
            update(RunBudget)
            .where(RunBudget.id == budget.id, RunBudget.status == "open")
            .values(status="exhausted", updated_at=_utcnow())
        )
        await session.flush()
        return None

    row = BudgetReservation(
        run_budget_id=budget.id,
        attempt_id=attempt_id,
        reserved_calls=calls,
        reserved_tokens=tokens,
    )
    session.add(row)
    await session.flush()
    return Reservation(
        id=row.id,
        run_budget_id=row.run_budget_id,
        attempt_id=row.attempt_id,
        reserved_calls=row.reserved_calls,
        reserved_tokens=row.reserved_tokens,
    )


async def reconcile_actual(
    session: AsyncSession,
    reservation: Reservation,
    *,
    actual_calls: int | None = None,
    actual_tokens: int | None = None,
) -> RunBudget | None:
    """Settle a reservation against the provider's actual usage.

    Releases the hold (reserved −) and records the actuals (consumed +) in
    one conditional UPDATE. A known actual is recorded in full even when it
    overshoots its hold (the under-reservation policy: actuals are never
    clipped to the estimate). An unknown dimension adds nothing to consumed —
    never zero (ADR-0013) — but its hold is re-booked as *unresolved
    liability*: the dispatch agreed to that bound, so it keeps occupying
    budget capacity instead of opening it up as zero spend.

    The budget is marked ``exhausted`` when exposure reaches or overshoots a
    limit (nothing provably remains to grant). Exactly-once via the
    reservation's ``released`` flip: a re-run after a crash cannot
    double-apply the move, and the hold decrement never drives a counter
    negative.

    Raises :class:`ValueError` on a negative actual: that would mint
    capacity, and the budget is not at fault.
    """
    if actual_calls is not None:
        _require_non_negative("actual_calls", actual_calls)
    if actual_tokens is not None:
        _require_non_negative("actual_tokens", actual_tokens)
    flip = await session.execute(
        update(BudgetReservation)
        .where(BudgetReservation.id == reservation.id, BudgetReservation.released.is_(False))
        .values(released=True)
    )
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    if flip.rowcount != 1:  # type: ignore[attr-defined]
        # Already reconciled (crash-retry) — the counters were moved once.
        return await session.get(RunBudget, reservation.run_budget_id)

    # An unknown actual keeps its hold's worth of liability: the estimate was
    # the only bound the dispatch ever agreed to, so releasing it outright
    # would spend the budget's accounting on a guess of zero.
    unresolved_calls = reservation.reserved_calls if actual_calls is None else 0
    unresolved_tokens = reservation.reserved_tokens if actual_tokens is None else 0
    await session.execute(
        update(RunBudget)
        .where(RunBudget.id == reservation.run_budget_id)
        .values(
            # Never let a hold drive a counter negative (defensive: holds are
            # only ever granted against the live row).
            reserved_calls=case(
                (
                    RunBudget.reserved_calls >= reservation.reserved_calls,
                    RunBudget.reserved_calls - reservation.reserved_calls,
                ),
                else_=0,
            ),
            reserved_tokens=case(
                (
                    RunBudget.reserved_tokens >= reservation.reserved_tokens,
                    RunBudget.reserved_tokens - reservation.reserved_tokens,
                ),
                else_=0,
            ),
            unresolved_calls=RunBudget.unresolved_calls + unresolved_calls,
            unresolved_tokens=RunBudget.unresolved_tokens + unresolved_tokens,
            consumed_calls=RunBudget.consumed_calls + (actual_calls or 0),
            consumed_tokens=RunBudget.consumed_tokens + (actual_tokens or 0),
            updated_at=_utcnow(),
        )
    )
    return await _exhaust_if_over(session, reservation.run_budget_id)


async def close_budget(session: AsyncSession, budget: RunBudget) -> bool:
    """Close the budget (terminal; refuses all further reservations).

    Actuals recorded after a close are ignored — the run is finished. Returns
    ``False`` when the budget was already closed.
    """
    result = await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget.id, RunBudget.status != "closed")
        .values(status="closed", updated_at=_utcnow())
    )
    await session.flush()
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _exhaust_if_over(session: AsyncSession, budget_id: str) -> RunBudget | None:
    """Mark the budget ``exhausted`` when exposure reaches a limit.

    A fully-spent budget is exhausted: nothing remains for any further
    actual, so the status flips the moment exposure — consumed actuals plus
    unresolved liability — hits the limit (or overshoots it, when a hold
    under-reserved the call). A liability never releases, so it counts toward
    the limit exactly like spend does.
    """
    await session.execute(
        update(RunBudget)
        .where(
            RunBudget.id == budget_id,
            RunBudget.status == "open",
            or_(
                and_(
                    RunBudget.max_calls.is_not(None),
                    _exposure(RunBudget.consumed_calls, RunBudget.unresolved_calls, 0)
                    >= RunBudget.max_calls,
                ),
                and_(
                    RunBudget.max_tokens.is_not(None),
                    _exposure(RunBudget.consumed_tokens, RunBudget.unresolved_tokens, 0)
                    >= RunBudget.max_tokens,
                ),
            ),
        )
        .values(status="exhausted", updated_at=_utcnow())
    )
    await session.flush()
    return await session.get(RunBudget, budget_id)


async def _exhaust_unbounded_receipt(session: AsyncSession, budget_id: str) -> RunBudget | None:
    """Exhaust a hard token budget that received an uncosted receipt.

    No hold and no known tokens: the receipt's spend has no upper bound, so
    no remaining token headroom is provable. Counters stay untouched (no
    figure is fabricated); the status is what refuses further work.
    """
    await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget_id, RunBudget.status == "open")
        .values(status="exhausted", updated_at=_utcnow())
    )
    await session.flush()
    return await session.get(RunBudget, budget_id)


async def reconcile_harness_receipt(
    session: AsyncSession,
    run_id: str,
    usage: Any,
) -> RunBudget | None:
    """Reconcile a harness candidate's usage receipt against the run budget.

    The F22 harness path: the receipt parsed from ``candidate.meta.json``
    counts as exactly one call whose tokens are the reported input+output
    (cached rides inside the inclusive input — never added on top). Unknown
    tokens leave the token counter untouched (unknown ≠ zero); the
    completeness fact rides the ``llm_calls`` ledger row the caller writes.

    The harness path holds no reservation, so an unknown token figure has no
    estimate to book as liability — the spend is unbounded. On a hard token
    budget that makes headroom unprovable, and a budget that cannot prove
    headroom must not keep accepting work: it is marked ``exhausted`` rather
    than left open as if the receipt had been free.

    Actuals are recorded on ``exhausted`` budgets too; only a ``closed``
    budget ignores receipts. Returns the refreshed budget, or ``None`` when
    the run has none (or it is closed).
    """
    budget = await budget_for_run(session, run_id)
    if budget is None or budget.status == "closed":
        return None
    tokens = _known_token_total(usage)
    recorded = await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget.id, RunBudget.status != "closed")
        .values(
            consumed_calls=RunBudget.consumed_calls + 1,
            consumed_tokens=RunBudget.consumed_tokens + (tokens or 0),
            updated_at=_utcnow(),
        )
    )
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    if recorded.rowcount != 1:  # type: ignore[attr-defined]
        return None
    if tokens is None and budget.max_tokens is not None:
        # Unbounded receipt on a hard token budget: no headroom is provable.
        return await _exhaust_unbounded_receipt(session, budget.id)
    return await _exhaust_if_over(session, budget.id)


class BudgetGuard:
    """The per-run handle an ``LLMClient`` uses for reserve/reconcile.

    Owns short-lived sessions: the reservation is committed BEFORE the
    provider is contacted and the reconciliation committed AFTER the response,
    so the hold is visible to every other attempt in between. One guard per
    run budget; ``attempt_id`` stamps every audit row it creates.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        budget_id: str,
        *,
        attempt_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.budget_id = budget_id
        self._attempt_id = attempt_id

    async def reserve(self, *, calls: int = 1, tokens: int = 0) -> Reservation | None:
        """Commit a pre-dispatch hold, or ``None`` when refused."""
        async with self._session_factory() as session:
            budget = await session.get(RunBudget, self.budget_id)
            if budget is None:
                return None
            reservation = await reserve(
                session, budget, calls=calls, tokens=tokens, attempt_id=self._attempt_id
            )
            await session.commit()
            return reservation

    async def reconcile(
        self,
        reservation: Reservation,
        *,
        actual_calls: int | None = None,
        actual_tokens: int | None = None,
    ) -> None:
        """Commit the post-response settlement of a hold."""
        async with self._session_factory() as session:
            await reconcile_actual(
                session,
                reservation,
                actual_calls=actual_calls,
                actual_tokens=actual_tokens,
            )
            await session.commit()

    async def refresh(self) -> RunBudget | None:
        """The current budget row (status/counters), or ``None`` if gone."""
        async with self._session_factory() as session:
            return await session.get(RunBudget, self.budget_id)


async def load_budget_guard(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    attempt_id: str | None = None,
) -> BudgetGuard | None:
    """The run's enforcement handle, or ``None`` when nothing is budgeted.

    The constructor hook RunService passes to the agents' ``LLMClient``:
    ``LLMClient(settings, session_factory, budget=guard)``. A closed budget
    yields no guard — the run is finished and no dispatch will happen.
    """
    async with session_factory() as session:
        budget = await budget_for_run(session, run_id)
        if budget is None or budget.status == "closed":
            return None
        return BudgetGuard(session_factory, budget.id, attempt_id=attempt_id)
