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

Exposure is ``consumed + reserved + unresolved``: a reservation is granted
only when that total plus the request still fits the limit, so budget that
was already spent is never grantable again. Unknown usage is never counted
as zero *and* never released as spendable capacity: an unknown dimension
parks its hold in the ``unresolved_*`` counters (the completeness fact still
travels on the ``llm_calls`` ledger row the caller writes), and an unknown
receipt with no hold to fence it — the harness path — stops a hard-limited
budget instead of reading as spendable headroom. A refusal to grant a
reservation marks the budget ``exhausted`` — a budget that cannot satisfy a
reservation must not keep accepting work.

Wiring (ADR-0018 §5, R13): RunService opens the budget when the RunSpec
carries limits — :func:`open_budget_from_spec` in the same session that
freezes the spec at plan acceptance (the standard path opens it BEFORE the
first paid call, from the budget class's numeric profile resolved at freeze
time, so the planner itself reserves) — and passes a :class:`BudgetGuard`
(built with :func:`load_budget_guard`) to the planner/implementer/reviewer
``LLMClient`` as its optional ``budget`` handle. The harness path reconciles
the candidate meta usage receipt with :func:`reconcile_harness_receipt`
(keyed by episode so repeated artifact polls cannot double-consume), and
R23 adds the identity-arbitrated front door:
:func:`ingest_usage_receipt` inserts the receipt's ``(run, attempt,
receipt_id)`` identity into ``usage_receipts`` with ``ON CONFLICT DO
NOTHING`` first, so the ledger row and the budget move exactly once per
distinct receipt no matter how often the artifact is re-read. Its episode
dispatches are gated by :func:`budget_block_reason` — wall clock and
episode count are the only axes a non-intercepted lane can honestly enforce.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.controller import RunNotFound, as_aware_utc
from forge.durable.models import BudgetReservation, FlowRun, LLMCall, RunBudget, UsageReceipt

logger = logging.getLogger(__name__)

#: The refusal reason surfaced to callers (LLMClient raises
#: ``LLMError("budget_exhausted")`` without contacting the provider).
BUDGET_EXHAUSTED = "budget_exhausted"

#: The budget profile class every unknown budget class degrades to (R13):
#: the same fallback rule as the harness selection's ``budget_class``.
DEFAULT_BUDGET_CLASS = "standard"


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
    """Sum the KNOWN token parts of a usage receipt (R23 normalization).

    Prefers the receipt's own shape-aware total
    (``HarnessUsage.total_known_tokens``): Anthropic(-compatible) counters
    are DISJOINT — ``input_tokens`` already excludes the cache — so their
    spend total is ``input + cache_read + cache_write + output``;
    OpenAI-compatible counters are INCLUSIVE — the cache rides inside the
    input and is never added on top (ADR-0013). Objects without the
    property keep the legacy inclusive reading (input + output). Unknown
    parts contribute nothing (never zero-filled); nothing known → ``None``.
    """
    no_total: Any = object()  # sentinel: "the attribute is absent"
    shape_aware = getattr(usage, "total_known_tokens", no_total)
    if (
        shape_aware is not no_total
        and not isinstance(shape_aware, bool)
        and (shape_aware is None or isinstance(shape_aware, int))
    ):
        return shape_aware
    parts = (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )
    known = [part for part in parts if isinstance(part, int) and not isinstance(part, bool)]
    if not known:
        return None
    return sum(known)


def _limit_or_none(value: object) -> int | None:
    """A usable positive budget limit, or ``None`` (absent/invalid = unset)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _usage_amount(value: object, name: str) -> int:
    """A whole, non-negative usage amount; anything else is a caller bug.

    Every counter move is an ``counter = counter + delta`` UPDATE, so a
    negative or non-integer delta would *create* budget instead of spending
    it — the amounts are validated here, at the only entry points that move
    them, before any write happens.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"budget {name} must be a non-negative int, got {value!r}")
    return value


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


