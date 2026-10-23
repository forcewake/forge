"""Durable budget reservations, exposure and idempotent usage receipts."""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.controller import RunNotFound, as_aware_utc
from forge.durable.models import (
    BudgetAmendment,
    BudgetReservation,
    FlowRun,
    LLMCall,
    RunBudget,
    UsageReceipt,
)

logger = logging.getLogger(__name__)

#: The refusal reason surfaced to callers (LLMClient raises ``LLMError("budget_exhausted")``
#: without contacting the provider).
BUDGET_EXHAUSTED = "budget_exhausted"

#: The budget profile class every unknown budget class degrades to: the same fallback rule
#: as the harness selection's ``budget_class``.
DEFAULT_BUDGET_CLASS = "standard"

#: the DISTINCT budget dimensions.
AXIS_USD = "usd"
AXIS_CALLS = "calls"
AXIS_TOKENS = "tokens"
AXIS_WALLCLOCK = "wallclock"
BUDGET_AXES = (AXIS_USD, AXIS_CALLS, AXIS_TOKENS, AXIS_WALLCLOCK)

#: who a reservation is for. ``implementation`` (the planner/implementer legs) cannot enter
#: the protected closing share; ``closing`` (the reviewer leg) may spend the run's full
#: limits — the share exists precisely so the mandatory closing review can use it.
RESERVE_IMPLEMENTATION = "implementation"
RESERVE_CLOSING = "closing"


@dataclass(frozen=True)
class BudgetLimits:
    """The three F22 budget dimensions; ``None`` = unlimited on that axis."""

    wallclock_s: int | None = None
    max_calls: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class Reservation:
    """Value receipt of one granted hold — the reconcile-time identity."""

    id: str
    run_budget_id: str
    attempt_id: str | None
    reserved_calls: int
    reserved_tokens: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _known_token_total(usage: Any) -> int | None:
    """Sum the KNOWN token parts of a usage receipt ( normalization)."""
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
    """A whole, non-negative usage amount; anything else is a caller bug."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"budget {name} must be a non-negative int, got {value!r}")
    return value


def budget_limits_from_spec(spec_document: object) -> BudgetLimits | None:
    """Extract F22 budget limits from a RunSpec document's ``budgets`` block."""
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
    """Resolve a budget class's numeric ceilings from the configured profiles: the profile *name* the run's frozen ``budget_class`` points at, with unknown names degrading to the ``"standard"`` profile — never to a guess. ``None`` when nothing usable is configured or every axis of the resolved profile is unset: the run is then unlimited and no budget row is opened."""
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


@dataclass(frozen=True)
class BudgetProfileResolution:
    """ONE numeric profile resolved before execution, with the selection REASON recorded — the ``budget.profile_selection_reason`` observable's value."""

    budget_class: str
    profile_name: str | None
    limits: BudgetLimits | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "budget_class": self.budget_class,
            "profile_name": self.profile_name,
            "limits": (
                {
                    "max_calls": self.limits.max_calls,
                    "max_tokens": self.limits.max_tokens,
                    "wallclock_s": self.limits.wallclock_s,
                }
                if self.limits is not None
                else None
            ),
            "profile_selection_reason": self.reason,
        }


def resolve_budget_profile(
    profiles: dict[str, dict[str, Any]], budget_class: str
) -> BudgetProfileResolution:
    """Resolve ONE numeric profile for *budget_class* and RECORD why."""
    if not profiles:
        return BudgetProfileResolution(
            budget_class=budget_class,
            profile_name=None,
            limits=None,
            reason="no budget profiles configured (unlimited)",
        )
    profile = profiles.get(budget_class)
    if not isinstance(profile, dict):
        # The bounded fallback (the rule, now RECORDED): an unknown or unconfigured class
        # maps to the DEFAULT class's profile — the mapping stays total and the fallback is
        # named, never silent.
        fallback = profiles.get(DEFAULT_BUDGET_CLASS)
        fallback_reason = (
            f"class {budget_class!r} unconfigured -> standard profile (bounded fallback)"
        )
        if not isinstance(fallback, dict):
            return BudgetProfileResolution(
                budget_class=budget_class,
                profile_name=None,
                limits=None,
                reason=fallback_reason,
            )
        fallback_limits = BudgetLimits(
            wallclock_s=_limit_or_none(fallback.get("wallclock_s")),
            max_calls=_limit_or_none(fallback.get("max_calls")),
            max_tokens=_limit_or_none(fallback.get("max_tokens")),
        )
        return BudgetProfileResolution(
            budget_class=budget_class,
            profile_name=DEFAULT_BUDGET_CLASS,
            limits=None if fallback_limits == BudgetLimits() else fallback_limits,
            reason=fallback_reason,
        )
    limits = BudgetLimits(
        wallclock_s=_limit_or_none(profile.get("wallclock_s")),
        max_calls=_limit_or_none(profile.get("max_calls")),
        max_tokens=_limit_or_none(profile.get("max_tokens")),
    )
    if limits == BudgetLimits():
        return BudgetProfileResolution(
            budget_class=budget_class,
            profile_name=budget_class,
            limits=None,
            reason=f"profile {budget_class!r} carries no limited axis (unlimited)",
        )
    return BudgetProfileResolution(
        budget_class=budget_class,
        profile_name=budget_class,
        limits=limits,
        reason=f"configured profile: {budget_class}",
    )


