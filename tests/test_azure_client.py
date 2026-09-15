"""Azure DevOps client tests: auth bytes, api-version injection, CAS drift.

Everything runs against pytest-httpx — no live Azure DevOps. Endpoint/field
ground truth: docs/research/azure-devops.md; normalized payload fixtures:
tests/fixtures/azure_payloads/ (research §10, SHAs made consistent).
"""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import pytest
from pytest_httpx import HTTPXMock

from forge.integrations.azure import (
    AzureDevOpsAuthError,
    AzureDevOpsClient,
    AzureDevOpsDriftError,
    AzureDevOpsError,
    AzureDevOpsNotFoundError,
    AzureDevOpsRateLimited,
    AzureRepositoryReader,
    CommitPayload,
    FileChange,
    THREAD_STATUS_ACTIVE,
)

BASE = "https://dev.azure.test/fabrikam"
PROJECT = "Fabrikam"
REPO = "core"
REPO_PATH = f"{BASE}/{PROJECT}/_apis/git/repositories/{REPO}"
PUSH_URL = f"{REPO_PATH}/pushes"
REFS_URL = f"{REPO_PATH}/refs"
ITEMS_URL = f"{REPO_PATH}/items"
PROJECT_URL = f"{BASE}/_apis/projects/{PROJECT}"

TOKEN = "test-pat-token"
EXPECTED_AUTH = "Basic " + base64.b64encode(f":{TOKEN}".encode("utf-8")).decode("ascii")

# The fixture SHA chain (identical values live in tests/fixtures/azure_payloads/).
ZERO_SHA = "0" * 40
BASE_SHA = "9c4e2a7f1b3d8e5a60c2f47b19d3a8e07f5c6b21"  # frozen attempt base
BRANCH_TIP = "2f8a61c9e37b4d05a1f8c6e29b70d4135ea8c9f4"  # branch-create commit
PUSH_TIP = "b47e09d3c5a28f16e0d9a4c7138b52fa60e7d831"  # after the normal push
NEW_HEAD = "e6b42d90a1c7583f0d8e4a6b29c17f5308da2b47"  # after the PR push
MERGE_1 = "8a30c5f17e29b46d08fa3b5c192e7d40a6c9e518"
MERGE_2 = "c94f2e6b08d13a57b9e0c4a827f36d1059be4d70"

FIXTURES = Path(__file__).parent / "fixtures" / "azure_payloads"

EXPECTED_FIXTURES = {
    "push_branch_create.json",
    "push_normal.json",
    "pr_created.json",
    "pr_updated_push.json",
    "pr_commented_on.json",
    "build_complete_failed.json",
    "workitem_commented_implement.json",
    "pipeline_run_created.json",
    "thread_create_request.json",
    "thread_create_response.json",
    "push_response_stale_object_id.json",
    "build_timeline_failed.json",
    # AZ-2 ingress fixtures (webhook normalization matrix).
    "pr_commented_on_implement.json",
    "workitem_commented_go.json",
    "build_complete_succeeded.json",
}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def azdo_url(url: str, *, api_version: str = "7.1", **params: str) -> str:
    """Registration URL with the exact query pytest-httpx matches on.

    The client injects ``api-version`` on every request, so every mocked URL
    must carry the full query string (order-insensitive, values compared
    decoded).
    """
    return f"{url}?{urlencode({'api-version': api_version, **params})}"


@pytest.fixture()
async def azdo(httpx_mock: HTTPXMock) -> AsyncIterator[AzureDevOpsClient]:
    client = AzureDevOpsClient(base_url=BASE, token=TOKEN)
    yield client
    await client.aclose()


@pytest.fixture()
async def reader(azdo: AzureDevOpsClient) -> AzureRepositoryReader:
    return AzureRepositoryReader(azdo, PROJECT, REPO)


# ---------------------------------------------------------------------------
# Transport: auth bytes + api-version injection (research §1.2/§1.3)
# ---------------------------------------------------------------------------


async def test_auth_header_is_basic_base64_of_colon_pat(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})

    await azdo.get_project(PROJECT)

    (request,) = httpx_mock.get_requests()
    # The exact documented credential bytes: base64(":" + PAT) — empty
    # username, colon prefix (research §1.2).
    assert request.headers["authorization"] == EXPECTED_AUTH


async def test_api_version_71_injected_on_every_request(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})

    await azdo.get_project(PROJECT)

    (request,) = httpx_mock.get_requests()
    assert request.url.params["api-version"] == "7.1"


async def test_basic_auth_is_computed_per_request(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})

    await azdo.get_project(PROJECT)
    await azdo.get_project(PROJECT)

    assert all(r.headers["authorization"] == EXPECTED_AUTH for r in httpx_mock.get_requests())


async def test_401_tf400813_raises_auth_error_with_envelope_message(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        status_code=401,
        json={
            "$id": "1",
            "message": "TF400813: Resource not available for anonymous access. "
            "The request was not authenticated.",
        },
    )

    with pytest.raises(AzureDevOpsAuthError) as exc_info:
        await azdo.get_project(PROJECT)

    assert "TF400813" in str(exc_info.value)
    assert exc_info.value.status_code == 401
    assert len(httpx_mock.get_requests()) == 1  # a PAT is static: no retry


async def test_203_html_login_page_raises_auth_error(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    # The documented mis-encoded-PAT symptom: 203 Non-Authoritative with the
    # login page HTML (research §1.2) — never parsed for a payload.
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        status_code=203,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html>Sign in to Azure DevOps</html>",
    )

    with pytest.raises(AzureDevOpsAuthError, match="HTML"):
        await azdo.get_project(PROJECT)


async def test_redirect_to_login_raises_auth_error(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        status_code=302,
        headers={"location": f"{BASE}/_login"},
    )

    with pytest.raises(AzureDevOpsAuthError):
        await azdo.get_project(PROJECT)

    assert len(httpx_mock.get_requests()) == 1  # redirects never followed


async def test_404_raises_not_found(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        status_code=404,
        json={"message": "TF200016: Project does not exist."},
    )

    with pytest.raises(AzureDevOpsNotFoundError):
        await azdo.get_project(PROJECT)


async def test_500_on_get_retries_then_succeeds(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), status_code=503)
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})

    project = await azdo.get_project(PROJECT)

    assert project["id"] == "p1"
    assert len(httpx_mock.get_requests()) == 2


