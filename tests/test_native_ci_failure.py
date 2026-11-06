"""Only positive current native test proof can authorize another bounded implementation."""

from datetime import timedelta
import json

import httpx
import pytest

from forge.domain.states import Handler, State
from forge_protocol import RunIdentity
from forge.engine.verify import VerifyHandler
from forge.port_values import ReadUnavailable
from forge.verification.failure_proof import junit_failure, gitlab_failure
from tests.ci_failure_fixture import JUNIT, NativeCI
from tests.test_engine_delivery import (
    delivery as delivery,
    case as case,
    env as env,
    settings as settings,
    published,
)
from tests.test_github_reads import app_key as app_key


async def native(delivery, app_key):
    env = delivery["env"]
    run, frozen, pending, sha = await published(delivery)
    wire = NativeCI(env.intent.task.subject.repo, sha, env.clock, app_key)
    env.runner.handlers[Handler.VERIFY] = VerifyHandler(
        delivery["store"],
        delivery["delivery_store"],
        env.fake,
        env.fake,
        wire.ci,
        delivery["budgets"],
        env.clock,
    )
    return env, run, frozen, sha, wire


async def test_concrete_native_positive_failure_reaches_exact_engine_repair(delivery, app_key):
    env, run, frozen, sha, wire = await native(delivery, app_key)
    try:
        checks = await wire.ci.checks_for(wire.repo, sha)
        assert checks[0].failure_kind == "code" and checks[0].native_id == "901"
        assert checks[0].native_run_id == "700" and checks[0].tested_sha == sha
        assert "assert False" not in checks[0].evidence
        assert (await env.runner.tick()).completed == 1
        current = await env.runs.load(run.id)
        assert (current.state, current.attempt, current.generation) == (State.RUNNING, 2, 2)
        step = await env.steps.get(f"{run.id}:2:2:repair")
        repair = json.loads(step.payload_json)["repair"]
        assert (
            repair["candidate_sha"] == sha and repair["original_base_sha"] == frozen.spec.base_sha
        )
        assert (await env.approvals.load(run.id)).approved == frozen.approved
        assert len(env.fake.drafts) == 1
        for _ in range(2):
            assert (await env.runner.tick()).completed == 1
        assert len(env.fake.lanes) == 2
        second = await delivery["store"].pending(
            RunIdentity(forge_run_id=run.id, attempt=2, generation=2)
        )
        assert second.dispatch.execution_base_sha == sha

    finally:
        await wire.close()


@pytest.mark.parametrize(
    "failure", ["missing", "disabled", "setup", "stale", "future", "error", "foreign"]
)
async def test_unproven_native_red_never_reserves_or_dispatches_repair(delivery, app_key, failure):
    env, run, _, sha, wire = await native(delivery, app_key)
    github = wire.repo.identity.provider == "github"
    try:
        if failure == "missing":
            wire.routes["check-runs/901" if github else "pipelines/700/test_report_summary"] = (
                httpx.Response(404)
            )
        elif failure == "disabled":
            wire.ci.failure_jobs = frozenset()
        elif failure == "setup":
            wire.job["conclusion" if github else "failure_reason"] = (
                "startup_failure" if github else "runner_system_failure"
            )
        elif failure in {"stale", "future"}:
            stamp = (
                env.clock.now() + timedelta(seconds=1 if failure == "future" else -3601)
            ).isoformat()
            wire.job["completed_at" if github else "finished_at"] = stamp
            wire.check["completed_at"] = stamp
        elif failure == "error":
            wire.check["output"]["text"] = JUNIT.replace("<failure", "<error").replace(
                "</failure>", "</error>"
            )
            wire.summary["test_suites"][0]["suite_error"] = "report collection failed"
        elif failure == "foreign":
            wire.check["check_suite"]["id"] = 801
            wire.summary["test_suites"][0]["build_ids"] = [900]
        before = await delivery["budgets"].load(run.id, "implementation")
        assert (await wire.ci.checks_for(wire.repo, sha))[0].failure_kind != "code"
        assert (await env.runner.tick()).parked == 1
        after = await env.runs.load(run.id)
        assert (after.attempt, after.generation) == (1, 1)
        assert after.reason == "verification_non_code_failure"
        assert await delivery["budgets"].load(run.id, "implementation") == before
        assert len(env.fake.lanes) == 1
    finally:
        await wire.close()