def wallclock_deadline(budget: RunBudget) -> datetime | None:
    """The budget's absolute wall-clock deadline, or ``None`` when unlimited."""
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
    closing_reserved_calls: int | None = None,
    closing_reserved_tokens: int | None = None,
    closing_partition_policy: str | None = None,
) -> RunBudget:
    """Open the run's budget — idempotent per run (UNIQUE ``run_id``)."""
    if await session.get(FlowRun, run_id) is None:
        raise RunNotFound(f"flow run {run_id!r} not found")
    existing = await budget_for_run(session, run_id)
    if existing is not None:
        return existing
    inserted = await session.execute(
        pg_insert(RunBudget)
        .values(
            run_id=run_id,
            spec_digest=spec_digest,
            wallclock_s=wallclock_s,
            max_calls=max_calls,
            max_tokens=max_tokens,
            closing_reserved_calls=closing_reserved_calls,
            closing_reserved_tokens=closing_reserved_tokens,
            closing_partition_policy=closing_partition_policy,
        )
        # ON CONFLICT DO NOTHING works identically on Postgres and SQLite (tests) — the same
        # portable arbiter as the webhook inbox.
        .on_conflict_do_nothing(index_elements=[RunBudget.run_id])
    )
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    if inserted.rowcount != 1:  # type: ignore[attr-defined]
        # Lost the open race: adopt the winner's (frozen) row. Read back through the session
        # so both callers share the row's identity.
        winner = (
            await session.execute(
                select(RunBudget)
                .where(RunBudget.run_id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return winner
    return (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id))).scalar_one()


async def open_budget_from_spec(
    session: AsyncSession,
    *,
    run_id: str,
    spec_document: object,
    spec_digest: str | None = None,
    closing_reserved_calls: int | None = None,
    closing_reserved_tokens: int | None = None,
    closing_partition_policy: str | None = None,
) -> RunBudget | None:
    """Open the run's budget when its RunSpec carries budget limits."""
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
        closing_reserved_calls=closing_reserved_calls,
        closing_reserved_tokens=closing_reserved_tokens,
        closing_partition_policy=closing_partition_policy,
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
    purpose: str = RESERVE_IMPLEMENTATION,
) -> Reservation | None:
    """Reserve calls/tokens before a dispatch; ``None`` = refused."""
    calls = _usage_amount(calls, "calls")
    tokens = _usage_amount(tokens, "tokens")
    if calls < 1:
        raise ValueError("budget calls must reserve at least one call")
    if purpose not in (RESERVE_IMPLEMENTATION, RESERVE_CLOSING):
        raise ValueError(f"unknown reservation purpose {purpose!r}")
    deadline = wallclock_deadline(budget)
    if deadline is not None and _utcnow() > deadline:
        # Past the wall clock nothing is grantable any more — exhaust durably so the stop is
        # visible in every later read.
        await _exhaust_open(session, budget.id)
        return None
    # Implementation leaves the closing share available; a missing share means a shared axis.
    implementation_ceiling_calls = (
        RunBudget.max_calls - func.coalesce(RunBudget.closing_reserved_calls, 0)
        if purpose == RESERVE_IMPLEMENTATION
        else RunBudget.max_calls
    )
    implementation_ceiling_tokens = (
        RunBudget.max_tokens - func.coalesce(RunBudget.closing_reserved_tokens, 0)
        if purpose == RESERVE_IMPLEMENTATION
        else RunBudget.max_tokens
    )
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
                <= implementation_ceiling_calls,
            ),
            or_(
                RunBudget.max_tokens.is_(None),
                RunBudget.consumed_tokens
                + RunBudget.reserved_tokens
                + RunBudget.unresolved_tokens
                + tokens
                <= implementation_ceiling_tokens,
            ),
        )
        .values(
            reserved_calls=RunBudget.reserved_calls + calls,
            reserved_tokens=RunBudget.reserved_tokens + tokens,
            updated_at=_utcnow(),
        )
    )
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    if granted.rowcount != 1:  # type: ignore[attr-defined]
        # Refused.
        share_applies = purpose == RESERVE_IMPLEMENTATION and (
            budget.closing_reserved_calls is not None or budget.closing_reserved_tokens is not None
        )
        if not share_applies:
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
    """Settle a reservation against the provider's actual usage."""
    if actual_calls is not None:
        actual_calls = _usage_amount(actual_calls, "actual_calls")
    if actual_tokens is not None:
        actual_tokens = _usage_amount(actual_tokens, "actual_tokens")
    flip = await session.execute(
        update(BudgetReservation)
        .where(BudgetReservation.id == reservation.id, BudgetReservation.released.is_(False))
        .values(released=True)
    )
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    if flip.rowcount != 1:  # type: ignore[attr-defined]
        # Already reconciled (crash-retry) — the counters were moved once.
        return await session.get(RunBudget, reservation.run_budget_id)

    # The hold of an unknown dimension becomes unresolved liability — the same capacity that
    # was fenced for the dispatch stays fenced.
    unresolved_calls = reservation.reserved_calls if actual_calls is None else 0
    unresolved_tokens = reservation.reserved_tokens if actual_tokens is None else 0
    await session.execute(
        update(RunBudget)
        .where(RunBudget.id == reservation.run_budget_id)
        .values(
            # Never let a hold drive a counter negative (defensive: holds are only ever
            # granted against the live row).
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
    """Close the budget (terminal; refuses all further reservations)."""
    result = await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget.id, RunBudget.status != "closed")
        .values(status="closed", updated_at=_utcnow())
    )
    await session.flush()
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    return result.rowcount == 1  # type: ignore[attr-defined]