async def test_500_on_post_is_never_retried(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullrequests"), method="POST", status_code=500
    )

    with pytest.raises(AzureDevOpsError):
        await azdo.create_draft_pr(PROJECT, REPO, "forge/wi-42", "main", "forge: WI-42 candidate")

    # Non-idempotent: a replay would duplicate the PR (the F06 lesson).
    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


async def test_429_retried_once_with_retry_after_then_succeeds(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        status_code=429,
        headers={"Retry-After": "0"},
        json={"message": "TF400733: request blocked due to exceeding usage"},
    )
    httpx_mock.add_response(url=azdo_url(PROJECT_URL), json={"id": "p1"})

    project = await azdo.get_project(PROJECT)

    assert project["id"] == "p1"
    assert len(httpx_mock.get_requests()) == 2


async def test_429_on_non_idempotent_call_surfaces_rate_limited(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        status_code=429,
        headers={"Retry-After": "3"},
        json={"message": "TF400733: throttled"},
    )

    with pytest.raises(AzureDevOpsRateLimited) as exc_info:
        await azdo.push_commits(
            PROJECT,
            REPO,
            "forge/wi-42",
            expected_old_sha=BASE_SHA,
            commits=[CommitPayload(comment="c", changes=[])],
        )

    assert exc_info.value.retry_after == 3
    assert len(httpx_mock.get_requests()) == 1  # writes fire exactly once


# ---------------------------------------------------------------------------
# Read surface: projects / repositories / refs / items / trees
# ---------------------------------------------------------------------------


async def test_get_project_returns_payload(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(PROJECT_URL),
        json={"id": "9f8e7d6c-0000-0000-0000-000000000009", "name": PROJECT},
    )

    project = await azdo.get_project(PROJECT)

    assert project["name"] == PROJECT


async def test_get_repository_returns_raw_repo_document(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    repo_doc = load_fixture("push_branch_create.json")["resource"]["repository"]
    httpx_mock.add_response(url=azdo_url(REPO_PATH), json=repo_doc)

    repository = await azdo.get_repository(PROJECT, REPO)

    assert repository["id"] == "1a2b3c4d-0000-0000-0000-000000000001"
    assert repository["defaultBranch"] == "refs/heads/main"
    assert repository["project"]["name"] == PROJECT


async def test_get_refs_sends_filter_and_returns_value_list(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL, filter="heads/main"),
        json={
            "value": [{"name": "refs/heads/main", "objectId": BRANCH_TIP, "isLocked": False}],
            "count": 1,
        },
    )

    refs = await azdo.get_refs(PROJECT, REPO, filter="heads/main")

    (request,) = httpx_mock.get_requests()
    assert request.url.params["filter"] == "heads/main"
    assert refs[0]["objectId"] == BRANCH_TIP


async def test_get_branch_head_returns_object_id(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL, filter="heads/main"),
        json={"value": [{"name": "refs/heads/main", "objectId": BRANCH_TIP}]},
    )

    assert await azdo.get_branch_head(PROJECT, REPO, "main") == BRANCH_TIP


async def test_get_branch_head_missing_raises_not_found(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL, filter="heads/ghost"), json={"value": [], "count": 0}
    )

    with pytest.raises(AzureDevOpsNotFoundError, match="ghost"):
        await azdo.get_branch_head(PROJECT, REPO, "ghost")


async def test_get_item_sends_path_and_commit_version_descriptor(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/README.md",
            includeContent="true",
            **{"versionDescriptor.version": BASE_SHA, "versionDescriptor.versionType": "commit"},
        ),
        json={"path": "/README.md", "content": "hello\n", "isSymLink": False},
    )

    item = await azdo.get_item(PROJECT, REPO, "/README.md", version=BASE_SHA)

    assert item["content"] == "hello\n"
    (request,) = httpx_mock.get_requests()
    assert request.url.params["path"] == "/README.md"
    assert request.url.params["includeContent"] == "true"
    assert request.url.params["versionDescriptor.version"] == BASE_SHA
    assert request.url.params["versionDescriptor.versionType"] == "commit"


async def test_get_item_without_version_omits_descriptor(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(ITEMS_URL, path="/README.md", includeContent="true"),
        json={"path": "/README.md", "content": "x"},
    )

    await azdo.get_item(PROJECT, REPO, "/README.md")

    (request,) = httpx_mock.get_requests()
    assert "versionDescriptor.version" not in request.url.params
    assert "versionDescriptor.versionType" not in request.url.params


async def test_get_tree_passes_recursive_flag(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BASE_SHA}", recursive="true"),
        json={"treeEntries": [], "truncated": False},
    )

    tree = await azdo.get_tree(PROJECT, REPO, BASE_SHA, recursive=True)

    assert tree["truncated"] is False
    (request,) = httpx_mock.get_requests()
    assert request.url.params["recursive"] == "true"


# ---------------------------------------------------------------------------
# CAS write surface: branch creation + pushes (research §3)
# ---------------------------------------------------------------------------


async def test_create_branch_from_sends_40_zero_old_object_id(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL),
        method="POST",
        json=[{"name": "refs/heads/forge/wi-42", "updateStatus": "succeeded"}],
    )

    await azdo.create_branch_from(PROJECT, REPO, "forge/wi-42", base_sha=BASE_SHA)

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert json.loads(request.content) == {
        "refUpdates": [
            {"name": "refs/heads/forge/wi-42", "oldObjectId": ZERO_SHA, "newObjectId": BASE_SHA}
        ]
    }


async def test_create_branch_from_value_wrapped_success(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL),
        method="POST",
        json={"value": [{"name": "refs/heads/forge/wi-42", "updateStatus": "succeeded"}]},
    )

    payload = await azdo.create_branch_from(PROJECT, REPO, "forge/wi-42", base_sha=BASE_SHA)

    assert payload["value"][0]["updateStatus"] == "succeeded"


async def test_create_branch_rejection_maps_to_drift(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL),
        method="POST",
        json=[
            {
                "name": "refs/heads/forge/wi-42",
                "updateStatus": "createBranchPermissionRequired",
                "customMessage": "not allowed",
            }
        ],
    )

    with pytest.raises(AzureDevOpsDriftError) as exc_info:
        await azdo.create_branch_from(PROJECT, REPO, "forge/wi-42", base_sha=BASE_SHA)

    assert exc_info.value.status == "createBranchPermissionRequired"
    assert exc_info.value.ref_name == "refs/heads/forge/wi-42"


