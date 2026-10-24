"""Required native check inventories retain strict identity and conflict rules.

XML/TRX ingestion, report bundles, environment qualification, waivers and editorial
revision reuse are retired. Native check outcomes are never presented as proof
that external report files or environment images were inspected.
"""

from datetime import timedelta
from itertools import permutations
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from forge.domain.run_spec import VerificationContract
from forge.domain.states import State
from forge.port_values import CheckResult
from forge.store.command_runs import CommandRuns
from forge.store.models_execution import EngineCandidate
from tests.test_engine_delivery import (
    case as case,
    check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)
from tests.test_runs_verification import NOW, SHA, evaluate, observation


class TestStrictInventoryMatching:
    def test_the_native_required_document_is_closed_frozen_and_round_trips(self):
        inventory = VerificationContract(required_checks=("api", "domain"))
        assert VerificationContract.model_validate_json(inventory.canonical_bytes) == inventory
        assert inventory.digest != VerificationContract(required_checks=("api", "another")).digest
        for obsolete in ("reports", "producer", "waived_conclusions", "environment_profile_digest"):
            document = inventory.model_dump(mode="json") | {obsolete: []}
            with pytest.raises(ValidationError):
                VerificationContract.model_validate_json(json.dumps(document))

    def test_valid_complete_native_coverage_verifies(self):
        checks = (observation("api"), observation("domain", native_id="other"))
        value = evaluate(checks, ("api", "domain"))
        assert value.status == "passed" and value.successful == ("api", "domain")
        assert len(json.loads(value.evidence)["checks"]) == 2

    def test_blank_native_subject_is_refused_and_an_unrelated_success_cannot_fill_it(self):
        document = observation("api").model_dump(mode="json")
        document["tested_sha"] = ""
        with pytest.raises(ValidationError):
            CheckResult.model_validate_json(json.dumps(document))
        value = evaluate((observation("unrelated"), observation("domain")), ("api", "domain"))
        assert value.status == "pending" and value.successful == ("domain",)

    def test_attempt_10_failure_not_hidden_by_attempt_9_success(self):
        checks = (observation(attempt=9), observation(status="failed", attempt=10, native_id="new"))
        for order in permutations(checks):
            value = evaluate(order)
            assert value.status == "failed"
            assert json.loads(value.evidence)["checks"][0]["native_id"] == "new"

    @pytest.mark.parametrize("attempt", ("run-beta", "10", True, 0, -1))
    def test_unproven_ordinal_or_noninteger_attempt_is_refused(self, attempt):
        document = observation().model_dump(mode="json") | {"attempt": attempt}
        with pytest.raises(ValidationError):
            CheckResult.model_validate_json(json.dumps(document))

    def test_duplicate_ids_with_contradictory_outcomes_conflict(self):
        checks = (observation(), observation(status="failed"))
        for order in permutations(checks):
            assert evaluate(order).status == "unknown"

    @pytest.mark.parametrize(
        "field,value", (("tested_sha", "b" * 40), ("role", "lane"), ("name", "unrelated"))
    )
    def test_wrong_candidate_role_or_name_cannot_fill_a_required_obligation(self, field, value):
        changed = observation().model_copy(update={field: value})
        result = evaluate((changed,))
        assert result.status == ("stale" if field == "tested_sha" else "pending")
        assert not result.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)

    def test_each_required_job_binds_its_own_exact_candidate(self):
        checks = (observation("api"), observation("domain", tested_sha="b" * 40))
        for order in permutations(checks):
            assert evaluate(order, ("api", "domain")).status == "stale"

    def test_failed_native_command_is_not_hidden_by_a_passed_report_claim(self):
        failed = observation(status="failed", evidence='{"trx_outcome":"passed","failures":0}')
        assert evaluate((failed,)).status == "failed"

    def test_old_candidate_missing_and_skipped_native_jobs_stay_distinguishable(self):
        assert evaluate((observation(tested_sha="b" * 40),)).status == "stale"
        assert evaluate((), final=True).status == "not_configured"
        assert evaluate((observation(status="skipped"),)).status == "unknown"


class TestApplicability:
    def test_an_exact_diff_digest_cannot_substitute_for_the_published_native_sha(self):
        value = evaluate((observation(),))
        assert not value.verified_for("d" * 64, ("unit",), now=NOW, max_age_seconds=60)
        assert value.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)

    def test_a_changed_required_contract_cannot_reuse_a_narrower_pass(self):
        value = evaluate((observation(),))
        assert not value.verified_for(SHA, ("unit", "domain"), now=NOW, max_age_seconds=60)

    def test_synthetic_merge_and_future_or_stale_evidence_never_count_as_current_source(self):
        for check_value in (
            observation(tested_sha="9" * 40),
            observation(observed_at=NOW + timedelta(seconds=1)),
            observation(observed_at=NOW - timedelta(seconds=61)),
        ):
            assert evaluate((check_value,)).status == "stale"


@pytest.mark.parametrize(
    "mode", ("old_success", "same_attempt_conflict", "newer_failure", "newer_success", "complete")
)
async def test_actual_verifier_uses_current_native_inventory_and_operator_shares_its_decision(
    delivery, mode
):
    env = delivery["env"]
    env.intent = env.intent.model_copy(
        update={
            "policy": env.intent.policy.model_copy(
                update={"verification": VerificationContract(required_checks=("unit", "domain"))}
            )
        }
    )
    run, _, _, sha = await published(delivery)
    first = check(delivery, sha)
    domain = first.model_copy(update={"name": "domain", "native_id": "domain-native"})
    if mode == "old_success":
        values = (first, domain.model_copy(update={"tested_sha": "b" * 40}))
    elif mode == "same_attempt_conflict":
        values = (first, domain, domain.model_copy(update={"status": "failed"}))
    elif mode == "newer_failure":
        values = (
            first,
            domain,
            domain.model_copy(
                update={"status": "failed", "attempt": 10, "native_id": "new-domain"}
            ),
        )
    elif mode == "newer_success":
        values = (
            first,
            domain.model_copy(update={"status": "failed", "attempt": 9}),
            domain.model_copy(update={"attempt": 10, "native_id": "new-domain"}),
        )
    else:
        values = (first, domain)
    env.fake.checks[env.intent.task.subject.repo.identity.digest] = tuple(reversed(values))
    tick = await env.runner.tick()
    if mode in ("complete", "newer_success"):
        assert tick.completed == 1
        for _ in range(2):
            assert (await env.runner.tick()).completed == 1
        expected = State.READY_FOR_HUMAN
    elif mode == "old_success":
        assert tick.rescheduled == 1
        expected = State.VERIFYING
    else:
        assert tick.parked == 1
        expected = State.BLOCKED
    current = await env.runs.load(run.id)
    assert current.state == expected
    view = json.loads(await CommandRuns(env.factory).snapshot(current))
    assert view["state"] == expected.value
    assert bool(delivery["reviewer"].calls) == (expected == State.READY_FOR_HUMAN)
    async with env.factory() as session:
        row = await session.scalar(select(EngineCandidate).where(EngineCandidate.run_id == run.id))
        verdict = json.loads(row.validation_json)["verification"]
        assert verdict["tested_sha"] == sha
        assert verdict["required"] == ["unit", "domain"]
        if expected == State.READY_FOR_HUMAN:
            assert set(verdict["successful"]) == {"unit", "domain"}