# budget amendments — persisted, command-identified, applied atomically to the enforcement
# resource


@dataclass(frozen=True)
class BudgetAmendmentCommand:
    """One operator amendment: an axis, an amount, a reason, an operator and the ORIGINATING NATIVE COMMAND identity — all required."""

    run_id: str
    command_id: str
    axis: str
    amount: float | int
    reason: str
    operator: str = ""

    def __post_init__(self) -> None:
        if not str(self.command_id or "").strip():
            raise ValueError(
                "budget amendment requires the originating native command identity"
                " (command_id) — content identity cannot tell two decisions apart"
            )
        if self.axis not in BUDGET_AXES:
            raise ValueError(
                f"budget amendment axis {self.axis!r} is not one of {BUDGET_AXES}"
                " — the dimensions are distinct and never converted implicitly"
            )
        if not str(self.reason or "").strip():
            raise ValueError("budget amendment requires an explicit reason (auditable)")
        amount = self.amount
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("budget amendment amount must be a number")
        if self.axis == AXIS_USD:
            if not isinstance(amount, float) and not isinstance(amount, int):
                raise ValueError("usd amendment amount must be a number")
            if not math.isfinite(float(amount)) or float(amount) <= 0:
                raise ValueError("usd amendment amount must be a positive number")
        else:
            if isinstance(amount, float) and (not math.isfinite(amount) or amount != int(amount)):
                raise ValueError(
                    f"{self.axis} amendment amount must be a whole number of units on that axis"
                )
            if int(amount) <= 0:
                raise ValueError(f"{self.axis} amendment amount must be positive")

    @property
    def amount_usd(self) -> float | None:
        return round(float(self.amount), 6) if self.axis == AXIS_USD else None

    @property
    def amount_calls(self) -> int | None:
        return int(self.amount) if self.axis == AXIS_CALLS else None

    @property
    def amount_tokens(self) -> int | None:
        return int(self.amount) if self.axis == AXIS_TOKENS else None

    @property
    def amount_wallclock_s(self) -> int | None:
        return int(self.amount) if self.axis == AXIS_WALLCLOCK else None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command_id": str(self.command_id),
            "axis": self.axis,
            "amount_usd": self.amount_usd,
            "amount_calls": self.amount_calls,
            "amount_tokens": self.amount_tokens,
            "amount_wallclock_s": self.amount_wallclock_s,
            "reason": str(self.reason).strip(),
            "operator": str(self.operator or ""),
        }


