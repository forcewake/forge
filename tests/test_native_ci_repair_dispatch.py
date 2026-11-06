"""A real second native dispatch uses the failed candidate while approval keeps its base."""

import json

from forge_protocol import LaneDispatch, RepairContext
import httpx
import pytest

from forge.port_values import Applied, Refused
from forge.providers.github.ci import run_title
from tests import gitlab_engine_fixture as gl
from tests import test_github_rest as gh
from tests.native_clients_fixture import (
    app_key as app_key,
    github as github,
    gitlab as gitlab,
    blob_case as blob_case,
)


native = blob_case


@pytest.mark.parametrize("human_move", [False, True])
async def test_second_native_lane_binds_repair_candidate_nonce_and_refuses_human_move(
    native, human_move
):
    original = native.dispatch
    candidate = "c" * 40
    repair = RepairContext(
        forge_run_id=original.forge_run_id,
        attempt=2,
        generation=2,
        approved_digest=original.approved_digest,
        original_base_sha=original.base_sha,
        candidate_sha=candidate,
        prior_attempt=1,
        prior_generation=1,
        failure_context="Exact native unit-test failure",
    )
    dispatch = LaneDispatch.model_validate_json(
        json.dumps(
            {
                **original.model_dump(mode="json"),
                "attempt": 2,
                "generation": 2,
                "nonce": "new-repair-nonce-" + "a" * 32,
                "repair": repair.model_dump(mode="json"),
            }
        )
    )
    assert dispatch.base_sha == original.base_sha != dispatch.execution_base_sha == candidate

    async def lookup(repo, nonce):
        return (
            dispatch if repo.identity == native.repo.identity and nonce == dispatch.nonce else None
        )

    native.ci.dispatch_lookup = lookup
    actual_head = "f" * 40 if human_move else candidate
    if native.repo.identity.provider == "github":
        native.wire.routes["git/ref/heads/" + dispatch.ref]["object"]["sha"] = actual_head
        row = gh.run(id=800, head_sha=candidate, run_attempt=2, display_title=run_title(dispatch))
        native.wire.routes["actions/workflows/71/runs"] = {"total_count": 1, "workflow_runs": [row]}
        native.wire.routes["actions/runs/800/attempts/2"] = row
        native.wire.mutations["actions/workflows/71/dispatches"] = httpx.Response(204)
        target = "/actions/workflows/71/dispatches"
    else:
        native.wire.routes["repository/branches/" + dispatch.ref]["commit"]["id"] = actual_head
        row = gl.pipeline(800, sha=candidate, ref=dispatch.ref)
        native.wire.routes["pipelines"] = [row]
        native.wire.routes["pipelines/800"] = row

        def create(request):
            native.wire.routes["pipelines/800/variables"] = json.loads(request.content)["variables"]
            return httpx.Response(201, json=row)

        native.wire.mutations["POST", "pipeline"] = create
        target = "/pipeline"
    outcome = await native.writes.dispatch_lane(native.repo, dispatch)
    calls = [r for r in native.wire.calls if r.method == "POST" and r.url.path.endswith(target)]
    if human_move:
        assert isinstance(outcome, Refused) and outcome.reason == "dispatch_head_changed"
        assert calls == []
        return
    assert isinstance(outcome, Applied), outcome
    assert len(calls) == 1 and outcome.value.correlation_id == dispatch.nonce
    assert outcome.value.native_id == "800"
    assert (await native.ci.lane_status(outcome.value)).handle == outcome.value
    found = await native.ci.find_lane(native.repo, dispatch.nonce)
    assert found == outcome.value
    payload = json.loads(calls[0].content)
    if native.repo.identity.provider == "github":
        actual = LaneDispatch.model_validate_json(payload["inputs"]["dispatch"])
        probes = [r for r in native.wire.calls if r.url.path.endswith("/actions/workflows/71/runs")]
        assert probes and all(r.url.params["head_sha"] == candidate for r in probes)
    else:
        variables = {r["key"]: r["value"] for r in payload["variables"]}
        actual = LaneDispatch.model_validate_json(variables["FORGE_LANE_DISPATCH"])
        probes = [r for r in native.wire.calls if r.url.path.endswith("/pipelines")]
        assert probes and all(r.url.params["sha"] == candidate for r in probes)
    assert actual == dispatch and actual.base_sha == original.base_sha
