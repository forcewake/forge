"""Retained GitHub lessons through native scoped HTTP and leased Git publication.

Historical names identify ledger evidence. GraphQL commits, heuristic branch
fallbacks, provider artifact downloads and blind dispatch acceptance are retired;
their authority, loss and confinement lessons pin the sole native contracts.
"""

from datetime import timedelta
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.port_values import Applied, Found, Inconclusive, ReadUnavailable
from forge.providers.github.transport import Transport, TransportFailure
from tests import test_github_reads as reads
from tests import test_github_rest as rest
from tests import test_native_git_publication as git_proof
from tests import native_clients_fixture as fixture
from tests import test_github_native_commit as native_fixture
from tests.native_clients_fixture import blob, jobs, resource_calls

app_key, github = fixture.app_key, fixture.github
connected, native = native_fixture.connected, native_fixture.native


async def test_client_connection_hygiene_is_pinned(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:123")
    seen = []

    def wire(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    transport = Transport(transport=httpx.MockTransport(wire))
    try:
        with pytest.raises(TransportFailure) as failure:
            await transport.request("GET", "/bounded", "synthetic-private")
        assert failure.value.status == 302 and len(seen) == 1
        assert seen[0].url.host == "api.github.com"
        assert seen[0].headers["authorization"] == "Bearer synthetic-private"
        assert transport._http.timeout.connect == 30
    finally:
        await transport.close()


async def test_app_jwt_is_rs256_with_required_claims(github, app_key):
    await github.auth.token(github.repo)
    key = serialization.load_pem_private_key(app_key.encode(), password=None)
    token = github.wire.mints[0].headers["authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    assert claims == {
        "iss": "app-9",
        "iat": int(github.clock.now().timestamp()) - 60,
        "exp": int(github.clock.now().timestamp()) + 540,
    }


async def test_mint_sends_bearer_jwt_with_github_headers(github):
    await github.auth.token(github.repo)
    request = github.wire.mints[0]
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["authorization"].startswith("Bearer ")
    body = json.loads(request.content)
    assert body["repository_ids"] == [123]
    assert set(body["permissions"].values()) == {"read"}


async def test_installation_token_is_cached(github):
    first = await github.auth.token(github.repo)
    second = await github.auth.token(github.repo)
    assert first is second and len(github.wire.mints) == 1
    assert "synthetic-token" not in repr(first)


async def test_token_near_expiry_is_re_minted(github):
    first = await github.auth.token(github.repo)
    github.clock.value += timedelta(seconds=3400)
    second = await github.auth.token(github.repo)
    assert first.value != second.value and len(github.wire.mints) == 2


async def test_invalidate_forces_re_mint(github):
    first = await github.auth.token(github.repo)
    write = await github.auth.token(github.repo, write=True)
    github.auth.invalidate(github.repo)
    assert (await github.auth.token(github.repo)).value != first.value
    assert await github.auth.token(github.repo, write=True) is write
    assert len(github.wire.mints) == 3


async def test_client_re_mints_once_on_401(github):
    attempts = []

    def response(request):
        attempts.append(request)
        return httpx.Response(
            401 if len(attempts) == 1 else 200,
            json={"total_count": 1, "repositories": [github.wire.metadata]},
        )

    github.wire.metadata_response = response
    assert (await github.api.metadata(github.repo)).document["id"] == 123
    assert len(attempts) == len(github.wire.mints) == 2
    assert attempts[0].headers["authorization"] != attempts[1].headers["authorization"]
    github.api._metadata_cache.clear()
    github.wire.metadata_response = httpx.Response(401)
    with pytest.raises(TransportFailure):
        await github.api.metadata(github.repo)
    assert len(resource_calls(github, "/installation/repositories")) == 4


async def test_rate_limit_surfaces_retry_after_without_retries(github):
    github.wire.metadata_response = httpx.Response(
        403, headers={"Retry-After": "123", "x-ratelimit-remaining": "0"}
    )
    with pytest.raises(TransportFailure) as failure:
        await github.api.metadata(github.repo)
    assert failure.value.reason == "rate_limited" and failure.value.retry_after == 123
    assert len(resource_calls(github, "/installation/repositories")) == 1


async def test_primary_limit_without_retry_after_uses_reset(github):
    # Scheduler owns retry timing. An untrusted reset header cannot authorize a hot retry.
    github.wire.metadata_response = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999999999999999999"}
    )
    with pytest.raises(TransportFailure) as failure:
        await github.api.metadata(github.repo)
    assert failure.value.reason == "rate_limited" and failure.value.retry_after is None
    assert len(resource_calls(github, "/installation/repositories")) == 1


async def test_get_issue_maps_onto_neutral_dto(github):
    task = await github.subjects.get_task(github.subject)
    assert task.subject == github.subject and task.title == "Implement safely"
    assert task.description == "Keep the approved input." and task.labels == ("forge",)
    assert len(task.version) == 64


async def test_get_issue_comments_follows_link_pagination(github):
    def page(request):
        number = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[reads.comment(i, "other") for i in range(1, 101)]
            if number == 1
            else [reads.comment(101)],
        )

    github.wire.routes["issues/7/comments"] = page
    result = await github.subjects.probe_comment(github.subject, reads.MARKER)
    assert isinstance(result, Found) and result.value.value.native_id == "101"
    assert [r.url.params["page"] for r in resource_calls(github, "/comments")] == ["1", "2"]