def _sample_commits() -> list[CommitPayload]:
    return [
        CommitPayload(
            comment="forge: candidate for WI-42 (attempt 1)",
            changes=[
                FileChange(path="/docs/plan.md", change_type="add", content="# Plan\n"),
                FileChange(path="/src/app.py", change_type="edit", content="print(1)\n"),
                FileChange(path="/src/old.py", change_type="delete"),
            ],
        )
    ]


async def test_push_commits_body_shape_add_edit_delete(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        json={"pushId": 1044, "refUpdates": [{"updateStatus": "succeeded"}]},
    )

    await azdo.push_commits(
        PROJECT,
        REPO,
        "forge/wi-42",
        expected_old_sha=BASE_SHA,
        commits=_sample_commits(),
    )

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(request.content)
    assert body["refUpdates"] == [{"name": "refs/heads/forge/wi-42", "oldObjectId": BASE_SHA}]
    changes = body["commits"][0]["changes"]
    assert changes[0] == {
        "changeType": "add",
        "item": {"path": "/docs/plan.md"},
        "newContent": {"content": "# Plan\n", "contentType": "rawtext"},
    }
    assert changes[1]["newContent"]["contentType"] == "rawtext"
    assert "newContent" not in changes[2]  # delete carries no content
    assert body["commits"][0]["comment"] == "forge: candidate for WI-42 (attempt 1)"


async def test_push_commits_base64_maps_to_base64encoded(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        json={"pushId": 1045, "refUpdates": [{"updateStatus": "succeeded"}]},
    )

    await azdo.push_commits(
        PROJECT,
        REPO,
        "forge/wi-42",
        expected_old_sha=BASE_SHA,
        commits=[
            CommitPayload(
                comment="binary asset",
                changes=[
                    FileChange(
                        path="/assets/logo.bin",
                        change_type="add",
                        content="AAECAw==",
                        encoding="base64",
                    )
                ],
            )
        ],
    )

    body = json.loads(httpx_mock.get_requests()[0].content)
    # The API spelling is "base64encoded" (research §3.1) — forge's "base64".
    assert body["commits"][0]["changes"][0]["newContent"]["contentType"] == "base64encoded"


async def test_push_success_push_shaped_response_returns_payload(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        json={
            "pushId": 1044,
            "refUpdates": [
                {
                    "name": "refs/heads/forge/wi-42",
                    "newObjectId": PUSH_TIP,
                    "updateStatus": "succeeded",
                }
            ],
        },
    )

    payload = await azdo.push_commits(
        PROJECT,
        REPO,
        "forge/wi-42",
        expected_old_sha=BASE_SHA,
        commits=_sample_commits(),
    )

    assert payload["pushId"] == 1044


async def test_push_stale_object_id_fixture_raises_drift(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    # Research §10.11: the CAS miss arrives as HTTP 200 with
    # updateStatus: staleObjectId (value-wrapped shape).
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        json=load_fixture("push_response_stale_object_id.json"),
    )

    with pytest.raises(AzureDevOpsDriftError) as exc_info:
        await azdo.push_commits(
            PROJECT,
            REPO,
            "forge/wi-42",
            expected_old_sha=PUSH_TIP,
            commits=_sample_commits(),
        )

    assert exc_info.value.status == "staleObjectId"
    assert exc_info.value.ref_name == "refs/heads/forge/wi-42"
    assert exc_info.value.status_code == 200
    assert "old object id" in exc_info.value.message