@pytest.mark.parametrize("change", ["job_sha", "job_attempt", "job_pipeline"])
async def test_native_identity_mismatch_is_unavailable_not_a_repair(delivery, app_key, change):
    env, run, _, sha, wire = await native(delivery, app_key)
    try:
        if wire.repo.identity.provider == "github":
            wire.job[
                {"job_sha": "head_sha", "job_attempt": "run_attempt", "job_pipeline": "run_id"}[
                    change
                ]
            ] = "f" * 40 if change == "job_sha" else 9
        elif change == "job_attempt":
            wire.job["id"] = 902
            assert (await wire.ci.checks_for(wire.repo, sha))[0].failure_kind == "unknown"
            return
        else:
            wire.job["commit" if change == "job_sha" else "pipeline"]["id"] = (
                "f" * 40 if change == "job_sha" else 9
            )
        with pytest.raises(ReadUnavailable):
            await wire.ci.checks_for(wire.repo, sha)
        assert (await env.runs.load(run.id)).attempt == 1
    finally:
        await wire.close()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "<testsuite/>",
        JUNIT.replace('failures="1"', 'failures="0"'),
        JUNIT.replace('tests="1"', 'tests="2"'),
        JUNIT.replace('errors="0"', 'errors="1"'),
        '<!DOCTYPE x [<!ENTITY e "boom">]>' + JUNIT,
        JUNIT.replace("<failure", "<error").replace("</failure>", "</error>"),
        JUNIT + "x",
        JUNIT.replace("<testcase ", '<testcase errors="0" ', 1).replace(
            'name="test_rejects_trailing"', 'name=""'
        ),
    ],
)
def test_xml_proof_refuses_empty_errors_entities_counts_and_malformed_data(bad):
    assert junit_failure(bad) is None


def test_positive_xml_preserves_only_bounded_failed_case_names_and_digest():
    proof = junit_failure(JUNIT)
    assert proof.tests == proof.failures == 1 and len(proof.digest) == 64
    assert proof.names == ("tests.test_parser.test_rejects_trailing",)
    assert "assert False" not in proof.encoded()
    assert gitlab_failure({"test_suites": [], "total": {}}, "901") is None


async def test_native_code_proof_does_not_override_human_head_movement(delivery, app_key):
    env, run, _, sha, wire = await native(delivery, app_key)
    try:
        assert (await wire.ci.checks_for(wire.repo, sha))[0].failure_kind == "code"
        repo = env.fake.repo(env.intent.task.subject.repo)
        repo.branches["forge/" + run.id] = "f" * 40
        assert (await env.runner.tick()).parked == 1
        assert (await env.runs.load(run.id)).reason == "verification_stale:branch_drift"
        assert len(env.fake.lanes) == 1 and repo.branches["forge/" + run.id] == "f" * 40
    finally:
        await wire.close()


@pytest.mark.parametrize(
    "bad", ["wrong_origin", "wrong_check", "wrong_app", "bad_counts", "mixed_jobs", "oversized"]
)
async def test_native_report_cannot_borrow_foreign_identity_or_ambiguous_counts(env, app_key, bad):
    wire = NativeCI(
        env.intent.task.subject.repo, env.intent.snapshot.primary.sha, env.clock, app_key
    )
    try:
        if bad == "wrong_origin":
            wire.job["check_run_url"] = wire.job.get("check_run_url", "").replace(
                "api.github.com", "foreign.invalid"
            )
            wire.summary["test_suites"][0]["build_ids"] = [900]
        elif bad == "wrong_check":
            wire.check["id"] = 902
            wire.summary["test_suites"][0]["build_ids"] = [902]
        elif bad == "wrong_app":
            wire.check["app"]["slug"] = "third-party"
            wire.summary["test_suites"][0]["suite_error"] = "untrusted report"
        elif bad == "bad_counts":
            wire.check["output"]["text"] = JUNIT.replace('failures="1"', 'failures="2"')
            wire.summary["total"]["failed"] = 2
        elif bad == "mixed_jobs":
            wire.check["check_suite"]["id"] = 999
            wire.summary["test_suites"][0]["build_ids"] = [901, 902]
        else:
            wire.check["output"]["text"] = JUNIT + " " * 65535
            wire.summary["irrelevant"] = "x" * 131072
        assert (await wire.ci.checks_for(wire.repo, wire.sha))[0].failure_kind == "unknown"
    finally:
        await wire.close()