async def test_create_issue_comment_is_never_retried(github):
    github.wire.mutations["issues/7/comments"] = httpx.Response(500)
    assert isinstance(
        await github.writes.post_comment(github.subject, "hello", reads.MARKER), Unknown
    )
    assert len(resource_calls(github, "/comments", "POST")) == 1


async def test_idempotent_gets_retry_on_5xx(github):
    # Retained recovery lesson: a later explicit attempt can recover, never a hidden loop.
    github.wire.routes["issues/7"] = httpx.Response(503)
    with pytest.raises(ReadUnavailable):
        await github.subjects.get_task(github.subject)
    assert len(resource_calls(github, "/issues/7")) == 1
    github.wire.routes["issues/7"] = reads.Wire().routes["issues/7"]
    assert (await github.subjects.get_task(github.subject)).subject == github.subject
    assert len(resource_calls(github, "/issues/7")) == 2


async def test_validation_422_surfaces_errors_list(github):
    github.wire.mutations["pulls"] = httpx.Response(
        422, json={"errors": [{"field": "head", "message": "synthetic-sensitive-body"}]}
    )
    result = await github.writes.ensure_draft(github.repo, github.dispatch.ref, "Title", "body")
    assert isinstance(result, Unknown) and "422" in result.evidence
    assert "synthetic-sensitive-body" not in result.evidence
    assert len(resource_calls(github, "/pulls", "POST")) == 1


async def test_get_branch_head_from_refs_endpoint(github):
    assert await github.repositories.head(github.repo, "main") == github.sha
    assert len(resource_calls(github, "/git/ref/heads/main")) == 1


async def test_get_branch_head_falls_back_to_commits_endpoint(github):
    # Native refs are authoritative. A missing ref cannot be replaced by unrelated history.
    github.wire.routes["git/ref/heads/main"] = httpx.Response(404)
    assert await github.repositories.head(github.repo, "main") is None
    assert not resource_calls(github, "/commits/main")
    assert isinstance(
        await github.repositories.probe_branch(github.repo, "main", github.sha), Inconclusive
    )


async def test_create_commit_on_branch_sends_cas_and_base64_contents(connected):
    writer, _, wire, server, _, base, calls = connected
    result = await writer.commit(reads.REPO, git_proof.candidate(base), base, "exact-cas")
    assert isinstance(result, Committed) and result.sha == wire.head()
    assert git_proof.git(server, "show", result.sha + ":src/file.txt", raw=True) == b"approved\n"
    assert git_proof.git(server, "rev-parse", result.sha + "^").decode() == base
    pushes = [call for call in calls if call[0] == "push"]
    assert (
        len(pushes) == 1 and f"--force-with-lease=refs/heads/{git_proof.BRANCH}:{base}" in pushes[0]
    )
    assert not any(
        request.method != "GET" and "/access_tokens" not in request.url.path
        for request in wire.calls
    )


async def test_stale_data_mismatch_surfaces_as_drift(connected):
    writer, _, wire, server, seed, base, calls = connected
    human = git_proof.human_commit(seed, server, base)
    git_proof.git(server, "update-ref", "refs/heads/" + git_proof.BRANCH, human, base)
    result = await writer.commit(reads.REPO, git_proof.candidate(base), base, "stale")
    assert isinstance(result, Conflict) and result.actual_head == human
    assert wire.head() == human and not calls


