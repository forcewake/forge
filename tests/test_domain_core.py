"""Pure lifecycle and cumulative accounting properties independent of adapters."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import random

from pydantic import ValidationError
import pytest

from forge.domain.budget import (
    BudgetConflict,
    BudgetExhausted,
    Observation,
    Pool,
    Resources,
    partition,
)
from forge.domain.states import (
    HANDLER_STATE,
    TERMINAL,
    TRANSITIONS,
    Handler,
    InvalidTransition,
    State,
    revive,
    transition,
)
from forge.domain.verdicts import CheckVerdict


def resource(usd=0, calls=0, tokens=0):
    return Resources(usd=Decimal(str(usd)), calls=calls, tokens=tokens)


def observation(usd=0, calls=0, tokens=0):
    return Observation(usd=Decimal(str(usd)), calls=calls, tokens=tokens)


def test_transition_graph_is_closed_and_terminal_states_are_absorbing():
    assert set(TRANSITIONS) == set(State)
    assert set(HANDLER_STATE) == set(Handler)
    for current in State:
        assert TRANSITIONS[current] <= set(State)
        for target in State:
            if target in TRANSITIONS[current]:
                assert transition(current, target) == target
            else:
                with pytest.raises(InvalidTransition):
                    transition(current, target)
        if current in TERMINAL:
            assert not TRANSITIONS[current]
    assert revive(State.BLOCKED, Handler.PLAN) == State.PLANNING
    assert revive(State.FAILED, Handler.PUBLISH) == State.RUNNING
    assert revive(State.BLOCKED, Handler.REVIEW) == State.REVIEWING
    with pytest.raises(InvalidTransition):
        revive(State.CANCELLED, Handler.DISPATCH)


def test_seeded_partitions_conserve_every_axis_and_refuse_negative_shares():
    rng = random.Random(42)
    for _ in range(200):
        total = resource(
            Decimal(rng.randrange(10000)) / 100, rng.randrange(100), rng.randrange(10000)
        )
        share = resource(total.usd / 2, total.calls // 2, total.tokens // 2)
        result = partition(total, share)
        assert result.implementation + result.closing == total
        assert total.covers(result.closing) and total.covers(result.implementation)
        with pytest.raises(ValueError):
            partition(total, total + resource(1))


def test_unknown_and_accrued_usage_hold_the_full_reservation_until_final():
    pool = Pool(identity="run:P", capacity=resource(10, 10, 1000))
    requested, minimum = resource(6, 3, 300), resource(1, 1, 1)
    held = pool.reserve("attempt-1", requested, minimum)
    assert held.reserve("attempt-1", requested, minimum) == held
    assert held.balance.available == resource(4, 7, 700)
    accrued = held.observe("attempt-1", observation(1, 1, 100), final=False)
    unknown = accrued.observe("attempt-1", None, final=False)
    assert unknown.balance.available == held.balance.available
    assert unknown.reservations[0].observed == observation(1, 1, 100)
    with pytest.raises(ValueError, match="cannot settle"):
        unknown.observe("attempt-1", None, final=True)
    settled = unknown.observe("attempt-1", observation(2, 2, 200), final=True)
    assert settled.balance.available == resource(8, 8, 800)
    assert settled.balance.settled == resource(2, 2, 200)
    again = settled.reserve("failed-then-retry", requested, minimum)
    assert again.balance.available == resource(2, 5, 500)
    with pytest.raises(BudgetConflict):
        settled.observe("attempt-1", observation(0), final=True)
    with pytest.raises(BudgetConflict):
        held.reserve("attempt-1", requested, resource(2))


def test_accrued_overrun_retains_its_high_water_and_blocks_new_spend():
    pool = Pool(identity="run:P", capacity=resource(10, 10, 1000))
    pool = pool.reserve("a", resource(5, 5, 100), resource(1))
    pool = pool.observe("a", observation(8, 6, 160), final=False)
    assert pool.balance.held == resource(8, 6, 160)
    assert pool.balance.available == resource(2, 4, 840)
    pool = pool.observe("a", observation(1, 1, 1), final=False)
    assert pool.balance.held == resource(8, 6, 160)
    pool = pool.observe("a", observation(12, 7, 1200), final=False)
    assert pool.balance.shortfall == resource(2, 0, 200)
    with pytest.raises(BudgetExhausted):
        pool.reserve("b", resource(0, 1, 0), resource(0, 1, 0))


def test_final_overrun_is_explicit_and_amendments_are_apply_once():
    pool = Pool(identity="run:P", capacity=resource(10))
    pool = pool.reserve("a", resource(6), resource(1)).observe("a", observation(12), final=True)
    assert pool.balance.shortfall == resource(2) and pool.balance.available == resource()
    pool = pool.amend("operator-command", resource(5))
    assert pool.balance.shortfall == resource() and pool.balance.available == resource(3)
    assert pool.amend("operator-command", resource(5)) == pool
    with pytest.raises(BudgetConflict):
        pool.amend("operator-command", resource(6))


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal(-1), True, 1.5])
def test_invalid_money_never_becomes_a_usable_allowance(bad):
    with pytest.raises(ValidationError):
        Resources(usd=bad)


def test_check_success_requires_every_required_check_for_the_current_candidate():
    values = dict(
        tested_sha="a" * 40,
        required=("tests",),
        successful=(),
        observed_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        evidence="{}",
    )
    with pytest.raises(ValidationError):
        CheckVerdict(status="passed", **values)
    passed = CheckVerdict(status="passed", **{**values, "successful": ("tests",)})
    freshness = dict(now=values["observed_at"], max_age_seconds=60)
    assert passed.verified_for("a" * 40, ("tests",), **freshness)
    assert not passed.verified_for("b" * 40, ("tests",), **freshness)
    assert not passed.verified_for("a" * 40, ("tests", "lint"), **freshness)
    assert not CheckVerdict(status="unknown", **values).verified_for(
        "a" * 40, ("tests",), **freshness
    )
    assert not passed.verified_for(
        "a" * 40, ("tests",), now=values["observed_at"] + timedelta(seconds=61), max_age_seconds=60
    )


def test_missing_usage_axes_never_default_to_zero_or_release_liability():
    pool = Pool(identity="run:P", capacity=resource(10, 10, 1000))
    pool = pool.reserve("a", resource(5, 5, 500), resource(1))
    partial = Observation(usd=Decimal(2), calls=None, tokens=100)
    pool = pool.observe("a", partial, final=False)
    assert pool.balance.held == resource(5, 5, 500)
    assert pool.reservations[0].observed.calls is None
    with pytest.raises(ValueError, match="cannot settle"):
        pool.observe("a", partial, final=True)
    with pytest.raises(ValidationError):
        Observation(usd=Decimal(2))
    with pytest.raises(TypeError):
        pool.observe("a", resource(2), final=True)


def test_money_arithmetic_is_exact_under_hostile_decimal_context():
    with localcontext() as hostile:
        hostile.prec, hostile.Emax, hostile.Emin = 6, 6, -6
        hostile.rounding = ROUND_DOWN
        hostile.traps[Inexact] = True
        total = resource("12345678901234567890.123456789012345678", 9, 90)
        closing = resource("0.000000000000123456", 1, 10)
        result = partition(total, closing)
        assert result.implementation.usd == Decimal("12345678901234567890.123456789012222222")
        assert result.implementation.calls == 8 and result.implementation.tokens == 80
        assert result.implementation + closing == total
        assert (total + closing).usd == Decimal("12345678901234567890.123456789012469134")
        assert hostile.prec == 6 and hostile.Emax == 6 and hostile.Emin == -6


def test_money_format_refuses_overflow_without_rounding():
    with localcontext() as hostile:
        hostile.prec = 6
        for value in ("100000000000000000000", "1.1234567890123456789", "0.0000000000000000001"):
            with pytest.raises(ValidationError):
                resource(value)
        maximum = resource("99999999999999999999.999999999999999999")
        with pytest.raises(ValidationError):
            maximum + resource("0.000000000000000001")
        assert resource("1.000000000000000000000000000000000000000000").usd.as_tuple().exponent == 0
        assert resource("0E-999999").usd.as_tuple().exponent == 0


def test_small_provider_costs_and_large_caps_settle_without_loss():
    with localcontext() as hostile:
        hostile.prec = 6
        pool = Pool(
            identity="precise", capacity=resource("12345678901234567890.123456789012345678", 2, 100)
        )
        pool = pool.reserve(
            "a", resource("0.000000000002", 1, 10), resource("0.000000000000000001", 1, 1)
        )
        known = Observation(usd=Decimal("0.000000000001234567"), calls=1, tokens=4)
        pool = pool.observe("a", known, final=False)
        assert pool.balance.held.usd == Decimal("0.000000000002")
        pool = pool.observe("a", known, final=True)
        assert pool.balance.settled.usd == Decimal("0.000000000001234567")
        assert pool.balance.available.usd == Decimal("12345678901234567890.123456789011111111")
        assert pool.balance.available.calls == 1 and pool.balance.available.tokens == 96
        assert pool.observe("a", known, final=True) is pool


def test_count_axes_refuse_storage_overflow_instead_of_truncating():
    from pydantic import ValidationError

    largest = (1 << 63) - 1
    for axis in ("calls", "tokens"):
        bound = Resources(**{axis: largest})
        assert getattr(bound, axis) == largest
        with pytest.raises(ValidationError):
            Resources(**{axis: largest + 1})
        with pytest.raises(ValidationError):
            bound + Resources(**{axis: 1})
