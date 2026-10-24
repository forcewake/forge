"""Sole-engine verification binds exact collection, approval, generation and native CI."""

from datetime import timedelta
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from forge.domain.run_spec import VerificationContract
from forge.domain.states import State
from forge.port_values import ReadUnavailable
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineRunSpec
from tests.test_engine_delivery import (
    advance,
    case as case,
    check,
    completed,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)

from tests.test_github_rest import app_key as app_key, provider as provider


async def ready_input(delivery):
    env = delivery["env"]
    result = await published(delivery)
    check(delivery, result[-1])
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    return result


async def candidate(session, run_id):
    return await session.scalar(
        select(EngineCandidate)
        .where(EngineCandidate.run_id == run_id)
        .order_by(EngineCandidate.attempt.desc())
    )


async def test_the_verdict_names_the_actual_collected_diff_and_frozen_approval(delivery):
    env = delivery["env"]
    run, frozen, pending, sha = await ready_input(delivery)
    async with env.factory() as session:
        row = await candidate(session, run.id)
        manifest = json.loads(row.manifest_json)
        evidence = json.loads(row.validation_json)
        assert manifest["base_sha"] == pending.dispatch.execution_base_sha
        assert manifest["approved_digest"] == frozen.approved.approved_digest
        assert (manifest["forge_run_id"], manifest["attempt"], manifest["generation"]) == (
            run.id,
            1,
            1,
        )
        from hashlib import sha256

        diff = await delivery["api"].blobs.read(manifest["diff_sha256"])
        assert sha256(diff).hexdigest() == manifest["diff_sha256"]
        assert evidence["verification"]["tested_sha"] == sha == row.published_sha
        assert evidence["review"]["candidate_sha"] == sha
        assert evidence["verification"]["required"] == list(
            frozen.spec.verification.required_checks
        )
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN


@pytest.mark.parametrize("corruption", ("digest", "published_sha", "state", "validation"))
async def test_missing_or_changed_published_candidate_refuses_existing_pass(delivery, corruption):
    env = delivery["env"]
    run, _, _, _ = await ready_input(delivery)
    async with env.factory.begin() as session:
        row = await candidate(session, run.id)
        if corruption == "digest":
            row.digest = "0" * 64
        elif corruption == "published_sha":
            row.published_sha = "0" * 40
        elif corruption == "state":
            row.state = "validated"
        else:
            row.validation_json = "{}"
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).state == State.BLOCKED


@pytest.mark.parametrize(
    "mutation",
    (
        "spec",
        "approved_input",
        "verification_subject",
        "verification_time",
        "verification_contract",
        "missing_verification",
        "review_subject",
    ),
)
async def test_recorded_pass_cannot_survive_changed_execution_or_subject_authority(
    delivery, mutation
):
    env = delivery["env"]
    run, _, _, _ = await ready_input(delivery)
    async with env.factory.begin() as session:
        if mutation in ("spec", "approved_input"):
            model = EngineRunSpec if mutation == "spec" else EngineApprovedInput
            row = await session.scalar(select(model).where(model.run_id == run.id))
            value = json.loads(row.document_json)
            value["task_title" if mutation == "spec" else "spec_digest"] = (
                "different" if mutation == "spec" else "0" * 64
            )
            row.document_json = json.dumps(value)
        else:
            row = await candidate(session, run.id)
            value = json.loads(row.validation_json)
            if mutation == "verification_subject":
                value["verification"]["tested_sha"] = "0" * 40
            elif mutation == "verification_time":
                value["verification"]["observed_at"] = (
                    env.clock.now() - timedelta(days=1)
                ).isoformat()
            elif mutation == "verification_contract":
                value["verification"]["required"] = ["other"]
                value["verification"]["successful"] = ["other"]
            elif mutation == "missing_verification":
                value.pop("verification")
            else:
                value["review"]["candidate_sha"] = "0" * 40
            row.validation_json = json.dumps(value)
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).state == State.BLOCKED
    async with env.factory() as session:
        # Refusal retains the historical candidate and the native posted review.
        assert await candidate(session, run.id) is not None
        assert (
            await session.scalar(
                select(EngineEffect).where(
                    EngineEffect.run_id == run.id, EngineEffect.kind == "post_review"
                )
            )
            is not None
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "kind",
        "payload_digest",
        "body",
        "marker",
        "native_subject",
        "native_kind",
        "owner",
        "state",
        "display",
    ),
)
async def test_readiness_requires_its_exact_native_review_receipt_and_original_owner(
    delivery, mutation
):
    env = delivery["env"]
    run, _, _, _ = await ready_input(delivery)
    async with env.factory.begin() as session:
        effect = await session.scalar(
            select(EngineEffect).where(
                EngineEffect.run_id == run.id, EngineEffect.kind == "post_review"
            )
        )
        if mutation == "kind":
            effect.kind = "post_comment"
        elif mutation == "payload_digest":
            effect.payload_digest = "0" * 64
        elif mutation in ("body", "marker"):
            document = json.loads(effect.payload_json)
            document["body_json" if mutation == "body" else "marker"] = "another"
            effect.payload_json = json.dumps(document)
        elif mutation in ("native_subject", "native_kind", "display"):
            document = json.loads(effect.outcome_json)
            if mutation == "native_subject":
                document["value"]["subject"]["number"] += 1
            elif mutation == "native_kind":
                document["value"]["kind"] = "issue_comment"
            else:
                document["value"]["subject"]["repo"]["full_name"] = "renamed/native-project"
            effect.outcome_json = json.dumps(document)
        elif mutation == "owner":
            effect.step_id = (
                await session.scalar(
                    select(EngineStep).where(
                        EngineStep.run_id == run.id, EngineStep.handler == "verify"
                    )
                )
            ).id
        else:
            effect.state = "unknown"
    tick = await env.runner.tick()
    current = await env.runs.load(run.id)
    if mutation == "display":
        assert tick.completed == 1 and current.state == State.READY_FOR_HUMAN
    else:
        assert tick.parked == 1 and current.reason == "readiness_review_unpublished"


