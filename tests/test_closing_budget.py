"""The frozen closing partition and candidate-bound review keep their original lessons."""

from datetime import timedelta
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from forge.budget.projection import pool_projection
from forge.domain.budget import BudgetExhausted, Observation, Pool, Resources, partition
from forge.domain.run_spec import RunPolicy
from forge.engine.delivery_values import ClosingWork
from forge.port_values import Applied, RepoRef, ReviewRef
from forge.providers.fake import Blob
from forge.providers.fake.state import commit_value
from forge.store.budgets import BudgetStore
from forge.store.command_runs import CommandRuns
from forge.store.models_budget import EngineBudgetAmendment
from forge.store import models_review  # noqa: F401 — register current reactive tables before fixtures.
from forge.store.models_execution import EngineCandidate
from forge.store.runs import RunStore
from tests.native_budget_lesson_fixture import resources
from tests import test_budget_engine as budget_fixtures
from tests.test_budget_engine import prepare
from tests.test_domain_approved_input import spec
from tests.test_engine_delivery import (
    case as case,
    check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)

budget_case = budget_fixtures.case


def policy_document():
    frozen = spec()
    return {name: frozen.model_dump(mode="json")[name] for name in RunPolicy.model_fields}


def report_pool():
    pool = Pool(identity="run:closing", capacity=resources(20, 20000, "10"))
    for key in ("settled", "partial", "unknown"):
        pool = pool.reserve(key, resources(), Resources())
    return (
        pool.observe("settled", Observation(usd=Decimal(".5"), calls=1, tokens=10), final=True)
        .observe("partial", Observation(usd=Decimal(".25"), calls=None, tokens=20), final=False)
        .observe("unknown", None, final=False)
    )


async def closing(delivery):
    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha)
    assert (await delivery["env"].runner.tick()).completed == 1
    return run, frozen, sha


async def amendment_rows(budget_case):
    async with budget_case["factory"]() as session:
        return list((await session.scalars(select(EngineBudgetAmendment))).all())


def test_the_absolute_policy_field_wins_over_the_fraction():
    document = policy_document()
    document["closing_reserve"]["usd"] = "0.60"
    frozen = RunPolicy.model_validate_json(json.dumps(document))
    assert frozen.closing_reserve.usd == Decimal(".60")
    document["closing_fraction"] = "0.9"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunPolicy.model_validate_json(json.dumps(document))


def test_the_fraction_record_resolves_against_the_cap():
    # A fraction is resolved by the owner before freeze; only exact amounts enter the contract.
    document = policy_document()
    document["closing_reserve"]["usd"] = str(Decimal(document["limits"]["usd"]) * Decimal(".2"))
    assert RunPolicy.model_validate_json(json.dumps(document)).closing_reserve.usd == 2


def test_the_default_fraction_is_provisional_and_documented():
    # The new closed contract has no provisional ambient default.
    document = policy_document()
    del document["closing_reserve"]
    with pytest.raises(ValidationError, match="closing_reserve"):
        RunPolicy.model_validate_json(json.dumps(document))


def test_no_cap_and_no_absolute_field_reserves_nothing_honestly():
    document = policy_document()
    del document["limits"], document["closing_reserve"]
    with pytest.raises(ValidationError) as refused:
        RunPolicy.model_validate_json(json.dumps(document))
    assert {error["loc"][0] for error in refused.value.errors()} == {"limits", "closing_reserve"}


def test_the_coder_ceiling_withholds_the_closing_reserve():
    pools = partition(resources(10, 1000, "1"), resources(2, 200, ".60"))
    pool = Pool(identity="implementation", capacity=pools.implementation)
    pool = pool.reserve("spent", resources(1, 10, ".35"), Resources()).observe(
        "spent", Observation(usd=Decimal(".35"), calls=1, tokens=10), final=True
    )
    with pytest.raises(BudgetExhausted):
        pool.reserve("coding", resources(1, 10, ".10"), resources(1, 10, ".10"))
    assert pools.closing.usd == Decimal(".60") and pool.balance.available.usd == Decimal(".05")


def test_the_closing_review_fits_while_its_reserve_is_intact():
    frozen = spec()
    pool = Pool(
        identity="closing", capacity=partition(frozen.limits, frozen.closing_reserve).closing
    )
    assert (
        pool.reserve("review", frozen.closing_reserve, resources(1, 1, ".01")).balance.held
        == frozen.closing_reserve
    )


def test_a_consumed_reserve_does_not_cover_the_review():
    pool = Pool(identity="closing", capacity=resources()).reserve(
        "unknown", resources(), Resources()
    )
    pool = pool.observe("unknown", None, final=False)
    with pytest.raises(BudgetExhausted):
        pool.reserve("another-review", resources(1, 1, ".01"), resources(1, 1, ".01"))


def test_the_five_report_fields_stay_distinguishable():
    view = pool_projection(report_pool())
    assert view["settled"]["usd"] == "0.5"
    assert view["known_accrued_lower_bound"]["usd"] == "0.25"
    assert view["held"]["usd"] == "4"
    assert view["available"]["usd"] == "5.5"
    assert view["unknown_axes"]["usd"] == 1
    assert view["reservation_states"] == {"settled": 1, "accrued": 1, "unknown": 1}


