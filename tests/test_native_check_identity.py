"""A successful retry belongs to one workflow run or pipeline, never another source."""

from datetime import timedelta

import pytest

from forge.verification.checks import verdict
from tests.test_verification_checks import NOW, SHA, check, reduce
from tests.test_github_rest import app_key as app_key, provider as provider, run, REPO


@pytest.mark.parametrize("scope", ("workflow", "run"))
@pytest.mark.parametrize("order", (False, True))
def test_same_named_higher_attempt_success_cannot_mask_another_native_source(scope, order):
    failing = check(status="failed", workflow_path=".github/workflows/ci.yml")
    passing = check(
        native_id="check-2",
        attempt=2,
        workflow_path=".github/workflows/decoy.yml"
        if scope == "workflow"
        else ".github/workflows/ci.yml",
    )
    if scope == "run":
        passing = passing.model_copy(update={"native_run_id": "run-2"})
    values = (failing, passing)
    assert reduce(*(values[::-1] if order else values)).status == "unknown"


def test_attempt_order_inside_one_source_cannot_be_replaced_by_read_timestamp():
    old = check(status="passed", observed_at=NOW)
    current = check(
        native_id="check-2", attempt=2, status="pending", observed_at=NOW - timedelta(seconds=1)
    )
    assert reduce(old, current).status == "pending"
    assert reduce(old, current.model_copy(update={"status": "passed"})).status == "passed"


@pytest.mark.parametrize("field", ("workflow_path", "native_run_id"))
def test_missing_native_source_identity_cannot_be_reconstructed_from_opaque_evidence(field):
    from pydantic import ValidationError
    from forge.port_values import CheckResult

    value = check().model_dump(mode="json")
    value["evidence"] = '{"workflow_path":"ci.yml","native_run_id":"run-1"}'
    value.pop(field)
    with pytest.raises(ValidationError):
        CheckResult.model_validate_json(__import__("json").dumps(value))


@pytest.mark.parametrize("scope", ("workflow", "run"))
@pytest.mark.parametrize("order", (False, True))
async def test_github_native_jobs_retain_workflow_and_run_identity_before_reduction(
    provider, scope, order
):
    _, _, ci, wire, _ = provider
    ci.verification_paths = frozenset({".github/workflows/ci.yml", ".github/workflows/decoy.yml"})
    failing = run(id=701, path=".github/workflows/ci.yml", event="push", run_attempt=1)
    passing = run(
        id=702,
        path=".github/workflows/decoy.yml" if scope == "workflow" else ".github/workflows/ci.yml",
        event="push",
        run_attempt=2,
    )
    documents = [failing, passing]
    wire.routes["actions/runs"] = {
        "total_count": 2,
        "workflow_runs": documents[::-1] if order else documents,
    }
    for document, conclusion in ((failing, "failure"), (passing, "success")):
        wire.routes[f"actions/runs/{document['id']}/attempts/{document['run_attempt']}/jobs"] = {
            "total_count": 1,
            "jobs": [
                {
                    "id": document["id"] + 1000,
                    "run_id": document["id"],
                    "run_attempt": document["run_attempt"],
                    "head_sha": SHA,
                    "name": "unit",
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ],
        }
    checks = await ci.checks_for(REPO, SHA)
    assert {item.native_run_id for item in checks} == {"701", "702"}
    assert {item.attempt for item in checks} == {1, 2}
    assert (
        verdict(checks, SHA, ("unit",), now=ci.clock.now(), max_age_seconds=60).status == "unknown"
    )
    assert all(
        request.method == "GET" or request.url.path.endswith("/access_tokens")
        for request in wire.calls
    )


@pytest.mark.parametrize("order", (False, True))
async def test_gitlab_native_pipeline_identity_prevents_later_job_id_masking_failure(order):
    from tests import gitlab_engine_fixture as f

    wire = f.Wire()
    api, _, _, _, ci, _ = f.adapters(wire)
    values = [f.pipeline(700, source="push"), f.pipeline(800, source="push")]
    wire.routes["pipelines"] = values[::-1] if order else values
    for pipeline, status in ((values[0], "failed"), (values[1], "success")):
        ident = pipeline["id"]
        wire.routes[f"pipelines/{ident}"] = pipeline
        wire.routes[f"pipelines/{ident}/variables"] = []
        wire.routes[f"pipelines/{ident}/jobs"] = [
            {
                "id": ident + 1,
                "name": "unit",
                "status": status,
                "pipeline": {"id": ident, "sha": f.SHA},
                "commit": {"id": f.SHA},
            }
        ]
    try:
        checks = await ci.checks_for(f.REPO, f.SHA)
        assert {(item.native_run_id, item.attempt) for item in checks} == {
            ("700", 701),
            ("800", 801),
        }
        assert all(item.workflow_path == ".gitlab-ci.yml" for item in checks)
        assert (
            verdict(checks, f.SHA, ("unit",), now=ci.clock.now(), max_age_seconds=60).status
            == "unknown"
        )
        assert all(request.method == "GET" for request in wire.calls)
    finally:
        await api.transport.close()


from tests.test_engine_delivery import (  # noqa: E402
    case as case,
    check as engine_check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)


@pytest.mark.parametrize("scope", ("workflow", "run"))
async def test_distinct_native_sources_cannot_start_closing_or_repair_in_the_engine(
    delivery, scope
):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    failing = engine_check(delivery, sha, status="failed", failure_kind="code")
    passing = failing.model_copy(
        update={
            "status": "passed",
            "native_id": "different-job",
            "attempt": 2,
            **(
                {"workflow_path": "different-ci.yml"}
                if scope == "workflow"
                else {"native_run_id": "different-run"}
            ),
        }
    )
    env.fake.checks[env.intent.task.subject.repo.identity.digest] = (failing, passing)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.reason == "verification_unknown"
    assert (current.attempt, current.generation) == (1, 1)
    assert not delivery["reviewer"].calls and len(env.fake.lanes) == 1