@dataclass(frozen=True)
class AppliedBudgetAmendment:
    """What one apply did — applied, replayed, or refused with the limiting axis named."""

    command: BudgetAmendmentCommand
    applied: bool
    replayed: bool
    refusal_reason: str | None
    limit_before: dict[str, Any] | None = None
    limit_after: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            **self.command.to_json(),
            "applied": self.applied,
            "replayed": self.replayed,
            "refusal_reason": self.refusal_reason,
            "limit_before": self.limit_before,
            "limit_after": self.limit_after,
        }


def _budget_limits_json(budget: RunBudget) -> dict[str, Any]:
    """The approved budget as it stands — the amendment's history record."""
    return {
        "max_calls": budget.max_calls,
        "max_tokens": budget.max_tokens,
        "wallclock_s": budget.wallclock_s,
        "status": budget.status,
        "closing_reserved_calls": budget.closing_reserved_calls,
        "closing_reserved_tokens": budget.closing_reserved_tokens,
    }


async def budget_amendments_for_run(session: AsyncSession, run_id: str) -> list[BudgetAmendment]:
    """The run's amendment rows, oldest first — the audit read."""
    return list(
        (
            await session.execute(
                select(BudgetAmendment)
                .where(BudgetAmendment.run_id == run_id)
                .order_by(BudgetAmendment.applied_at, BudgetAmendment.id)
            )
        )
        .scalars()
        .all()
    )


async def usd_amendment_total(session: AsyncSession, run_id: str) -> float:
    """The run's APPLIED usd-axis amendments, summed."""
    rows = await budget_amendments_for_run(session, run_id)
    return math.fsum(
        row.amount_usd for row in rows if row.status == "applied" and row.amount_usd is not None
    )