async def test_push_stale_bare_array_response_raises_drift(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    # The documented Refs-Update shape is a bare array — the parser must
    # accept it even though the pushes endpoint's wrapping is unverified
    # (research §10.11 extra-caution note).
    httpx_mock.add_response(
        url=azdo_url(PUSH_URL),
        method="POST",
        json=[{"name": "refs/heads/forge/wi-42", "updateStatus": "forcePushRequired"}],
    )

    with pytest.raises(AzureDevOpsDriftError) as exc_info:
        await azdo.push_commits(
            PROJECT,
            REPO,
            "forge/wi-42",
            expected_old_sha=BASE_SHA,
            commits=_sample_commits(),
        )

    assert exc_info.value.status == "forcePushRequired"


async def test_push_is_never_retried_on_http_error(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(url=azdo_url(PUSH_URL), method="POST", status_code=502)

    with pytest.raises(AzureDevOpsError):
        await azdo.push_commits(
            PROJECT,
            REPO,
            "forge/wi-42",
            expected_old_sha=BASE_SHA,
            commits=_sample_commits(),
        )

    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


def test_push_rejects_invalid_change_type_before_any_call():
    with pytest.raises(ValueError, match="change_type"):
        FileChange(path="/x", change_type="rename", content="y")


def test_push_rejects_delete_with_content():
    with pytest.raises(ValueError, match="delete"):
        FileChange(path="/x", change_type="delete", content="y")


def test_push_rejects_add_without_content():
    with pytest.raises(ValueError, match="content"):
        FileChange(path="/x", change_type="add")


def test_push_rejects_unknown_encoding():
    with pytest.raises(ValueError, match="encoding"):
        FileChange(path="/x", change_type="add", content="y", encoding="utf16")


# ---------------------------------------------------------------------------
# Pull requests (research §4)
# ---------------------------------------------------------------------------


async def test_create_draft_pr_body_is_draft_with_prefixed_refs(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullrequests"),
        method="POST",
        json={"pullRequestId": 512, "isDraft": True},
    )

    pr = await azdo.create_draft_pr(
        PROJECT,
        REPO,
        "forge/wi-42",
        "main",
        "forge: WI-42 candidate (attempt 1)",
        description="Attempt 1 from attempt-base 9c4e2a7f…",
    )

    assert pr["pullRequestId"] == 512
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert json.loads(request.content) == {
        "sourceRefName": "refs/heads/forge/wi-42",
        "targetRefName": "refs/heads/main",
        "title": "forge: WI-42 candidate (attempt 1)",
        "description": "Attempt 1 from attempt-base 9c4e2a7f…",
        "isDraft": True,
    }


async def test_get_pr_normalized_from_fixture_resource(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512"),
        json=load_fixture("pr_created.json")["resource"],
    )

    pr = await azdo.get_pr(PROJECT, REPO, 512)

    assert pr.id == 512
    assert pr.title == "forge: WI-42 candidate (attempt 1)"
    assert pr.source_ref_name == "refs/heads/forge/wi-42"
    assert pr.target_ref_name == "refs/heads/main"
    assert pr.is_draft is True
    assert pr.status == "active"
    assert pr.last_merge_commit_id == MERGE_1  # plain GitCommitRef, not a triple
    assert pr.created_by_display_name == "Forge Bot"
    assert pr.created_by_unique_name == "forge-bot@fabrikam.example"
    assert pr.web_url is None  # fixture carries no _links.web — parsed defensively


async def test_get_pr_web_url_from_links(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512"),
        json={
            "pullRequestId": 512,
            "title": "t",
            "status": "active",
            "_links": {"web": {"href": f"{BASE}/{PROJECT}/_git/{REPO}/pullrequest/512"}},
        },
    )

    pr = await azdo.get_pr(PROJECT, REPO, 512)

    assert pr.web_url == f"{BASE}/{PROJECT}/_git/{REPO}/pullrequest/512"


async def test_get_pr_iterations_three_way_shas(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/iterations"),
        json={
            "value": [
                {
                    "id": 1,
                    "sourceRefCommit": {"commitId": PUSH_TIP},
                    "targetRefCommit": {"commitId": MERGE_1},
                    "commonRefCommit": {"commitId": BASE_SHA},
                },
                {
                    "id": 2,
                    "sourceRefCommit": {"commitId": NEW_HEAD},
                    "targetRefCommit": {"commitId": MERGE_1},
                    "commonRefCommit": {"commitId": BASE_SHA},
                },
            ],
            "count": 2,
        },
    )

    iterations = await azdo.get_pr_iterations(PROJECT, REPO, 512)

    # The three-way SHAs live ONLY on iterations (research correction #2):
    # latest source is the incremental "after", previous is "before".
    assert [it.id for it in iterations] == [1, 2]
    assert iterations[-1].source_ref_commit == NEW_HEAD
    assert iterations[0].source_ref_commit == PUSH_TIP
    assert all(it.common_ref_commit == BASE_SHA for it in iterations)


async def test_list_pr_threads_returns_value_list(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    thread_response = load_fixture("thread_create_response.json")
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads"),
        json={"value": [thread_response], "count": 1},
    )

    threads = await azdo.list_pr_threads(PROJECT, REPO, 512)

    assert threads[0]["id"] == 77
    assert threads[0]["status"] == "active"  # responses carry STRING enums


async def test_create_pr_thread_matches_documented_request_fixture(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads"),
        method="POST",
        json=load_fixture("thread_create_response.json"),
    )
    expected = load_fixture("thread_create_request.json")
    content = expected["comments"][0]["content"]

    await azdo.create_pr_thread(
        PROJECT,
        REPO,
        512,
        content,
        file_path="/src/retry.py",
        line_start=42,
        line_end=48,
        offset_end=20,
        change_tracking_id=4,
        first_comparing_iteration=2,
        second_comparing_iteration=3,
    )

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    # NUMERIC enums on the request (commentType: 1, status: 1) — byte-equal
    # to the documented request body (research §10.9).
    assert json.loads(request.content) == expected


async def test_create_pr_thread_minimal_body_has_no_context(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads"),
        method="POST",
        json={"id": 78},
    )

    await azdo.create_pr_thread(PROJECT, REPO, 512, "summary finding")

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert json.loads(request.content) == {
        "comments": [{"parentCommentId": 0, "content": "summary finding", "commentType": 1}],
        "status": THREAD_STATUS_ACTIVE,
    }


async def test_create_pr_thread_change_tracking_without_iterations(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads"),
        method="POST",
        json={"id": 79},
    )

    await azdo.create_pr_thread(PROJECT, REPO, 512, "inline", change_tracking_id=4)

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(request.content)
    assert body["pullRequestThreadContext"] == {"changeTrackingId": 4}


async def test_create_pr_thread_parses_string_response(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads"),
        method="POST",
        json=load_fixture("thread_create_response.json"),
    )

    thread = await azdo.create_pr_thread(PROJECT, REPO, 512, "finding")

    assert thread["id"] == 77
    assert thread["status"] == "active"
    assert thread["comments"][0]["commentType"] == "text"
    assert thread["pullRequestThreadContext"]["changeTrackingId"] == 4


async def test_reply_pr_thread_sends_numeric_comment_type(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads/77/comments"),
        method="POST",
        json={"id": 311},
    )

    await azdo.reply_pr_thread(PROJECT, REPO, 512, 77, "fixed in e6b42d90", parent_comment_id=310)

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert json.loads(request.content) == {
        "content": "fixed in e6b42d90",
        "parentCommentId": 310,
        "commentType": 1,
    }


async def test_update_thread_status_sends_numeric_body(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/pullRequests/512/threads/77"),
        method="PATCH",
        json={"id": 77, "status": "fixed"},  # response echoes the STRING enum
    )

    thread = await azdo.update_thread_status(PROJECT, REPO, 512, 77, status=2)

    assert thread["status"] == "fixed"
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "PATCH"]
    assert json.loads(request.content) == {"status": 2}


# ---------------------------------------------------------------------------
# Work items: the plan/gate comment surface (research §5)
# ---------------------------------------------------------------------------


async def test_get_work_item(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/wit/workItems/142"),
        json={"id": 142, "fields": {"System.Title": "Ship the flux capacitor"}},
    )

    work_item = await azdo.get_work_item(PROJECT, 142)

    assert work_item["fields"]["System.Title"] == "Ship the flux capacitor"


async def test_add_work_item_comment_sends_preview_stripe_and_markdown(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{BASE}/{PROJECT}/_apis/wit/workItems/142/comments",
            api_version="7.1-preview.4",
            format="markdown",
        ),
        method="POST",
        json={"workItemId": 142, "commentId": 42, "text": "# Plan"},
    )

    comment = await azdo.add_work_item_comment(PROJECT, 142, "# Plan\n- step 1")

    assert comment["commentId"] == 42
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    # WIT comments is the ONE preview stripe forge pins (7.1-preview.4).
    assert request.url.params["api-version"] == "7.1-preview.4"
    assert request.url.params["format"] == "markdown"
    assert json.loads(request.content) == {"text": "# Plan\n- step 1"}


async def test_get_work_item_comments_uses_preview_stripe(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{BASE}/{PROJECT}/_apis/wit/workItems/142/comments",
            api_version="7.1-preview.4",
        ),
        json={"totalCount": 1, "comments": [{"commentId": 42, "text": "# Plan"}]},
    )

    batch = await azdo.get_work_item_comments(PROJECT, 142)

    assert batch["comments"][0]["commentId"] == 42
    (request,) = httpx_mock.get_requests()
    assert request.url.params["api-version"] == "7.1-preview.4"


