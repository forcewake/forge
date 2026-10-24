"""Retained publication/CI lessons using native Git CAS and exact pipeline binding.

REST commit actions/start_branch are retired. Real receiver failure and response
loss preserve one push and reconcile exact parent, marker and complete tree.
"""

import json

import httpx

from forge.domain.verdicts import Committed, Unknown
from forge.port_values import Applied, Found
from forge.providers.git_process import GitFailure
from tests import gitlab_engine_fixture as gl
from tests import test_native_git_publication as proof
from tests import native_clients_fixture as fixture
from tests.native_clients_fixture import resource_calls

gitlab, gitlab_native = fixture.gitlab, fixture.gitlab_native
native = proof.native


async def test_create_commit_posts_actions(gitlab_native):
    env = gitlab_native
    result = await env.writer.commit(env.repo, env.candidate, env.base, "exact-native")
    assert isinstance(result, Committed)
    assert proof.git(env.server, "show", result.sha + ":src/file.txt", raw=True) == b"approved\n"
    assert proof.git(env.server, "rev-parse", result.sha + "^").decode() == env.base
    pushes = [call for call in env.calls if call[0] == "push"]
    assert len(pushes) == 1
    assert f"--force-with-lease=refs/heads/{proof.BRANCH}:{env.base}" in pushes[0]
    assert all(request.method == "GET" for request in env.wire.calls)


async def test_create_commit_single_post_on_503_no_generic_retry(gitlab_native):
    env = gitlab_native
    hook = env.server / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    result = await env.writer.commit(env.repo, env.candidate, env.base, "refused")
    assert isinstance(result, Unknown)
    assert proof.git(env.server, "rev-parse", "refs/heads/" + proof.BRANCH).decode() == env.base
    assert len([call for call in env.calls if call[0] == "push"]) == 1
    assert all(request.method == "GET" for request in env.wire.calls)


async def test_create_commit_timeout_raises_commit_outcome_unknown(gitlab_native, monkeypatch):
    from forge.providers.gitlab import commit as module

    env = gitlab_native
    publish = module.publish

    async def lost(*args, **kwargs):
        await publish(*args, **kwargs)
        raise GitFailure("git_operation_unavailable")

    monkeypatch.setattr(module, "publish", lost)
    result = await env.writer.commit(env.repo, env.candidate, env.base, "lost-response")
    assert isinstance(result, Unknown)
    accepted = proof.git(env.server, "rev-parse", "refs/heads/" + proof.BRANCH).decode()
    assert accepted != env.base
    recovered = await env.repositories.probe_commit(
        env.repo, proof.BRANCH, "lost-response", env.base, env.candidate
    )
    assert isinstance(recovered, Found) and isinstance(recovered.value, Committed)
    assert recovered.value.sha == accepted
    assert len([call for call in env.calls if call[0] == "push"]) == 1


async def test_list_commits_returns_sha_message_and_parents(gitlab_native):
    env = gitlab_native
    result = await env.writer.commit(env.repo, env.candidate, env.base, "parent-bound")
    assert isinstance(result, Committed)
    rows = await env.api.pages(env.repo, "repository/commits", params={"ref_name": result.sha})
    assert rows[0]["id"] == result.sha and rows[0]["parent_ids"] == [env.base]
    assert rows[0]["message"].endswith("parent-bound") and rows[1]["parent_ids"] == []
    assert proof.git(env.server, "show", result.sha + ":src/link") == b"file.txt"


async def test_create_pipeline_posts_ref(gitlab):
    gitlab.wire.mutations["POST", "pipeline"] = httpx.Response(201, json=gl.pipeline())
    result = await gitlab.writes.dispatch_lane(gitlab.repo, gitlab.dispatch)
    assert isinstance(result, Applied) and result.value.native_id == "700"
    body = json.loads(resource_calls(gitlab, "/pipeline", "POST")[0].content)
    assert body["ref"] == gitlab.dispatch.ref
    variables = {row["key"]: row["value"] for row in body["variables"]}
    assert variables["FORGE_LANE_NONCE"] == gitlab.dispatch.nonce
    assert json.loads(variables["FORGE_LANE_DISPATCH"]) == json.loads(
        gitlab.dispatch.model_dump_json()
    )
    assert len(resource_calls(gitlab, "/pipeline", "POST")) == 1


async def test_list_pipelines_filters_by_sha(gitlab):
    checks = await gitlab.ci.checks_for(gitlab.repo, gitlab.sha)
    assert len(checks) == 1 and checks[0].tested_sha == gitlab.sha
    assert checks[0].native_id == "701" and checks[0].attempt == 701
    assert resource_calls(gitlab, "/pipelines")[0].url.params["sha"] == gitlab.sha
    jobs = resource_calls(gitlab, "/pipelines/700/jobs")[0]
    assert jobs.url.params["include_retried"] == "false"