async def apply_budget_amendment(
    session: AsyncSession, command: BudgetAmendmentCommand
) -> AppliedBudgetAmendment:
    """Apply one amendment ATOMICALLY to the enforcement resource."""
    inserted = await session.execute(
        pg_insert(BudgetAmendment)
        .values(
            run_id=command.run_id,
            command_id=str(command.command_id).strip(),
            axis=command.axis,
            amount_usd=command.amount_usd,
            amount_calls=command.amount_calls,
            amount_tokens=command.amount_tokens,
            amount_wallclock_s=command.amount_wallclock_s,
            reason=str(command.reason).strip(),
            operator=str(command.operator or ""),
            status="applied",
        )
        # The native-command identity is the idempotency key: a redelivery of ONE command
        # applies once (portable on Postgres and SQLite — the same arbiter as the
        # usage-receipt ingest).
        .on_conflict_do_nothing(index_elements=[BudgetAmendment.run_id, BudgetAmendment.command_id])
    )
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    if inserted.rowcount != 1:  # type: ignore[attr-defined]
        # Lost the command-identity race (a redelivery): DO NOTHING left the transaction
        # fully usable — no rollback, no session poison — and the standing row answers what
        # the first delivery did.
        standing = (
            await session.execute(
                select(BudgetAmendment).where(
                    BudgetAmendment.run_id == command.run_id,
                    BudgetAmendment.command_id == str(command.command_id).strip(),
                )
            )
        ).scalar_one()
        return AppliedBudgetAmendment(
            command=command,
            applied=False,
            replayed=True,
            refusal_reason=standing.refusal_reason,
            limit_before=standing.limit_before,
            limit_after=standing.limit_after,
        )

    if command.axis == AXIS_USD:
        # The usd axis is enforced at the CLOSING GATE over the usage receipts, not by the
        # guard's counters — the durable APPLIED row IS the enforcement record (the
        # effective cap rises by it in :func:`usd_amendment_total`), so it needs no
        # ``run_budgets`` row.
        budget = await budget_for_run(session, command.run_id)
        before = _budget_limits_json(budget) if budget is not None else None
        await _stamp_applied_history(session, command, before, before)
        return AppliedBudgetAmendment(
            command=command,
            applied=True,
            replayed=False,
            refusal_reason=None,
            limit_before=before,
            limit_after=before,
        )

    budget = await budget_for_run(session, command.run_id)
    if budget is None:
        return await _record_refused_amendment(
            session,
            command,
            f"{command.axis}: no run budget exists — the count axes are enforced"
            " by the guard's run_budgets row and there is nothing to amend",
        )

    before = _budget_limits_json(budget)

    limit_column = {
        AXIS_CALLS: RunBudget.max_calls,
        AXIS_TOKENS: RunBudget.max_tokens,
        AXIS_WALLCLOCK: RunBudget.wallclock_s,
    }[command.axis]
    amount = {
        AXIS_CALLS: command.amount_calls,
        AXIS_TOKENS: command.amount_tokens,
        AXIS_WALLCLOCK: command.amount_wallclock_s,
    }[command.axis]
    assert amount is not None
    standing_limit = {
        AXIS_CALLS: budget.max_calls,
        AXIS_TOKENS: budget.max_tokens,
        AXIS_WALLCLOCK: budget.wallclock_s,
    }[command.axis]
    if standing_limit is None:
        return await _record_refused_amendment(
            session,
            command,
            f"{command.axis}: the axis is unlimited — there is no cap for the"
            " amendment to raise (name the axis that actually limits)",
        )
    if budget.status == "closed":
        return await _record_refused_amendment(
            session,
            command,
            f"{command.axis}: the budget is closed (terminal) — an amendment"
            " never resurrects a finished run",
        )
    if command.axis == AXIS_WALLCLOCK:
        await session.execute(
            update(RunBudget)
            .where(RunBudget.id == budget.id)
            .values(
                wallclock_s=limit_column + amount,
                # The exhausted state existed only because the deadline passed; the extended
                # window re-opens it (a closed budget was refused above).
                status=case((RunBudget.status == "exhausted", "open"), else_=RunBudget.status),
                updated_at=_utcnow(),
            )
        )
        await session.flush()
        # The Core UPDATE bypasses the identity map — reload the row so the after-picture
        # reads the moved limits (refresh performs its IO inside the awaited greenlet, never
        # at attribute access).
        await session.refresh(budget)
        deadline = wallclock_deadline(budget)
        if deadline is not None and _utcnow() > deadline:
            # The extension still does not reach the present — the budget stays exhausted
            # and the amendment honestly says so.
            await _exhaust_open(session, budget.id)
            return await _record_refused_amendment(
                session,
                command,
                f"{command.axis}: the extended deadline is still in the past"
                " — the budget remains exhausted",
                before=before,
            )
        after = _budget_limits_json(budget)
        await _stamp_applied_history(session, command, before, after)
        return AppliedBudgetAmendment(
            command=command,
            applied=True,
            replayed=False,
            refusal_reason=None,
            limit_before=before,
            limit_after=after,
        )
    else:
        # AXIS_CALLS | AXIS_TOKENS: raise the numeric limit, re-opening an exhausted budget
        # in the same atomic move (closed was refused above).
        limit_name = "max_calls" if command.axis == AXIS_CALLS else "max_tokens"
        reopen = case((RunBudget.status == "exhausted", "open"), else_=RunBudget.status)
        await session.execute(
            update(RunBudget)
            .where(RunBudget.id == budget.id)
            .values(
                {
                    limit_name: limit_column + amount,
                    "status": reopen,
                    "updated_at": _utcnow(),
                }
            )
        )
        await session.flush()
        await session.refresh(budget)  # the moved limits, re-read
        after_ct = _budget_limits_json(budget)
        await _stamp_applied_history(session, command, before, after_ct)
        return AppliedBudgetAmendment(
            command=command,
            applied=True,
            replayed=False,
            refusal_reason=None,
            limit_before=before,
            limit_after=after_ct,
        )