async def test_add_work_item_comment_is_never_retried(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    url = azdo_url(
        f"{BASE}/{PROJECT}/_apis/wit/workItems/142/comments",
        api_version="7.1-preview.4",
        format="markdown",
    )
    httpx_mock.add_response(url=url, method="POST", status_code=500)

    with pytest.raises(AzureDevOpsError):
        await azdo.add_work_item_comment(PROJECT, 142, "# Plan")

    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


# ---------------------------------------------------------------------------
# Pipelines / builds (research §6)
# ---------------------------------------------------------------------------


async def test_run_pipeline_fixture_body_and_run_id(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/pipelines/207/runs"),
        method="POST",
        json=load_fixture("pipeline_run_created.json"),
    )

    run = await azdo.run_pipeline(
        PROJECT,
        207,
        ref_name="refs/heads/forge/wi-42",
        template_parameters={
            "run_id": "run-7f3a",
            "attempt_base": BASE_SHA,
            "driver": "claude-code",
            "model": "glm-5",
            "work_item_id": "142",
        },
    )

    # The response CARRIES the run id — the correlation handle (§6.2).
    assert run.run_id == 99001
    assert run.state == "inProgress"
    assert run.result is None
    assert run.url == load_fixture("pipeline_run_created.json")["url"]
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(request.content)
    assert body["resources"] == {"repositories": {"self": {"refName": "refs/heads/forge/wi-42"}}}
    assert body["templateParameters"]["attempt_base"] == BASE_SHA
    assert body["templateParameters"]["work_item_id"] == "142"
    assert "variables" not in body  # omitted when not provided


async def test_run_pipeline_with_variables(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/pipelines/207/runs"),
        method="POST",
        json={"id": 99002, "state": "inProgress", "result": None},
    )

    await azdo.run_pipeline(
        PROJECT,
        207,
        ref_name="refs/heads/forge/wi-43",
        variables={"EXTRA": {"value": "1", "isSecret": False}},
    )

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["variables"] == {"EXTRA": {"value": "1", "isSecret": False}}


async def test_run_pipeline_is_never_retried(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    url = azdo_url(f"{BASE}/{PROJECT}/_apis/pipelines/207/runs")
    httpx_mock.add_response(url=url, method="POST", status_code=500)

    with pytest.raises(AzureDevOpsError):
        await azdo.run_pipeline(PROJECT, 207, ref_name="refs/heads/forge/wi-42")

    # A replay would start a SECOND harness run.
    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


async def test_get_run_normalized(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/pipelines/207/runs/99001"),
        json={"id": 99001, "state": "completed", "result": "succeeded", "url": "u"},
    )

    run = await azdo.get_run(PROJECT, 207, 99001)

    assert (run.run_id, run.state, run.result) == (99001, "completed", "succeeded")


async def test_get_build(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/build/builds/88231"),
        json={"id": 88231, "result": "failed", "sourceVersion": MERGE_2},
    )

    build = await azdo.get_build(PROJECT, 88231)

    assert build["sourceVersion"] == MERGE_2


async def test_cancel_build_sends_patch_cancelling(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/build/builds/88231"),
        method="PATCH",
        json={"id": 88231, "status": "cancelling"},
    )

    build = await azdo.cancel_build(PROJECT, 88231)

    assert build["status"] == "cancelling"
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "PATCH"]
    # The Runs area has NO cancel — cancellation goes via the Builds PATCH.
    assert json.loads(request.content) == {"status": "cancelling"}


async def test_list_builds_by_repository_documented_params_only(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{BASE}/{PROJECT}/_apis/build/builds",
            repositoryId="1a2b3c4d-0000-0000-0000-000000000001",
            definitions="207,999",
            minTime="2026-09-15T12:05:00Z",
            queryOrder="queueTimeDescending",
            **{"$top": "25"},
        ),
        json={"value": [{"id": 88231, "sourceVersion": MERGE_2}], "count": 1},
    )

    builds = await azdo.list_builds_by_repository(
        PROJECT,
        "1a2b3c4d-0000-0000-0000-000000000001",
        definitions=[207, 999],
        min_time=datetime(2026, 9, 15, 12, 5, tzinfo=timezone.utc),
    )

    assert builds[0]["id"] == 88231
    (request,) = httpx_mock.get_requests()
    params = request.url.params
    assert params["repositoryId"] == "1a2b3c4d-0000-0000-0000-000000000001"
    assert params["definitions"] == "207,999"
    assert params["minTime"] == "2026-09-15T12:05:00Z"
    assert params["queryOrder"] == "queueTimeDescending"
    assert params["$top"] == "25"
    # Research correction #1: builds has NO sourceVersion filter — forge must
    # not invent one; correlation is client-side.
    assert "sourceVersion" not in params


async def test_get_timeline_fixture_records(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/build/builds/88231/timeline"),
        json=load_fixture("build_timeline_failed.json"),
    )

    timeline = await azdo.get_timeline(PROJECT, 88231)

    failed_tasks = [
        record
        for record in timeline["records"]
        if record["type"] == "Task" and record["result"] == "failed"
    ]
    assert [record["log"]["id"] for record in failed_tasks] == [5]
    assert "driver crashed after 12 steps" in failed_tasks[0]["issues"][0]["message"]


async def test_get_task_log_returns_plain_text(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/build/builds/88231/logs/5"),
        text="2026-09-15T12:06:00Z step 1 ok\n##[error]harness exit code 1\n",
    )

    log = await azdo.get_task_log(PROJECT, 88231, 5)

    assert "##[error]harness exit code 1" in log


async def test_get_run_artifact_signed_url(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    signed = "https://signed.test/download?sig=expiring"
    httpx_mock.add_response(
        url=azdo_url(
            f"{BASE}/{PROJECT}/_apis/pipelines/207/runs/99001/artifacts",
            artifactName="forge-candidate",
            **{"$expand": "signedContent"},
        ),
        json={"name": "forge-candidate", "signedContent": {"url": signed}},
    )

    url = await azdo.get_run_artifact_signed_url(PROJECT, 207, 99001, "forge-candidate")

    assert url == signed
    (request,) = httpx_mock.get_requests()
    assert request.url.params["$expand"] == "signedContent"
    assert request.url.params["artifactName"] == "forge-candidate"


async def test_get_run_artifact_without_signed_content_raises(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{BASE}/{PROJECT}/_apis/pipelines/207/runs/99001/artifacts",
            artifactName="missing-artifact",
            **{"$expand": "signedContent"},
        ),
        json={"count": 0, "value": []},
    )

    with pytest.raises(AzureDevOpsNotFoundError, match="signedContent"):
        await azdo.get_run_artifact_signed_url(PROJECT, 207, 99001, "missing-artifact")