@pytest.mark.parametrize("stage", ("review", "ready_before_ci", "ready_during_ci"))
async def test_head_drift_during_closing_keeps_historical_pass_but_never_readies(
    delivery, monkeypatch, stage
):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    assert (await env.runner.tick()).completed == 1

    def move():
        repo = env.fake.repo(env.intent.task.subject.repo)
        human = commit_value({"a.py": Blob(b"human\n")}, sha, "human")
        repo.commits[human.sha], repo.branches["forge/" + run.id] = human, human.sha

    if stage == "review":
        original = delivery["reviewer"].review

        async def during(*args, **kwargs):
            result = await original(*args, **kwargs)
            move()
            return result

        monkeypatch.setattr(delivery["reviewer"], "review", during)
    else:
        assert (await env.runner.tick()).completed == 1
        if stage == "ready_before_ci":
            move()
        else:
            original = env.fake.checks_for

            async def during_ci(*args, **kwargs):
                result = await original(*args, **kwargs)
                move()
                return result

            monkeypatch.setattr(env.fake, "checks_for", during_ci)
    assert (await env.runner.tick()).parked == 1
    assert "branch_drift" in (await env.runs.load(run.id)).reason
    async with env.factory() as session:
        history = json.loads((await candidate(session, run.id)).validation_json)
        assert history["verification"]["tested_sha"] == sha


@pytest.mark.parametrize("second", ("missing", "skipped", "old", "passed"))
async def test_frozen_secondary_required_job_cannot_disappear_behind_first_green(delivery, second):
    env = delivery["env"]
    env.intent = env.intent.model_copy(
        update={
            "policy": env.intent.policy.model_copy(
                update={"verification": VerificationContract(required_checks=("unit", "secondary"))}
            )
        }
    )
    run, _, _, sha = await published(delivery)
    primary = check(delivery, sha)
    if second != "missing":
        secondary = primary.model_copy(
            update={
                "name": "secondary",
                "native_id": "job-2",
                "status": "skipped" if second == "skipped" else "passed",
                "tested_sha": "f" * 40 if second == "old" else sha,
            }
        )
        env.fake.checks[env.intent.task.subject.repo.identity.digest] = (primary, secondary)
    tick = await env.runner.tick()
    if second == "passed":
        assert tick.completed == 1
        for _ in range(2):
            assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN
    else:
        assert tick.parked == 1 if second == "skipped" else tick.rescheduled == 1
        assert not delivery["reviewer"].calls
        assert (await env.runs.load(run.id)).state != State.READY_FOR_HUMAN


@pytest.mark.parametrize("role", ("lane", "unknown"))
async def test_green_harness_or_unknown_execution_is_never_independent_verification(delivery, role):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    if role == "unknown":
        with pytest.raises(ValidationError):
            check(delivery, sha, role=role)
    else:
        check(delivery, sha, role=role)
    assert (await env.runner.tick()).rescheduled == 1
    assert not delivery["reviewer"].calls
    assert (await env.runs.load(run.id)).state == State.VERIFYING


@pytest.mark.parametrize("failure_kind", ("infrastructure", "configuration", "unknown"))
async def test_native_non_code_failure_keeps_code_generation_and_budget_untouched(
    delivery, failure_kind
):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    before = await delivery["budgets"].load(run.id, "implementation")
    check(delivery, sha, status="failed", failure_kind=failure_kind)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert (current.attempt, current.generation) == (1, 1)
    assert current.reason == "verification_non_code_failure"
    assert await delivery["budgets"].load(run.id, "implementation") == before
    assert len(env.fake.lanes) == 1


