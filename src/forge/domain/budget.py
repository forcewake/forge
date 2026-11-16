"""Cumulative pool arithmetic with unknown liability held until final settlement."""

from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Rounded,
    Underflow,
    localcontext,
)
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, field_serializer, model_validator

from .value import Value


def _bounded_money(value: Decimal) -> Decimal:
    # Tuple arithmetic removes only insignificant zeros, without Decimal.normalize rounding.
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    end = len(digits)
    while end and digits[end - 1] == 0:
        end -= 1
    if end == 0:
        return Decimal(0)
    exponent += len(digits) - end
    digits = digits[:end]
    if max(-exponent, 0) > 18 or max(len(digits) + exponent, 0) > 20:
        raise ValueError("money exceeds exact NUMERIC(38,18) representation")
    return Decimal((sign, digits, exponent))


def _money_arithmetic(left: Decimal, right: Decimal, *, subtract: bool = False) -> Decimal:
    # Accepted operands align within 38 places; even an overflowing sum is exact here.
    context = Context(
        prec=80,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        traps=[Inexact, Rounded, Overflow, Underflow, InvalidOperation],
    )
    with localcontext(context):
        return left - right if subtract else left + right


Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False), AfterValidator(_bounded_money)]
Count = Annotated[int, Field(ge=0, le=(1 << 63) - 1)]
Positive = Annotated[int, Field(gt=0)]
Identity = Annotated[str, Field(min_length=1)]