def test_the_report_names_the_unresolved_upper_bound():
    view = pool_projection(report_pool())
    unknown = view["reservations"][-1]
    assert unknown["observed"] is None and unknown["allowance"]["usd"] == "2"
    assert view["unknown_axes"] == {"usd": 1, "calls": 2, "tokens": 1}


def test_the_report_separates_settled_accrued_and_retained_liability():
    view = pool_projection(report_pool())
    assert view["settled"] == {"usd": "0.5", "calls": 1, "tokens": 10}
    assert view["known_accrued_lower_bound"] == {"usd": "0.25", "calls": 0, "tokens": 20}
    assert view["unknown_axes"]["calls"] == 2  # zero is a labelled lower bound, not a known count.
    assert view["held"]["usd"] == "4"


def test_the_p01_partial_never_releases_the_envelope_in_the_closing_report():
    pool = Pool(identity="closing", capacity=resources(20, 1000, "10"))
    pool = pool.reserve("earlier", resources(8, 100, "8"), Resources()).observe(
        "earlier", Observation(usd=Decimal("8"), calls=8, tokens=100), final=True
    )
    pool = pool.reserve("partial", resources(2, 100, "2"), Resources()).observe(
        "partial", Observation(usd=Decimal(".5"), calls=1, tokens=10), final=False
    )
    assert pool.balance.held.usd == 2
    with pytest.raises(BudgetExhausted):
        pool.reserve("next", resources(1, 1, "1"), resources(1, 1, "1"))
    final = pool.observe("partial", Observation(usd=Decimal(".5"), calls=1, tokens=10), final=True)
    assert final.balance.settled.usd == Decimal("8.5") and final.balance.available.usd == Decimal(
        "1.5"
    )
    assert (
        final.observe("partial", Observation(usd=Decimal(".5"), calls=1, tokens=10), final=True)
        == final
    )


async def test_review_only_continuation_repeats_only_the_review(delivery):
    run, _, sha = await closing(delivery)
    env = delivery["env"]
    before = [name for name, _ in env.fake.calls].count("commit")
    assert (await env.runner.tick()).completed == 1
    assert len(delivery["reviewer"].calls) == 1
    assert (await env.runner.tick()).completed == 1
    assert [name for name, _ in env.fake.calls].count("commit") == before
    assert (await env.runs.load(run.id)).state.value == "ready_for_human"
    assert delivery["reviewer"].calls[0][2] == sha


async def test_a_moved_candidate_invalidates_the_shortcut_typed(delivery):
    run, _, sha = await closing(delivery)
    env = delivery["env"]
    repo = env.fake.repo(env.intent.task.subject.repo)
    moved = commit_value({"a.py": Blob(b"human\n")}, sha, "human")
    repo.commits[moved.sha], repo.branches["forge/" + run.id] = moved, moved.sha
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "review_stale:branch_drift"
    assert delivery["reviewer"].calls == [] and repo.branches["forge/" + run.id] == moved.sha


async def test_a_moved_tested_identity_invalidates_the_shortcut_typed(delivery):
    run, _, _ = await closing(delivery)
    async with delivery["env"].factory.begin() as session:
        row = await session.scalar(select(EngineCandidate))
        evidence = json.loads(row.validation_json)
        evidence["verification"]["tested_sha"] = "c" * 40
        row.validation_json = json.dumps(evidence)
    assert (await delivery["env"].runner.tick()).parked == 1
    assert (await delivery["env"].runs.load(run.id)).reason == "review_verification_stale"
    assert delivery["reviewer"].calls == []


async def test_only_an_explicit_budget_decision_opens_the_shortcut(delivery):
    run, _, _ = await closing(delivery)
    # A completed step is never re-dispatched by another ordinary worker tick.
    assert (await delivery["env"].runner.tick()).completed == 1
    assert (await delivery["env"].runner.tick()).completed == 1
    assert (await delivery["env"].runner.tick()).claimed == 0
    assert len(delivery["reviewer"].calls) == 1


async def test_a_top_up_requires_an_amount_and_a_reason(budget_case):
    await prepare(budget_case)
    for amount, reason in ((Resources(), "reason"), (resources(1, 0, "0"), " ")):
        with pytest.raises(ValueError):
            await budget_case["service"].amend(
                budget_case["lease"], "implementation", "command", amount, "owner", reason
            )
    assert not await amendment_rows(budget_case)


async def test_a_replayed_top_up_adds_its_amount_exactly_once(budget_case):
    await prepare(budget_case)
    for _ in range(2):
        assert isinstance(
            await budget_case["service"].amend(
                budget_case["lease"],
                "implementation",
                "native:42:1",
                resources(1, 0, ".5"),
                "owner",
                "closing",
            ),
            Applied,
        )
    pool = await budget_case["store"].load(budget_case["run"].id, "implementation")
    assert len(pool.amendments) == 1 and pool.reservations[0].allowance.usd == Decimal("2.5")
    assert budget_case["native"].writes.count("/key/update") == 1