# ---------------------------------------------------------------------------
# Service hooks (onboarding, research §2.0)
# ---------------------------------------------------------------------------


async def test_create_hook_subscription_body_contract(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/_apis/hooks/subscriptions"),
        method="POST",
        json={"id": "sub-1", "status": "enabled"},
    )

    subscription = await azdo.create_hook_subscription(
        "git.pullrequest.commented-on",
        {"repository": "1a2b3c4d-0000-0000-0000-000000000001"},
        "https://forge.example/webhook/azure_devops",
        "forge-hook",
        "hook-secret",
    )

    assert subscription["id"] == "sub-1"
    (request,) = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert str(request.url).startswith(f"{BASE}/_apis/hooks/subscriptions")  # org-level
    assert json.loads(request.content) == {
        "publisherId": "tfs",
        "eventType": "git.pullrequest.commented-on",
        "resourceVersion": "1.0",
        "consumerId": "webHooks",
        "consumerActionId": "httpRequest",
        "publisherInputs": {"repository": "1a2b3c4d-0000-0000-0000-000000000001"},
        "consumerInputs": {
            "url": "https://forge.example/webhook/azure_devops",
            "basicAuthUsername": "forge-hook",
            "basicAuthPassword": "hook-secret",
            "resourceDetailsToSend": "all",
        },
    }


async def test_create_hook_subscription_is_never_retried(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    url = azdo_url(f"{BASE}/_apis/hooks/subscriptions")
    httpx_mock.add_response(url=url, method="POST", status_code=500)

    with pytest.raises(AzureDevOpsError):
        await azdo.create_hook_subscription("git.push", {}, "https://f.example/h", "u", "p")

    assert len([r for r in httpx_mock.get_requests() if r.method == "POST"]) == 1


async def test_list_hook_subscriptions(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/_apis/hooks/subscriptions"),
        json={"value": [{"id": "sub-1", "eventType": "git.push"}], "count": 1},
    )

    subscriptions = await azdo.list_hook_subscriptions()

    assert [s["id"] for s in subscriptions] == ["sub-1"]


async def test_delete_hook_subscription(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/_apis/hooks/subscriptions/sub-1"), method="DELETE", status_code=204
    )

    await azdo.delete_hook_subscription("sub-1")

    (request,) = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
    assert str(request.url).startswith(f"{BASE}/_apis/hooks/subscriptions/sub-1")


# ---------------------------------------------------------------------------
# AzureRepositoryReader (AuthoritativeReader semantics)
# ---------------------------------------------------------------------------


async def test_reader_get_file_at_explicit_commit_sha(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/README.md",
            includeContent="true",
            **{"versionDescriptor.version": BASE_SHA, "versionDescriptor.versionType": "commit"},
        ),
        json={
            "path": "/README.md",
            "content": "hello world\n",
            "contentMetadata": {"size": 12},
            "isSymLink": False,
        },
    )

    repo_file = await reader.get_file(0, "/README.md", ref=BASE_SHA)

    (request,) = httpx_mock.get_requests()
    assert request.url.params["versionDescriptor.version"] == BASE_SHA
    assert request.url.params["versionDescriptor.versionType"] == "commit"
    assert repo_file.encoding == "base64"  # GitLab-schema compatible
    assert base64.b64decode(repo_file.content) == b"hello world\n"
    assert repo_file.file_name == "README.md"
    assert repo_file.size == 12


async def test_reader_get_file_head_omits_version_descriptor(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(ITEMS_URL, path="/README.md", includeContent="true"),
        json={"path": "/README.md", "content": "x"},
    )

    repo_file = await reader.get_file(0, "/README.md")

    assert repo_file.ref == "HEAD"
    (request,) = httpx_mock.get_requests()
    assert "versionDescriptor.version" not in request.url.params


async def test_reader_get_file_branch_ref_uses_branch_descriptor(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/README.md",
            includeContent="true",
            **{"versionDescriptor.version": "main", "versionDescriptor.versionType": "branch"},
        ),
        json={"path": "/README.md", "content": "x"},
    )

    await reader.get_file(0, "/README.md", ref="main")

    (request,) = httpx_mock.get_requests()
    assert request.url.params["versionDescriptor.version"] == "main"
    assert request.url.params["versionDescriptor.versionType"] == "branch"


async def test_reader_rejects_symlinks(httpx_mock: HTTPXMock, reader: AzureRepositoryReader):
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/link",
            includeContent="true",
            **{"versionDescriptor.version": BASE_SHA, "versionDescriptor.versionType": "commit"},
        ),
        json={"path": "/link", "content": "../secret", "isSymLink": True},
    )

    with pytest.raises(AzureDevOpsError, match="symlink"):
        await reader.get_file(0, "/link", ref=BASE_SHA)


async def test_reader_raises_when_items_api_returns_no_content(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    # Honesty over silent truncation: no inline content is an error, never
    # an empty file.
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/big.bin",
            includeContent="true",
            **{"versionDescriptor.version": BASE_SHA, "versionDescriptor.versionType": "commit"},
        ),
        json={"path": "/big.bin", "contentMetadata": {"size": 10_000_000}},
    )

    with pytest.raises(AzureDevOpsError, match="no inline content"):
        await reader.get_file(0, "/big.bin", ref=BASE_SHA)


async def test_reader_read_text_decodes(httpx_mock: HTTPXMock, reader: AzureRepositoryReader):
    httpx_mock.add_response(
        url=azdo_url(
            ITEMS_URL,
            path="/src/app.py",
            includeContent="true",
            **{"versionDescriptor.version": BASE_SHA, "versionDescriptor.versionType": "commit"},
        ),
        json={"path": "/src/app.py", "content": "print('hi')\n"},
    )

    assert await reader.read_text("/src/app.py", ref=BASE_SHA) == "print('hi')\n"