async def test_graphql_non_stale_errors_surface_as_api_error(connected):
    # GraphQL is retired; actual native receiver refusal is unknown, never committed.
    writer, _, wire, server, _, base, calls = connected
    hook = server / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    result = await writer.commit(reads.REPO, git_proof.candidate(base), base, "refused")
    assert isinstance(result, Unknown) and wire.head() == base
    assert len([call for call in calls if call[0] == "push"]) == 1


async def test_create_draft_pr_sends_draft_true(github):
    github.wire.mutations["pulls"] = httpx.Response(201, json=rest.pr())
    assert isinstance(
        await github.writes.ensure_draft(github.repo, github.dispatch.ref, "Title", "body"), Applied
    )
    body = json.loads(resource_calls(github, "/pulls", "POST")[0].content)
    assert body["draft"] is True and body["maintainer_can_modify"] is False
    assert body["head"] == github.dispatch.ref and body["base"] == "main"


async def test_get_pr_by_head_filters_owner_branch(github):
    github.wire.routes["pulls"] = [rest.pr()]
    assert await github.reviews.find_draft(github.repo, github.dispatch.ref) == github.review
    request = resource_calls(github, "/pulls")[0]
    assert request.url.params["head"] == "new:" + github.dispatch.ref
    assert request.url.params["state"] == "all"
    github.wire.routes["pulls"][0]["head"]["repo"]["id"] = 999
    with pytest.raises(ReadUnavailable):
        await github.reviews.find_draft(github.repo, github.dispatch.ref)


async def test_get_pr_by_head_returns_none_when_absent(github):
    assert await github.reviews.find_draft(github.repo, "forge/missing") is None
    assert isinstance(await github.reviews.probe_draft(github.repo, "forge/missing"), Inconclusive)


async def test_list_check_runs_for_sha(github):
    jobs(github)
    checks = await github.ci.checks_for(github.repo, github.sha)
    assert [(check.tested_sha, check.status, check.attempt) for check in checks] == [
        (github.sha, "passed", 1)
    ]
    github.wire.routes["actions/runs/700/attempts/1/jobs"]["jobs"][0]["head_sha"] = "0" * 40
    with pytest.raises(ReadUnavailable):
        await github.ci.checks_for(github.repo, github.sha)


async def test_list_workflow_runs_for_sha_filters_by_name(github):
    # Native workflow paths, never mutable display names, choose independent verification.
    jobs(github)
    github.wire.routes["actions/runs"]["workflow_runs"] += [
        rest.run(id=701, path=".github/workflows/docs.yml")
    ]
    github.wire.routes["actions/runs"]["total_count"] = 2
    checks = await github.ci.checks_for(github.repo, github.sha)
    assert len(checks) == 1 and checks[0].role == "verification"
    assert resource_calls(github, "/actions/runs")[0].url.params["head_sha"] == github.sha