def test_failure_jobs_are_explicit_unique_frozen_required_names(tmp_path):
    from pydantic import ValidationError
    from forge.runtime.settings import RuntimeSettings
    from tests.runtime_fixture import configuration

    config, _ = configuration(tmp_path)
    document = config.model_dump(mode="json", by_alias=True)
    repo = document["connections"][0]["repositories"][0]
    repo["verification_failure_jobs"] = ["unit"]
    assert RuntimeSettings.model_validate_json(json.dumps(document)).connections[0].repositories[
        0
    ].verification_failure_jobs == ("unit",)
    for names in (["unit", "unit"], ["setup"], [" unit"], ["unit\n"]):
        repo["verification_failure_jobs"] = names
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate_json(json.dumps(document))


async def test_lane_or_untrusted_workflow_never_supplies_code_failure(env, app_key):
    wire = NativeCI(
        env.intent.task.subject.repo, env.intent.snapshot.primary.sha, env.clock, app_key
    )
    try:
        if wire.repo.identity.provider == "github":
            wire.run["path"] = ".github/workflows/lane.yml"
            checks = await wire.ci.checks_for(wire.repo, wire.sha)
            assert (
                len(checks) == 1
                and checks[0].role == "lane"
                and checks[0].failure_kind == "unknown"
            )
            wire.run["path"] = ".github/workflows/untrusted.yml"
            assert await wire.ci.checks_for(wire.repo, wire.sha) == ()
        else:
            checks = await wire.ci._checks(wire.repo, wire.run, wire.sha, "lane")
            assert checks[0].role == "lane" and checks[0].failure_kind == "unknown"
            wire.run["source"] = "api"
            assert await wire.ci.checks_for(wire.repo, wire.sha) == ()
        assert not any(
            r.url.path.endswith(("/check-runs/901", "/test_report_summary")) for r in wire.calls
        )
    finally:
        await wire.close()


def test_xml_cannot_hide_execution_errors_in_an_unknown_namespace():
    xml = JUNIT.replace("<failure ", '<e:error xmlns:e="urn:unknown" ').replace(
        "</failure>", "</e:error>"
    )
    assert junit_failure(xml) is None


@pytest.mark.parametrize("env", ["github"], indirect=True)
async def test_github_attempt_endpoint_binds_documented_job_without_optional_attempt(env, app_key):
    wire = NativeCI(
        env.intent.task.subject.repo, env.intent.snapshot.primary.sha, env.clock, app_key
    )
    try:
        wire.job.pop("run_attempt")
        checks = await wire.ci.checks_for(wire.repo, wire.sha)
        assert checks[0].attempt == wire.run["run_attempt"] == 1
        assert checks[0].native_id == "901" and checks[0].failure_kind == "code"
        attempts = [
            r for r in wire.calls if r.url.path.endswith("/actions/runs/700/attempts/1/jobs")
        ]
        assert len(attempts) == 1
        wire.job["run_attempt"] = 2
        with pytest.raises(ReadUnavailable, match="check_job_attempt_mismatch"):
            await wire.ci.checks_for(wire.repo, wire.sha)
        wire.job.pop("run_attempt")
        wire.job["run_id"] = 701
        with pytest.raises(ReadUnavailable, match="check_job_attempt_mismatch"):
            await wire.ci.checks_for(wire.repo, wire.sha)
    finally:
        await wire.close()