async def test_reader_get_tree_at_sha_maps_entries(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BASE_SHA}", recursive="true"),
        json={
            "treeEntries": [
                {"objectId": "t" * 40, "gitObjectType": "tree", "relativePath": "src"},
                {
                    "objectId": "b" * 40,
                    "gitObjectType": "blob",
                    "relativePath": "src/app.py",
                },
            ],
            "truncated": False,
        },
    )

    entries = await reader.get_tree(0, ref=BASE_SHA, recursive=True)

    assert [entry.path for entry in entries] == ["src", "src/app.py"]
    assert entries[1].type == "blob"
    assert entries[1].id == "b" * 40


async def test_reader_rejects_truncated_tree(httpx_mock: HTTPXMock, reader: AzureRepositoryReader):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BASE_SHA}", recursive="true"),
        json={"treeEntries": [{"objectId": "b" * 40, "relativePath": "a"}], "truncated": True},
    )

    with pytest.raises(AzureDevOpsError, match="truncated"):
        await reader.get_tree(0, ref=BASE_SHA, recursive=True)


async def test_reader_tree_filters_by_path_prefix(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BASE_SHA}", recursive="true"),
        json={
            "treeEntries": [
                {"objectId": "b" * 40, "gitObjectType": "blob", "relativePath": "src/app.py"},
                {"objectId": "c" * 40, "gitObjectType": "blob", "relativePath": "docs/x.md"},
            ],
            "truncated": False,
        },
    )

    entries = await reader.get_tree(0, path="src", ref=BASE_SHA, recursive=True)

    assert [entry.path for entry in entries] == ["src/app.py"]


async def test_reader_tree_resolves_branch_via_refs(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(REFS_URL, filter="heads/main"),
        json={"value": [{"name": "refs/heads/main", "objectId": BRANCH_TIP}]},
    )
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BRANCH_TIP}"),
        json={"treeEntries": [{"objectId": "b" * 40, "relativePath": "a"}], "truncated": False},
    )

    entries = await reader.get_tree(0, ref="main")

    assert entries[0].path == "a"
    (refs_request, _) = httpx_mock.get_requests()
    assert refs_request.url.params["filter"] == "heads/main"


async def test_reader_tree_head_resolves_default_branch(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(REPO_PATH),
        json=load_fixture("push_branch_create.json")["resource"]["repository"],
    )
    httpx_mock.add_response(
        url=azdo_url(REFS_URL, filter="heads/main"),
        json={"value": [{"name": "refs/heads/main", "objectId": BRANCH_TIP}]},
    )
    httpx_mock.add_response(
        url=azdo_url(f"{REPO_PATH}/trees/{BRANCH_TIP}"),
        json={"treeEntries": [], "truncated": False},
    )

    assert await reader.get_tree(0, ref="HEAD") == []

    repo_request, refs_request, _ = httpx_mock.get_requests()
    assert str(repo_request.url).startswith(f"{BASE}/{PROJECT}/_apis/git/repositories/{REPO}?")
    assert refs_request.url.params["filter"] == "heads/main"


async def test_reader_maps_work_item_to_issue(httpx_mock: HTTPXMock, reader: AzureRepositoryReader):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/wit/workItems/142"),
        json={
            "id": 142,
            "fields": {
                "System.Title": "Ship the flux capacitor",
                "System.Description": "Make it go to 88 mph.",
                "System.State": "Approved",
                "System.CreatedBy": {
                    "displayName": "Dev User",
                    "uniqueName": "dev@fabrikam.example",
                },
            },
            "_links": {"html": {"href": f"{BASE}/{PROJECT}/_workitems/edit/142"}},
        },
    )

    issue = await reader.get_issue(0, 142)

    assert issue.iid == 142
    assert issue.title == "Ship the flux capacitor"
    assert issue.state == "Approved"
    assert issue.web_url == f"{BASE}/{PROJECT}/_workitems/edit/142"
    # AzDO identity ids are GUIDs — id is 0, uniqueName carries the identity.
    assert issue.author is not None
    assert issue.author.id == 0
    assert issue.author.username == "dev@fabrikam.example"


async def test_reader_maps_work_item_with_string_created_by(
    httpx_mock: HTTPXMock, reader: AzureRepositoryReader
):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/wit/workItems/142"),
        json={"id": 142, "fields": {"System.Title": "T", "System.CreatedBy": "Dev User"}},
    )

    issue = await reader.get_issue(0, 142)

    assert issue.author is not None
    assert issue.author.name == "Dev User"


# ---------------------------------------------------------------------------
# AZ-4 additions: work-item patch/link, repo resolution, draft-PR dedupe
# (research §4.6; additive to the frozen AZ-1/AZ-3 client surface)
# ---------------------------------------------------------------------------


WORK_ITEM_URL = f"{BASE}/{PROJECT}/_apis/wit/workItems/142"