async def test_the_ledger_rebuilds_from_recorded_evidence(budget_case):
    await prepare(budget_case)
    await budget_case["service"].amend(
        budget_case["lease"],
        "implementation",
        "native:42:1",
        resources(1, 0, ".25"),
        "owner",
        "closing",
    )
    rebuilt = BudgetStore(budget_case["factory"])
    pool = await rebuilt.load(budget_case["run"].id, "implementation")
    assert pool.amendments[0].command_id == "native:42:1"
    assert pool.reservations[0].allowance.usd == Decimal("2.25")


async def test_the_amendment_ledger_projects_both_views_of_one_table(budget_case):
    await prepare(budget_case)
    await budget_case["service"].amend(
        budget_case["lease"],
        "implementation",
        "one",
        resources(1, 0, ".5"),
        "human:alice",
        "review",
    )
    (row,) = await amendment_rows(budget_case)
    assert row.state == "applied" and row.actor == "human:alice" and row.reason == "review"
    assert Resources.model_validate_json(row.target_json).usd == Decimal("2.5")
    pool = await budget_case["store"].load(budget_case["run"].id, "implementation")
    assert pool.amendments[0].amount.usd == Decimal(".5")


async def test_the_amendment_ledger_over_no_rows_is_the_empty_ledger(budget_case):
    assert await amendment_rows(budget_case) == []


def test_the_partition_sizes_each_numeric_axis_under_the_versioned_rule():
    frozen = spec()
    pools = partition(frozen.limits, frozen.closing_reserve)
    assert pools.closing == resources(2, 1000, "2")
    assert pools.implementation == resources(18, 9000, "8")
    assert frozen.limits.wallclock_seconds == 600


def test_the_partition_never_takes_the_whole_axis_or_partitions_units():
    total = resources(4, 1, "10")
    protected = resources(3, 0, "2")
    result = partition(total, protected)
    assert result.implementation == resources(1, 1, "8")
    with pytest.raises(ValueError):
        partition(total, resources(5, 0, "2"))


def test_the_authority_ttl_degrades_to_the_default_never_to_forever():
    # Missing or non-aware deadlines now refuse; no ambient fallback can mean forever.
    from forge.engine.delivery_values import DeliverySettings
    from datetime import datetime, timezone

    value = dict(
        candidate_sha="a" * 40,
        review=ReviewRef(
            repo=RepoRef(
                identity=spec().plan.steps[0].citations[0].repository, full_name="owner/repo"
            ),
            number=1,
        ),
        published_at=datetime.now(timezone.utc),
        settings=DeliverySettings(repair_minimum=Resources(), closing_minimum=Resources()),
    )
    with pytest.raises(ValidationError, match="expires_at"):
        ClosingWork.model_validate(value)
    with pytest.raises(ValidationError):
        ClosingWork.model_validate({**value, "expires_at": datetime.now()})


async def test_an_expired_authority_invalidates_the_shortcut_before_paid_review(delivery):
    run, _, _ = await closing(delivery)
    env = delivery["env"]
    env.clock.instant += timedelta(seconds=301)
    env.fake.now = env.clock.now()
    # Verification remains fresh while the independent fixed closing deadline expires.
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "closing_deadline_expired"
    assert delivery["reviewer"].calls == []


async def test_missing_verification_invalidates_the_shortcut_before_paid_review(delivery):
    run, _, _ = await closing(delivery)
    async with delivery["env"].factory.begin() as session:
        row = await session.scalar(select(EngineCandidate))
        evidence = json.loads(row.validation_json)
        evidence["verification"]["status"] = "unknown"
        row.validation_json = json.dumps(evidence)
    assert (await delivery["env"].runner.tick()).parked == 1
    assert (await delivery["env"].runs.load(run.id)).reason == "review_verification_stale"
    assert delivery["reviewer"].calls == []


def test_the_r40_04_observability_names_exist():
    view = pool_projection(report_pool())
    assert {
        "held",
        "settled",
        "available",
        "shortfall",
        "known_accrued_lower_bound",
        "unknown_axes",
        "reservation_states",
        "reservations",
    } <= view.keys()


async def test_durable_receipts_fold_into_the_report(budget_case):
    await prepare(budget_case)
    assert await budget_case["service"].finish(budget_case["lease"], "implementation") == "accrued"
    pool = await budget_case["store"].load(budget_case["run"].id, "implementation")
    snapshot = json.loads(
        await CommandRuns(budget_case["factory"]).snapshot(
            await RunStore(budget_case["factory"]).load(budget_case["run"].id)
        )
    )
    view = snapshot["budgets"]["implementation"]
    assert view["held"]["usd"] == "2" and view["unknown_axes"] == {"calls": 1, "tokens": 1}
    assert view["reservations"][0]["observed"] == {"usd": "0", "calls": None, "tokens": None}
    assert view == pool_projection(pool)