class TestActionsEndpoints:
    async def test_dispatch_parses_the_2026_run_id_response(self, github):
        github.wire.mutations["actions/workflows/71/dispatches"] = httpx.Response(
            202, json={"run_id": 999}
        )
        result = await github.writes.dispatch_lane(github.repo, github.dispatch)
        assert isinstance(result, Applied) and result.value.native_id == "700"
        sent = json.loads(resource_calls(github, "/dispatches", "POST")[0].content)
        assert sent["ref"] == github.dispatch.ref
        assert json.loads(sent["inputs"]["dispatch"]) == json.loads(
            github.dispatch.model_dump_json()
        )

    async def test_dispatch_legacy_empty_body_returns_empty_dict(self, github):
        github.wire.mutations["actions/workflows/71/dispatches"] = httpx.Response(204)
        github.wire.routes["actions/workflows/71/runs"] = {"total_count": 0, "workflow_runs": []}
        assert isinstance(await github.writes.dispatch_lane(github.repo, github.dispatch), Unknown)
        github.wire.routes["actions/workflows/71/runs"] = {
            "total_count": 1,
            "workflow_runs": [rest.run()],
        }
        assert (await github.ci.find_lane(github.repo, github.dispatch.nonce)).native_id == "700"
        assert len(resource_calls(github, "/dispatches", "POST")) == 1

    async def test_dispatch_is_never_retried(self, github):
        github.wire.mutations["actions/workflows/71/dispatches"] = httpx.Response(500)
        assert isinstance(await github.writes.dispatch_lane(github.repo, github.dispatch), Unknown)
        assert len(resource_calls(github, "/dispatches", "POST")) == 1

    async def test_list_workflow_dispatch_runs_filters_and_windows(self, github):
        handle = await github.ci.find_lane(github.repo, github.dispatch.nonce)
        assert handle.correlation_id == github.dispatch.nonce and handle.native_id == "700"
        request = resource_calls(github, "/actions/workflows/71/runs")[0]
        assert dict(request.url.params) == {
            "event": "workflow_dispatch",
            "branch": github.dispatch.ref,
            "head_sha": github.sha,
            "per_page": "100",
            "page": "1",
        }
        github.wire.routes["actions/workflows/71/runs"]["workflow_runs"][0]["head_repository"][
            "id"
        ] = 999
        assert isinstance(
            await github.ci.probe_dispatch(github.repo, github.dispatch.nonce), Inconclusive
        )

    async def test_download_artifact_zip_follows_the_302(self, github):
        # Candidate bytes now arrive through the authenticated lane; native reads never follow redirects.
        github.wire.routes["git/blobs/" + reads.BLOB] = httpx.Response(
            302, headers={"location": "https://attacker.invalid/zip"}
        )
        with pytest.raises(ReadUnavailable):
            await github.repositories.read_blob(github.repo, github.sha, github.path)
        assert all(request.url.host == "api.github.com" for request in github.wire.calls)

    async def test_get_workflow_run_and_jobs_and_artifacts(self, github):
        jobs(github)
        handle = await github.ci.find_lane(github.repo, github.dispatch.nonce)
        assert (await github.ci.lane_status(handle)).status == "succeeded"
        checks = await github.ci.checks_for(github.repo, github.sha)
        assert checks[0].native_id == "901" and checks[0].attempt == 1
        assert resource_calls(github, "/actions/runs/700/attempts/1/jobs")

    async def test_cancel_workflow_run_posts_once(self, github):
        # Cancellation authority is local lease/session/key revocation; this adapter exposes no CI cancel write.
        # Preserve the non-replay lesson on a concrete native mutation with unknown remote outcome.
        github.wire.mutations["issues/7/comments"] = httpx.Response(503)
        assert isinstance(
            await github.writes.post_comment(github.subject, "cancelled", reads.MARKER), Unknown
        )
        assert len(resource_calls(github, "/comments", "POST")) == 1

    async def test_get_job_log_follows_the_302(self, github):
        github.wire.routes["git/blobs/" + reads.BLOB] = httpx.Response(
            307, headers={"location": "https://logs.attacker.invalid/path"}
        )
        with pytest.raises(ReadUnavailable):
            await github.repositories.read_blob(github.repo, github.sha, github.path)
        assert len(resource_calls(github, "/git/blobs/" + reads.BLOB)) == 1
        assert all(request.url.host == "api.github.com" for request in github.wire.calls)


async def test_reader_get_file_returns_full_base64_blob(github):
    body = b"hello world\r\n"
    blob(github, body)
    assert await github.repositories.read_blob(github.repo, github.sha, github.path) == body


async def test_reader_oversized_file_resolves_through_blob_api(github):
    body = b"large" * 250000
    blob(github, body)
    assert await github.repositories.read_blob(github.repo, github.sha, github.path) == body
    assert not any("/contents/" in request.url.path for request in github.wire.calls)


async def test_reader_rejects_truncated_tree(github):
    github.wire.routes["git/trees/" + github.tree]["truncated"] = True
    with pytest.raises(ReadUnavailable):
        await github.repositories.read_tree(github.repo, github.sha)


async def test_reader_rejects_non_regular_files(github):
    # Raw symlink bytes are representable; a submodule cannot masquerade as a blob.
    with pytest.raises(ReadUnavailable):
        await github.repositories.read_blob(github.repo, github.sha, "vendor/lib")
    assert not any("/git/blobs/" in request.url.path for request in github.wire.calls)


async def test_reader_maps_issue_from_client(github):
    github.wire.routes["issues/7"]["body"] = None
    task = await github.subjects.get_task(github.subject)
    assert task.subject.number == 7 and task.description == ""
    github.wire.routes["issues/7"]["number"] = 8
    with pytest.raises(ReadUnavailable):
        await github.subjects.get_task(github.subject)