async def _stamp_applied_history(
    session: AsyncSession,
    command: BudgetAmendmentCommand,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    """Write the enforcement history onto the freshly inserted row."""
    await session.execute(
        update(BudgetAmendment)
        .where(
            BudgetAmendment.run_id == command.run_id,
            BudgetAmendment.command_id == str(command.command_id).strip(),
            BudgetAmendment.status == "applied",
        )
        .values(limit_before=before, limit_after=after)
    )
    await session.flush()


async def _record_refused_amendment(
    session: AsyncSession,
    command: BudgetAmendmentCommand,
    refusal_reason: str,
    *,
    before: dict[str, Any] | None = None,
) -> AppliedBudgetAmendment:
    """Flip the freshly inserted amendment row to ``refused`` (typed reason)."""
    await session.execute(
        update(BudgetAmendment)
        .where(
            BudgetAmendment.run_id == command.run_id,
            BudgetAmendment.command_id == str(command.command_id).strip(),
            BudgetAmendment.status == "applied",
        )
        .values(status="refused", refusal_reason=refusal_reason[:300], limit_before=before)
    )
    await session.flush()
    return AppliedBudgetAmendment(
        command=command,
        applied=False,
        replayed=False,
        refusal_reason=refusal_reason,
        limit_before=before,
    )


async def limiting_axis(
    session: AsyncSession,
    run_id: str,
    *,
    calls: int = 1,
    tokens: int = 0,
    purpose: str = RESERVE_CLOSING,
    now: datetime | None = None,
) -> str | None:
    """Which axis would refuse a reservation of this shape, or ``None``."""
    budget = await budget_for_run(session, run_id)
    if budget is None:
        return None
    if budget.status == "closed":
        return "status"
    deadline = wallclock_deadline(budget)
    if deadline is not None and as_aware_utc(now or _utcnow()) > deadline:
        return AXIS_WALLCLOCK
    ceiling_calls = budget.max_calls
    ceiling_tokens = budget.max_tokens
    if purpose == RESERVE_IMPLEMENTATION:
        if budget.closing_reserved_calls is not None and budget.max_calls is not None:
            ceiling_calls = budget.max_calls - budget.closing_reserved_calls
        if budget.closing_reserved_tokens is not None and budget.max_tokens is not None:
            ceiling_tokens = budget.max_tokens - budget.closing_reserved_tokens
    if ceiling_calls is not None and (
        budget.consumed_calls + budget.reserved_calls + budget.unresolved_calls + calls
        > ceiling_calls
    ):
        return AXIS_CALLS
    if ceiling_tokens is not None and (
        budget.consumed_tokens + budget.reserved_tokens + budget.unresolved_tokens + tokens
        > ceiling_tokens
    ):
        return AXIS_TOKENS
    if budget.status == "exhausted":
        # Room on every numeric axis yet the row still says exhausted — the status itself is
        # the blocker the operator sees named.
        return "status"
    return None


async def budget_block_reason(
    session: AsyncSession,
    run_id: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Why a NEW dispatch/episode must not start against this run, or ``None``."""
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
    """Claim an idempotency key for a harness receipt; ``False`` = seen before."""
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
    """Durably flip an ``open`` budget to ``exhausted`` (any other status stands)."""
    await session.execute(
        update(RunBudget)
        .where(RunBudget.id == budget_id, RunBudget.status == "open")
        .values(status="exhausted", updated_at=_utcnow())
    )
    await session.flush()


async def _exhaust_if_over(session: AsyncSession, budget_id: str) -> RunBudget | None:
    """Mark the budget ``exhausted`` when recorded spend reaches a limit."""
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
    """Reconcile a harness candidate's usage receipt against the run budget."""
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
    # rowcount is the UPDATE's matched-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
    if recorded.rowcount != 1:  # type: ignore[attr-defined]
        return None
    if tokens is None and budget.max_tokens is not None:
        # Limits are frozen at open time, so the loaded row's limit is live.
        await _exhaust_open(session, budget.id)
    return await _exhaust_if_over(session, budget.id)


def _receipt_cost_columns(usage: Any, raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The canonical cost/finality columns for a receipt ( item 5)."""
    from forge.adaptive.usage_ingestion import COST_BASES

    def _raw(name: str) -> Any:
        return (raw or {}).get(name)

    def _cost(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)) or float(value) < 0:
            return None
        return round(float(value), 6)

    own_final = getattr(usage, "final", None)
    if not isinstance(own_final, bool):
        own_final = _raw("final")
    final = own_final if isinstance(own_final, bool) else True
    cost = _cost(getattr(usage, "cost_usd", None))
    if cost is None:
        cost = _cost(_raw("cost_usd"))
    basis = str(getattr(usage, "cost_basis", "") or _raw("cost_basis") or "").strip()
    return {
        "final": final,
        "cost_usd": cost,
        "cost_basis": basis if basis in COST_BASES else None,
        "rate_card_id": str(_raw("rate_card_id") or "")[:64] or None,
        "route_version": str(_raw("route_version") or "")[:64] or None,
        "segment": str(_raw("segment") or "")[:80] or None,
        "artifact_digest": str(_raw("artifact_digest") or "")[:128] or None,
    }


async def ingest_usage_receipt(
    session: AsyncSession,
    *,
    run_id: str,
    usage: Any,
    attempt_id: str = "",
    model_fallback: str = "unknown",
    record_ledger_call: bool = True,
) -> tuple[RunBudget | None, bool]:
    """Ingest one harness usage receipt EXACTLY ONCE."""
    from forge.runs.candidate import usage_receipt_id

    # The deferred import keeps forge.durable → forge.runs.candidate out of module level:
    # runs.candidate → factory.llm → forge.durable.budgets would close a cycle at import
    # time.
    attempt = str(getattr(usage, "attempt_id", "") or attempt_id or "")[:100]
    receipt_id = str(getattr(usage, "receipt_id", "") or "") or usage_receipt_id(
        run_id, attempt, usage
    )

    def _int(name: str) -> int | None:
        value = getattr(usage, name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    raw = usage.raw_document()
    # the durable identity widened to its fourth component — the SOURCE NAMESPACE (the lane
    # label the receipt arrived under, the same value the ``source`` column carries).
    namespace = str(getattr(usage, "source", "") or "")[:100]
    cost_columns = _receipt_cost_columns(usage, raw if isinstance(raw, dict) else None)
    inserted = await session.execute(
        pg_insert(UsageReceipt)
        .values(
            run_id=run_id,
            attempt_id=attempt,
            receipt_id=receipt_id,
            source_namespace=namespace,
            driver=str(getattr(usage, "driver", "") or "") or None,
            model=str(getattr(usage, "model", "") or "") or None,
            input_tokens=_int("input_tokens"),
            cached_input_tokens=_int("cached_input_tokens"),
            cache_write_tokens=_int("cache_write_tokens"),
            output_tokens=_int("output_tokens"),
            completeness=str(getattr(usage, "completeness", "") or "unknown"),
            source=str(getattr(usage, "source", "") or "") or None,
            **cost_columns,
            raw=raw if isinstance(raw, dict) else None,
        )
        # ON CONFLICT DO NOTHING works identically on Postgres and SQLite (tests) — the same
        # portable arbiter as the webhook inbox, now against the CANONICAL four-column
        # identity.
        .on_conflict_do_nothing(
            index_elements=[
                UsageReceipt.run_id,
                UsageReceipt.attempt_id,
                UsageReceipt.receipt_id,
                UsageReceipt.source_namespace,
            ]
        )
    )
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
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
    """The per-run handle an ``LLMClient`` uses for reserve/reconcile."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        budget_id: str,
        *,
        attempt_id: str | None = None,
        purpose: str = RESERVE_IMPLEMENTATION,
    ) -> None:
        if purpose not in (RESERVE_IMPLEMENTATION, RESERVE_CLOSING):
            raise ValueError(f"unknown reservation purpose {purpose!r}")
        self._session_factory = session_factory
        self.budget_id = budget_id
        self._attempt_id = attempt_id
        self.purpose = purpose

    async def reserve(self, *, calls: int = 1, tokens: int = 0) -> Reservation | None:
        """Commit a pre-dispatch hold, or ``None`` when refused."""
        async with self._session_factory() as session:
            budget = await session.get(RunBudget, self.budget_id)
            if budget is None:
                return None
            reservation = await reserve(
                session,
                budget,
                calls=calls,
                tokens=tokens,
                attempt_id=self._attempt_id,
                purpose=self.purpose,
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
    purpose: str = RESERVE_IMPLEMENTATION,
) -> BudgetGuard | None:
    """The run's enforcement handle, or ``None`` when nothing is budgeted."""
    async with session_factory() as session:
        budget = await budget_for_run(session, run_id)
        if budget is None or budget.status == "closed":
            return None
        return BudgetGuard(session_factory, budget.id, attempt_id=attempt_id, purpose=purpose)