async def test_update_work_item_sends_the_json_patch_contract(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    ops = [{"op": "add", "path": "/fields/System.Title", "value": "renamed"}]
    httpx_mock.add_response(url=azdo_url(WORK_ITEM_URL), json={"id": 142, "rev": 10})

    result = await azdo.update_work_item(PROJECT, 142, ops)

    assert result["rev"] == 10
    (request,) = httpx_mock.get_requests()
    assert request.method == "PATCH"
    assert request.url.params["api-version"] == "7.1"
    # The WIT PATCH only parses under the json-patch content type.
    assert request.headers["content-type"] == "application/json-patch+json"
    assert json.loads(request.content) == ops


async def test_update_work_item_is_never_retried(httpx_mock: HTTPXMock, azdo: AzureDevOpsClient):
    # A replayed relations/- add would duplicate the ArtifactLink — the
    # mutation primitive is retry-less like the other writes.
    httpx_mock.add_response(url=azdo_url(WORK_ITEM_URL), status_code=500, json={"message": "boom"})

    with pytest.raises(AzureDevOpsError):
        await azdo.update_work_item(PROJECT, 142, [])

    assert len(httpx_mock.get_requests()) == 1


async def test_link_work_item_to_pr_sends_the_artifact_link_patch(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    project_guid = "9f8e7d6c-0000-0000-0000-000000000009"
    repo_guid = "1a2b3c4d-0000-0000-0000-000000000001"
    httpx_mock.add_response(url=azdo_url(WORK_ITEM_URL), json={"id": 142})

    await azdo.link_work_item_to_pr(PROJECT, 142, project_guid, repo_guid, 512)

    (request,) = httpx_mock.get_requests()
    (op,) = json.loads(request.content)
    assert op["op"] == "add"
    assert op["path"] == "/relations/-"
    value = op["value"]
    assert value["rel"] == "ArtifactLink"
    # The exact artifactId template, %2F-separated, or the link renders
    # one-way (research §4.6).
    assert value["url"] == (f"vstfs:///Git/PullRequestId/{project_guid}%2F{repo_guid}%2F512")
    # CASE-SENSITIVE: "Pull Request", not "pull request".
    assert value["attributes"]["name"] == "Pull Request"


async def test_list_repositories_returns_the_project_repos(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/git/repositories"),
        json={
            "value": [
                {
                    "id": "1a2b3c4d-0000-0000-0000-000000000001",
                    "name": "core",
                    "project": {"id": "9f8e7d6c-0000-0000-0000-000000000009", "name": PROJECT},
                }
            ],
            "count": 1,
        },
    )

    repositories = await azdo.list_repositories(PROJECT)

    assert [repo["name"] for repo in repositories] == ["core"]
    (request,) = httpx_mock.get_requests()
    assert request.url.params["api-version"] == "7.1"


async def test_list_repositories_accepts_a_bare_array(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(f"{BASE}/{PROJECT}/_apis/git/repositories"),
        json=[{"id": "1a2b3c4d-0000-0000-0000-000000000001", "name": "core"}],
    )

    repositories = await azdo.list_repositories(PROJECT)

    assert repositories[0]["name"] == "core"


def _pr_payload(
    pr_id: int,
    *,
    source_branch: str,
    is_draft: bool,
    created: str,
) -> dict:
    return {
        "pullRequestId": pr_id,
        "sourceRefName": source_branch,
        "targetRefName": "refs/heads/main",
        "isDraft": is_draft,
        "status": "active",
        "creationDate": created,
    }


async def test_find_draft_pr_by_head_returns_the_newest_draft_on_the_branch(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{REPO_PATH}/pullrequests",
            **{"searchCriteria.status": "active", "$top": "50"},
        ),
        json={
            "value": [
                # An older draft on the same head...
                _pr_payload(
                    500,
                    source_branch="refs/heads/forge/wi-42",
                    is_draft=True,
                    created="2026-09-14T10:00:00Z",
                ),
                # ...the NEWEST draft wins...
                _pr_payload(
                    512,
                    source_branch="refs/heads/forge/wi-42",
                    is_draft=True,
                    created="2026-09-15T10:00:00Z",
                ),
                # ...non-draft and other-branch PRs never match.
                _pr_payload(
                    513,
                    source_branch="refs/heads/forge/wi-42",
                    is_draft=False,
                    created="2026-09-15T11:00:00Z",
                ),
                _pr_payload(
                    514,
                    source_branch="refs/heads/dev/topic",
                    is_draft=True,
                    created="2026-09-15T12:00:00Z",
                ),
            ]
        },
    )

    pr = await azdo.find_draft_pr_by_head(PROJECT, REPO, "forge/wi-42")

    assert pr is not None and pr["pullRequestId"] == 512


async def test_find_draft_pr_by_head_returns_none_without_a_draft(
    httpx_mock: HTTPXMock, azdo: AzureDevOpsClient
):
    httpx_mock.add_response(
        url=azdo_url(
            f"{REPO_PATH}/pullrequests",
            **{"searchCriteria.status": "active", "$top": "50"},
        ),
        json={
            "value": [
                _pr_payload(
                    513,
                    source_branch="refs/heads/forge/wi-42",
                    is_draft=False,
                    created="2026-09-15T11:00:00Z",
                ),
            ]
        },
    )

    assert await azdo.find_draft_pr_by_head(PROJECT, REPO, "forge/wi-42") is None


# ---------------------------------------------------------------------------
# Fixture inventory + SHA chain consistency (research §10)
# ---------------------------------------------------------------------------


def test_fixture_inventory_is_complete():
    assert {p.name for p in FIXTURES.glob("*.json")} == EXPECTED_FIXTURES


@pytest.mark.parametrize(
    "name,event_type",
    [
        ("push_branch_create.json", "git.push"),
        ("push_normal.json", "git.push"),
        ("pr_created.json", "git.pullrequest.created"),
        ("pr_updated_push.json", "git.pullrequest.updated"),
        ("pr_commented_on.json", "ms.vss-code.git-pullrequest-comment-event"),
        ("build_complete_failed.json", "build.complete"),
        ("workitem_commented_implement.json", "workitem.commented"),
    ],
)
def test_webhook_fixtures_carry_envelope_and_event_type(name: str, event_type: str):
    payload = load_fixture(name)
    assert payload["eventType"] == event_type
    assert payload["publisherId"] == "tfs"
    assert payload["resourceContainers"]["project"]["baseUrl"] == "https://dev.azure.com/fabrikam/"


def test_fixture_sha_chain_is_consistent():
    branch_create = load_fixture("push_branch_create.json")["resource"]
    push_normal = load_fixture("push_normal.json")["resource"]
    pr_created = load_fixture("pr_created.json")["resource"]
    pr_updated = load_fixture("pr_updated_push.json")["resource"]
    build = load_fixture("build_complete_failed.json")["resource"]
    run = load_fixture("pipeline_run_created.json")
    stale = load_fixture("push_response_stale_object_id.json")["value"][0]

    assert branch_create["refUpdates"][0]["oldObjectId"] == ZERO_SHA
    assert branch_create["refUpdates"][0]["newObjectId"] == BRANCH_TIP
    assert push_normal["refUpdates"][0]["oldObjectId"] == BRANCH_TIP
    assert push_normal["refUpdates"][0]["newObjectId"] == PUSH_TIP
    assert pr_created["lastMergeSourceCommit"]["commitId"] == PUSH_TIP
    assert pr_created["lastMergeCommit"]["commitId"] == MERGE_1
    assert pr_updated["lastMergeSourceCommit"]["commitId"] == NEW_HEAD
    assert pr_updated["lastMergeCommit"]["commitId"] == MERGE_2
    assert build["sourceVersion"] == MERGE_2  # build correlates to the PR merge
    assert build["definition"]["id"] == run["pipeline"]["id"] == 207
    assert run["templateParameters"]["attempt_base"] == BASE_SHA
    assert run["resources"]["repositories"]["self"]["version"] == NEW_HEAD
    assert stale["oldObjectId"] == PUSH_TIP  # forge believed PUSH_TIP was the tip
    assert stale["updateStatus"] == "staleObjectId"