class Resources(Value):
    usd: Money = Decimal(0)
    calls: Count = 0
    tokens: Count = 0

    def __add__(self, other: "Resources") -> "Resources":
        return Resources(
            usd=_money_arithmetic(self.usd, other.usd),
            calls=self.calls + other.calls,
            tokens=self.tokens + other.tokens,
        )

    def covers(self, other: "Resources") -> bool:
        return self.usd >= other.usd and self.calls >= other.calls and self.tokens >= other.tokens

    def subtract(self, other: "Resources") -> "Resources":
        if not self.covers(other):
            raise ValueError("resources would become negative")
        return Resources(
            usd=_money_arithmetic(self.usd, other.usd, subtract=True),
            calls=self.calls - other.calls,
            tokens=self.tokens - other.tokens,
        )

    def bounded_by(self, other: "Resources") -> "Resources":
        return Resources(
            usd=min(self.usd, other.usd),
            calls=min(self.calls, other.calls),
            tokens=min(self.tokens, other.tokens),
        )

    def high_water(self, other: "Resources") -> "Resources":
        return Resources(
            usd=max(self.usd, other.usd),
            calls=max(self.calls, other.calls),
            tokens=max(self.tokens, other.tokens),
        )

    @field_serializer("usd", when_used="json")
    def plain_usd(self, value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


class BudgetLimits(Resources):
    wallclock_seconds: Positive


class Partition(Value):
    implementation: Resources
    closing: Resources


def partition(total: Resources, closing: Resources) -> Partition:
    return Partition(implementation=total.subtract(closing), closing=closing)


class Observation(Value):
    usd: Money | None
    calls: Count | None
    tokens: Count | None

    @property
    def complete(self) -> bool:
        return self.usd is not None and self.calls is not None and self.tokens is not None

    @property
    def lower_bound(self) -> Resources:
        return Resources(usd=self.usd or Decimal(0), calls=self.calls or 0, tokens=self.tokens or 0)

    def high_water(self, other: "Observation") -> "Observation":
        maximum = self.lower_bound.high_water(other.lower_bound)
        return Observation(
            usd=maximum.usd if self.usd is not None or other.usd is not None else None,
            calls=maximum.calls if self.calls is not None or other.calls is not None else None,
            tokens=maximum.tokens if self.tokens is not None or other.tokens is not None else None,
        )


class Reservation(Value):
    identity: Identity
    requested: Resources
    allowance: Resources
    minimum: Resources = Resources()
    status: Literal["open", "unknown", "accrued", "settled"] = "open"
    observed: Observation | None = None

    @model_validator(mode="after")
    def validate_hold(self) -> "Reservation":
        if not self.requested.covers(self.allowance):
            raise ValueError("allowance exceeds requested resources")
        if self.allowance == Resources() or not self.allowance.covers(self.minimum):
            raise ValueError("reservation does not meet its minimum allowance")
        if self.status in ("accrued", "settled") and self.observed is None:
            raise ValueError("known usage requires an observation")
        if self.status == "settled" and (self.observed is None or not self.observed.complete):
            raise ValueError("settlement requires every usage axis to be known")
        if self.status == "open" and self.observed is not None:
            raise ValueError("an unopened observation cannot carry usage")
        return self


class Amendment(Value):
    command_id: Identity
    amount: Resources


class Balance(Value):
    available: Resources
    held: Resources
    settled: Resources
    shortfall: Resources


def _sum(values: tuple[Resources, ...]) -> Resources:
    result = Resources()
    for value in values:
        result = result + value
    return result


class BudgetConflict(ValueError):
    pass


class BudgetExhausted(ValueError):
    pass


class Pool(Value):
    identity: Identity
    capacity: Resources
    reservations: tuple[Reservation, ...] = ()
    amendments: tuple[Amendment, ...] = ()

    @model_validator(mode="after")
    def unique_identities(self) -> "Pool":
        for names in (
            tuple(row.identity for row in self.reservations),
            tuple(row.command_id for row in self.amendments),
        ):
            if len(set(names)) != len(names):
                raise ValueError("duplicate pool record identity")
        return self

    @property
    def balance(self) -> Balance:
        limit = self.capacity + _sum(tuple(row.amount for row in self.amendments))
        held = _sum(
            tuple(
                row.allowance.high_water(row.observed.lower_bound if row.observed else Resources())
                for row in self.reservations
                if row.status != "settled"
            )
        )
        settled = _sum(
            tuple(
                row.observed.lower_bound
                for row in self.reservations
                if row.status == "settled" and row.observed is not None
            )
        )
        retained = held + settled
        covered = retained.bounded_by(limit)
        return Balance(
            available=limit.subtract(covered),
            held=held,
            settled=settled,
            shortfall=retained.subtract(covered),
        )

    def reserve(self, identity: str, requested: Resources, minimum: Resources) -> "Pool":
        existing = next((row for row in self.reservations if row.identity == identity), None)
        if existing is not None:
            if existing.requested != requested or existing.minimum != minimum:
                raise BudgetConflict("reservation identity was reused with different input")
            return self
        granted = requested.bounded_by(self.balance.available)
        if (
            self.balance.shortfall != Resources()
            or granted == Resources()
            or not granted.covers(minimum)
        ):
            raise BudgetExhausted("budget_exhausted")
        return Pool(
            identity=self.identity,
            capacity=self.capacity,
            amendments=self.amendments,
            reservations=(
                *self.reservations,
                Reservation(
                    identity=identity, requested=requested, allowance=granted, minimum=minimum
                ),
            ),
        )

    def observe(self, identity: str, usage: Observation | None, *, final: bool) -> "Pool":
        if usage is not None and not isinstance(usage, Observation):
            raise TypeError("usage axes must be explicitly observed or unknown")
        existing = next((row for row in self.reservations if row.identity == identity), None)
        if existing is None:
            raise BudgetConflict("reservation does not exist")
        if existing.status == "settled":
            if not final or usage != existing.observed:
                raise BudgetConflict("settled usage cannot be overwritten")
            return self
        if final and (usage is None or not usage.complete):
            raise ValueError("unknown usage axes cannot settle")
        observed = usage if usage is not None else existing.observed
        if not final and observed is not None and existing.observed is not None:
            observed = observed.high_water(existing.observed)
        updated = Reservation(
            identity=identity,
            requested=existing.requested,
            allowance=existing.allowance,
            minimum=existing.minimum,
            status="settled" if final else "unknown" if usage is None else "accrued",
            observed=observed,
        )
        return Pool(
            identity=self.identity,
            capacity=self.capacity,
            amendments=self.amendments,
            reservations=tuple(
                updated if row.identity == identity else row for row in self.reservations
            ),
        )

    def amend(self, command_id: str, amount: Resources) -> "Pool":
        if amount == Resources():
            raise ValueError("amendment must increase a resource limit")
        existing = next((row for row in self.amendments if row.command_id == command_id), None)
        if existing is not None:
            if existing.amount != amount:
                raise BudgetConflict("amendment identity was reused with different input")
            return self
        return Pool(
            identity=self.identity,
            capacity=self.capacity,
            reservations=self.reservations,
            amendments=(*self.amendments, Amendment(command_id=command_id, amount=amount)),
        )

    def resolve_unknown(self) -> "Pool":
        """Settle an unproven hold at its known lower bound so retry can reserve again."""
        resolved: list[Reservation] = []
        changed = False
        for row in self.reservations:
            if row.status != "unknown":
                resolved.append(row)
                continue
            bound = row.observed.lower_bound if row.observed is not None else Resources()
            resolved.append(
                Reservation(
                    identity=row.identity,
                    requested=row.requested,
                    allowance=row.allowance,
                    minimum=row.minimum,
                    status="settled",
                    observed=Observation(usd=bound.usd, calls=bound.calls, tokens=bound.tokens),
                )
            )
            changed = True
        if not changed:
            return self
        return Pool(
            identity=self.identity,
            capacity=self.capacity,
            amendments=self.amendments,
            reservations=tuple(resolved),
        )