@pytest.mark.parametrize("phase", ("before_read", "during_read"))
async def test_transport_retries_are_bounded_by_verification_deadline_without_code_repair(
    delivery, monkeypatch, phase
):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    if phase == "before_read":
        env.fake.fail_next("checks_for", Fault(http_5xx=True))
        assert (await env.runner.tick()).rescheduled == 1
        advance(delivery, 60)
        env.fake.fail_next("checks_for", Fault(http_5xx=True))
    else:
        advance(delivery, 10)

        async def crosses_deadline(*args):
            advance(delivery, 55)
            raise ReadUnavailable("native_read_timed_out", "synthetic:transport")

        monkeypatch.setattr(env.fake, "checks_for", crosses_deadline)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.reason == "verification_unavailable_timeout"
    assert (current.attempt, current.generation) == (1, 1)
    assert not delivery["reviewer"].calls and len(env.fake.lanes) == 1


async def test_repair_generation_retains_history_and_requires_new_candidate_checks(delivery):
    from forge_protocol import RunIdentity

    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha, status="failed", failure_kind="code")
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    pending = await delivery["store"].pending(
        RunIdentity(forge_run_id=run.id, attempt=2, generation=2)
    )
    await completed(delivery, pending, old=b"new\n", new=b"repaired\n")
    assert (await env.runner.tick()).completed == 1
    check(delivery, sha)
    assert (await env.runner.tick()).rescheduled == 1
    assert not delivery["reviewer"].calls
    async with env.factory() as session:
        history = list(
            await session.scalars(
                select(EngineCandidate)
                .where(EngineCandidate.run_id == run.id)
                .order_by(EngineCandidate.generation)
            )
        )
        assert [row.generation for row in history] == [1, 2]
        assert json.loads(history[0].validation_json)["verification"]["tested_sha"] == sha
        assert json.loads(history[1].validation_json)["verification"]["status"] == "pending"


@pytest.mark.parametrize(
    "conclusion, expected",
    (("failure", "unknown"), ("timed_out", "infrastructure"), ("action_required", "configuration")),
)
async def test_native_github_execution_identity_and_typed_failure_survive_translation(
    provider, conclusion, expected
):
    from tests.test_github_rest import REPO, SHA, run
    from forge.verification.checks import code_failure, verdict

    _, _, ci, wire, _ = provider
    wire.routes["actions/runs"] = {
        "total_count": 1,
        "workflow_runs": [
            run(id=701, run_attempt=10, path=".github/workflows/ci.yml", event="push")
        ],
    }
    job = {
        "id": 902,
        "run_id": 701,
        "run_attempt": 10,
        "head_sha": SHA,
        "name": "unit",
        "status": "completed",
        "conclusion": conclusion,
    }
    wire.routes["actions/runs/701/attempts/10/jobs"] = {"total_count": 1, "jobs": [job]}
    checks = await ci.checks_for(REPO, SHA)
    (value,) = checks
    assert (value.native_id, value.attempt, value.tested_sha, value.role) == (
        "902",
        10,
        SHA,
        "verification",
    )
    assert value.failure_kind == expected
    decision = verdict(checks, SHA, ("unit",), now=value.observed_at, max_age_seconds=60)
    assert not code_failure(checks, decision)
    job["head_sha"] = "f" * 40
    with pytest.raises(ReadUnavailable):
        await ci.checks_for(REPO, SHA)


@pytest.mark.parametrize(
    "reason, expected",
    (
        ("script_failure", "unknown"),
        ("runner_system_failure", "infrastructure"),
        ("config_error", "configuration"),
    ),
)
async def test_native_gitlab_execution_identity_and_typed_failure_survive_translation(
    reason, expected
):
    from tests import gitlab_engine_fixture as f
    from forge.verification.checks import code_failure, verdict

    wire = f.Wire()
    api, _, _, _, ci, _ = f.adapters(wire)
    try:
        wire.routes["pipelines/700"]["source"] = "push"
        wire.routes["pipelines/700/variables"] = []
        job = wire.routes["pipelines/700/jobs"][0]
        job.update(id=711, name="unit", status="failed", failure_reason=reason)
        checks = await ci.checks_for(f.REPO, f.SHA)
        (value,) = checks
        assert (value.native_id, value.attempt, value.tested_sha, value.role) == (
            "711",
            711,
            f.SHA,
            "verification",
        )
        assert value.failure_kind == expected
        decision = verdict(checks, f.SHA, ("unit",), now=value.observed_at, max_age_seconds=60)
        assert not code_failure(checks, decision)
        job["pipeline"]["sha"] = f.OTHER
        with pytest.raises(ReadUnavailable):
            await ci.checks_for(f.REPO, f.SHA)
    finally:
        await api.transport.close()