def resolve_budget_limits(
    profiles: dict[str, dict[str, Any]], budget_class: str
) -> BudgetLimits | None:
    """Resolve a budget class's numeric ceilings from the configured profiles
    (R13): the profile *name* the run's frozen ``budget_class`` points at,
    with unknown names degrading to the ``"standard"`` profile — never to a
    guess. ``None`` when nothing usable is configured or every axis of the
    resolved profile is unset: the run is then unlimited and no budget row is
    opened. The caller freezes the result into the RunSpec at plan acceptance,
    so a later config change can never move an approved budget.
    """
    profile = profiles.get(budget_class) or profiles.get(DEFAULT_BUDGET_CLASS)
    if not isinstance(profile, dict):
        return None
    limits = BudgetLimits(
        wallclock_s=_limit_or_none(profile.get("wallclock_s")),
        max_calls=_limit_or_none(profile.get("max_calls")),
        max_tokens=_limit_or_none(profile.get("max_tokens")),
    )
    if limits == BudgetLimits():
        return None
    return limits


def wallclock_deadline(budget: RunBudget) -> datetime | None:
    """The budget's absolute wall-clock deadline, or ``None`` when unlimited.

    Anchored at the budget row's creation (run start — plan acceptance), so
    the deadline is durable and every later check (reservation refusals,
    episode gates, reconciler polls) reads the SAME clock.
    """
    seconds = _limit_or_none(budget.wallclock_s)
    if seconds is None or budget.created_at is None:
        return None
    return as_aware_utc(budget.created_at) + timedelta(seconds=seconds)


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
    declares no F22 budget dimensions. R13: the row may already exist — the
    standard path opens it BEFORE the first paid call so the planner itself
    reserves — in which case the frozen limits stand and only the missing
    ``spec_digest`` provenance is backfilled (limits are never moved).
    """
    limits = budget_limits_from_spec(spec_document)
    if limits is None:
        return None
    budget = await open_budget(
        session,
        run_id=run_id,
        spec_digest=spec_digest,
        wallclock_s=limits.wallclock_s,
        max_calls=limits.max_calls,
        max_tokens=limits.max_tokens,
    )
    if spec_digest is not None and not budget.spec_digest:
        await session.execute(
            update(RunBudget)
            .where(RunBudget.id == budget.id, RunBudget.spec_digest.is_(None))
            .values(spec_digest=spec_digest)
        )
        await session.flush()
    return budget


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
    request on top of the recorded exposure ``consumed + reserved +
    unresolved`` — the counters in the predicate are the row's CURRENT
    values, so *budget*'s in-memory snapshot is never trusted, and budget
    already spent (or parked as unresolved liability) is never granted
    again. A refused reservation marks an ``open`` budget ``exhausted`` (it
    cannot serve the standard reservation shape; ``closed``/``exhausted``
    budgets just refuse).

    R13 wall clock: a budget whose ``created_at + wallclock_s`` deadline has
    passed refuses every reservation too — and is durably exhausted, so the
    refusal is permanent and visible. This is what makes ``wallclock_s``
    *enforced* on interception lanes (every model dispatch passes through
    here before the provider is contacted), not merely recorded.

    Amounts must be whole non-negative ints and a hold must cover at least
    one call — every dispatch consumes one, so a zero-call hold
    under-reserves it by policy — otherwise ``ValueError`` is raised before
    any write happens.

    The grant is audited as a ``budget_reservations`` row whose ``released``
    flag is what makes :func:`reconcile_actual` exactly-once.
    """
    calls = _usage_amount(calls, "calls")
    tokens = _usage_amount(tokens, "tokens")
    if calls < 1:
        raise ValueError("budget calls must reserve at least one call")
    deadline = wallclock_deadline(budget)
    if deadline is not None and _utcnow() > deadline:
        # Past the wall clock nothing is grantable any more — exhaust
        # durably so the stop is visible in every later read.
        await _exhaust_open(session, budget.id)
        return None
    granted = await session.execute(
        update(RunBudget)
        .where(
            RunBudget.id == budget.id,
            RunBudget.status == "open",
            or_(
                RunBudget.max_calls.is_(None),
                RunBudget.consumed_calls
                + RunBudget.reserved_calls
                + RunBudget.unresolved_calls
                + calls
                <= RunBudget.max_calls,
            ),
            or_(
                RunBudget.max_tokens.is_(None),
                RunBudget.consumed_tokens
                + RunBudget.reserved_tokens
                + RunBudget.unresolved_tokens
                + tokens
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
        await _exhaust_open(session, budget.id)
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

    Releases the hold (reserved −) and records the known actuals (consumed
    +) in one conditional UPDATE; an unknown dimension is never counted as
    zero (ADR-0013) — its hold moves into the ``unresolved_*`` counters
    instead, so exposure ``consumed + reserved + unresolved`` is invariant
    and an unknown receipt cannot open hard-budget capacity as zero spend.
    An under-reservation still records the full actuals (the caller breached
    the reserve-enough policy), and the budget is marked ``exhausted`` when
    the recorded spend reaches or overshoots a limit (a fully-spent budget
    has nothing left to grant). Exactly-once via the reservation's
    ``released`` flip: a re-run after a crash cannot double-apply the move.
    """
    if actual_calls is not None:
        actual_calls = _usage_amount(actual_calls, "actual_calls")
    if actual_tokens is not None:
        actual_tokens = _usage_amount(actual_tokens, "actual_tokens")
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

    # The hold of an unknown dimension becomes unresolved liability — the
    # same capacity that was fenced for the dispatch stays fenced.
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
            consumed_calls=RunBudget.consumed_calls + (actual_calls or 0),
            consumed_tokens=RunBudget.consumed_tokens + (actual_tokens or 0),
            unresolved_calls=RunBudget.unresolved_calls + unresolved_calls,
            unresolved_tokens=RunBudget.unresolved_tokens + unresolved_tokens,
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


async def budget_block_reason(
    session: AsyncSession,
    run_id: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Why a NEW dispatch/episode must not start against this run, or ``None``.

    The dispatch-time gate for lanes whose work does not flow through
    :class:`BudgetGuard` reservations — the harness lanes (R13): their model
    calls happen inside a CI job forge cannot intercept, so the *dispatch*
    is the enforcement point. An ``exhausted`` budget blocks every new
    episode, and a wall clock past its deadline is durably exhausted first so
    the stop is visible. ``None`` for runs without a budget (unlimited) and
    for ``closed`` budgets (terminal-run bookkeeping — the same posture as
    :func:`load_budget_guard`, which hands out no guard for a finished run).
    """
    budget = await budget_for_run(session, run_id)
    if budget is None or budget.status == "closed":
        return None
    if budget.status == "exhausted":
        return f"{BUDGET_EXHAUSTED}: run budget is exhausted — no further dispatch"
    deadline = wallclock_deadline(budget)
    if deadline is not None and as_aware_utc(now or _utcnow()) > deadline:
        await _exhaust_open(session, budget.id)
        return (
            f"{BUDGET_EXHAUSTED}: wall clock budget of {budget.wallclock_s}s "
            "is spent — no further dispatch"
        )
    return None


def _receipt_claim_id(budget_id: str, dedupe_key: str) -> str:
    """The deterministic primary key of a receipt's dedupe claim row."""
    return hashlib.sha256(f"{budget_id}:{dedupe_key}".encode("utf-8")).hexdigest()[:32]


async def _claim_receipt_key(session: AsyncSession, budget_id: str, dedupe_key: str) -> bool:
    """Claim an idempotency key for a harness receipt; ``False`` = seen before.

    The claim is an INSERT of a marker row into ``budget_reservations`` whose
    PRIMARY KEY is derived from (budget, key) — a concurrent or repeated
    reconcile of the same episode loses the insert race (SAVEPOINT rollback,
    outer transaction intact) and is told so, which is what makes a repeated
    artifact poll a no-op instead of a second consumption. The marker is
    added INSIDE the savepoint: on a lost race its rollback expunges the
    pending row, leaving the outer transaction flushable. The marker shape
    (0/0 hold, ``released=True``) never comes from :func:`reserve`, which
    always holds at least one call and starts unreleased.
    """
    try:
        async with session.begin_nested():
            session.add(
                BudgetReservation(
                    id=_receipt_claim_id(budget_id, dedupe_key),
                    run_budget_id=budget_id,
                    attempt_id=(f"harness-receipt:{dedupe_key}"[:100] if dedupe_key else None),
                    reserved_calls=0,
                    reserved_tokens=0,
                    released=True,
                )
            )
            await session.flush()
    except IntegrityError:
        return False
    return True


async def _exhaust_open(session: AsyncSession, budget_id: str) -> None:
    """Durably flip an ``open`` budget to ``exhausted`` (any other status stands).

    The no-extra-conditions form, used where the budget has proven it cannot
    serve work: a refused reservation, and a hard-limited axis whose usage
    arrived unverifiable and has no hold to fence it.
    """
    await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget_id, RunBudget.status == "open")
        .values(status="exhausted", updated_at=_utcnow())
    )
    await session.flush()


async def _exhaust_if_over(session: AsyncSession, budget_id: str) -> RunBudget | None:
    """Mark the budget ``exhausted`` when recorded spend reaches a limit.

    Recorded spend is exposure minus the in-flight holds: consumed actuals
    plus unresolved liability. A fully-spent budget is exhausted — nothing
    remains for any further actual, so the status flips the moment spend
    hits the limit (or overshoots it, when a hold under-reserved the call).
    """
    calls_spent = RunBudget.consumed_calls + RunBudget.unresolved_calls
    tokens_spent = RunBudget.consumed_tokens + RunBudget.unresolved_tokens
    await session.execute(
        update(RunBudget)
        .where(
            RunBudget.id == budget_id,
            RunBudget.status == "open",
            or_(
                and_(RunBudget.max_calls.is_not(None), calls_spent >= RunBudget.max_calls),
                and_(RunBudget.max_tokens.is_not(None), tokens_spent >= RunBudget.max_tokens),
            ),
        )
        .values(status="exhausted", updated_at=_utcnow())
    )
    await session.flush()
    return await session.get(RunBudget, budget_id)


async def reconcile_harness_receipt(
    session: AsyncSession,
    run_id: str,
    usage: Any,
    *,
    dedupe_key: str | None = None,
) -> RunBudget | None:
    """Reconcile a harness candidate's usage receipt against the run budget.

    The F22 harness path: the receipt parsed from ``candidate.meta.json``
    counts as exactly one call whose tokens are the reported input+output
    (cached rides inside the inclusive input — never added on top). Unknown
    tokens leave the token counter untouched (unknown ≠ zero) — and, on a
    hard token limit, stop the budget: this path holds no reservation, so
    there is no liability figure to park and unknown must not read as
    spendable headroom. The completeness fact still rides the ``llm_calls``
    ledger row the caller writes. Actuals are recorded on ``exhausted``
    budgets too; only a ``closed`` budget ignores receipts. Returns the
    refreshed budget, or ``None`` when the run has none (or it is closed).

    R13 idempotency: *dedupe_key* names the artifact's episode (e.g. the
    harness pipeline id) — a repeated poll of the SAME episode claims the
    same key and is a no-op, so a reconciler crash-retry or re-delivered
    artifact cannot double-consume. ``None`` keeps the legacy count-always
    behavior for callers with no episode identity.
    """
    budget = await budget_for_run(session, run_id)
    if budget is None or budget.status == "closed":
        return None
    if dedupe_key is not None and not await _claim_receipt_key(session, budget.id, dedupe_key):
        return budget
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
        # Limits are frozen at open time, so the loaded row's limit is live.
        await _exhaust_open(session, budget.id)
    return await _exhaust_if_over(session, budget.id)


async def ingest_usage_receipt(
    session: AsyncSession,
    *,
    run_id: str,
    usage: Any,
    attempt_id: str = "",
    model_fallback: str = "unknown",
    record_ledger_call: bool = True,
) -> tuple[RunBudget | None, bool]:
    """Ingest one harness usage receipt EXACTLY ONCE (R23).

    The control-plane entry point for candidate-artifact spend: the
    receipt's identity — ``receipt_id`` over (run, attempt, normalized
    usage JSON), computed by the lane or recomputed identically here — is
    inserted into ``usage_receipts`` with ``ON CONFLICT DO NOTHING`` against
    the UNIQUE ``(run_id, attempt_id, receipt_id)`` index, so a repeated
    reconciler poll, a crash-retry, or a re-downloaded artifact replays the
    same identity and is a no-op at the DB, while a repair re-dispatch (a
    new attempt id) legitimately costs again.

    On the FIRST ingest of an identity this also writes the ``llm_calls``
    ledger row (unknown counters stay NULL — never zero) and reconciles the
    run budget through :func:`reconcile_harness_receipt` (exactly one call;
    an unknown receipt stops a hard token-limited budget instead of reading
    as spendable headroom). A missing/invalid receipt is still recorded —
    with ``completeness='unknown'`` and NULL counters — so the attempt's
    cost shows up in the unknown bucket, never silently as zero. The
    receipt row lands even when the run has no budget or a closed one: the
    ledger is honest, only the counters stand still.

    ``record_ledger_call=False`` leaves the ``llm_calls`` write to the
    caller (the GitLab lane records every delivery honestly and dedupes the
    budget through its own episode claim — the receipt table adds the
    identity dimension without changing that pinned shape).

    Returns ``(refreshed budget or None, created)`` — ``created=False``
    means the identity was ingested before and NOTHING moved.
    """
    from forge.runs.candidate import usage_receipt_id

    # The deferred import keeps forge.durable → forge.runs.candidate out of
    # module level: runs.candidate → factory.implementer → factory.llm →
    # forge.durable.budgets would close a cycle at import time.
    attempt = str(getattr(usage, "attempt_id", "") or attempt_id or "")[:100]
    receipt_id = str(getattr(usage, "receipt_id", "") or "") or usage_receipt_id(
        run_id, attempt, usage
    )

    def _int(name: str) -> int | None:
        value = getattr(usage, name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    raw = getattr(usage, "raw", None)
    inserted = await session.execute(
        pg_insert(UsageReceipt)
        .values(
            run_id=run_id,
            attempt_id=attempt,
            receipt_id=receipt_id,
            driver=str(getattr(usage, "driver", "") or "") or None,
            model=str(getattr(usage, "model", "") or "") or None,
            input_tokens=_int("input_tokens"),
            cached_input_tokens=_int("cached_input_tokens"),
            cache_write_tokens=_int("cache_write_tokens"),
            output_tokens=_int("output_tokens"),
            completeness=str(getattr(usage, "completeness", "") or "unknown"),
            source=str(getattr(usage, "source", "") or "") or None,
            raw=raw if isinstance(raw, dict) else None,
        )
        # ON CONFLICT DO NOTHING works identically on Postgres and SQLite
        # (tests) — the same portable arbiter as the webhook inbox.
        .on_conflict_do_nothing(
            index_elements=[UsageReceipt.run_id, UsageReceipt.attempt_id, UsageReceipt.receipt_id]
        )
    )
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    if inserted.rowcount != 1:  # type: ignore[attr-defined]
        return await budget_for_run(session, run_id), False

    if record_ledger_call:
        session.add(
            LLMCall(
                flow_run_id=run_id,
                role="implementer",
                provider="ci_harness",
                model=str(getattr(usage, "model", "") or "") or model_fallback,
                status="ok",
                input_tokens=_int("input_tokens"),
                output_tokens=_int("output_tokens"),
                cached_tokens=_int("cached_input_tokens"),
                driver=str(getattr(usage, "driver", "") or "") or None,
                completeness=str(getattr(usage, "completeness", "") or "unknown"),
            )
        )
        await session.flush()
    budget = await reconcile_harness_receipt(session, run_id, usage)
    return budget, True


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
