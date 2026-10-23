"""Native-ID GitHub read contracts through the real bounded HTTP/client layer."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import pytest
from forge_protocol import digest

from forge.domain.candidate import CandidateBundle, CreateFile, validate_candidate_bundle
from forge.domain.plan import RepositoryIdentity
from forge.port_values import (
    Actor,
    Applied,
    CommentRef,
    Found,
    Inconclusive,
    ReadUnavailable,
    RepoRef,
    SubjectRef,
)
from forge.providers.github.actor import GitHubActor
from forge.providers.github.auth import AppAuth
from forge.providers.github.client import API
from forge.providers.github.repo import GitHubRepo
from forge.providers.github.subject import GitHubSubject
from forge.providers.github.transport import Transport

COMMIT = "a" * 40
TREE = "b" * 40
PARENT = "c" * 40
BODY = b"approved file\n"
BLOB = hashlib.sha1(b"blob " + str(len(BODY)).encode() + b"\0" + BODY).hexdigest()
MARKER = "<!-- forge:effect:abc -->"
REPO = RepoRef(
    identity=RepositoryIdentity(provider="github", connection_id="conn-1", repository_id="123"),
    full_name="old/name",
)
SUBJECT = SubjectRef(repo=REPO, number=7)
BOT = Actor(
    provider="github", connection_id="conn-1", native_id="99", login="forge[bot]", is_bot=True
)
HUMAN = Actor(
    provider="github", connection_id="conn-1", native_id="42", login="reviewer", is_bot=False
)


class Clock:
    def now(self):
        return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def app_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class Wire:
    def __init__(self):
        self.metadata = {"id": 123, "full_name": "new/repository"}
        self.metadata_status = 200
        self.calls = []
        self.routes = {
            "git/ref/heads/main": {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": COMMIT},
            },
            "git/commits/" + COMMIT: {"sha": COMMIT, "tree": {"sha": TREE}},
            "git/trees/" + TREE: {
                "sha": TREE,
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree", "mode": "040000", "sha": "d" * 40},
                    {
                        "path": "src/file.txt",
                        "type": "blob",
                        "mode": "100644",
                        "sha": BLOB,
                        "size": len(BODY),
                    },
                    {
                        "path": "other.txt",
                        "type": "blob",
                        "mode": "100755",
                        "sha": BLOB,
                        "size": len(BODY),
                    },
                    {"path": "vendor/lib", "type": "commit", "mode": "160000", "sha": "e" * 40},
                ],
            },
            "git/blobs/" + BLOB: {
                "sha": BLOB,
                "size": len(BODY),
                "encoding": "base64",
                "content": base64.b64encode(BODY).decode() + "\n",
            },
            "issues/7": {
                "id": 70,
                "number": 7,
                "title": "Implement safely",
                "body": "Keep the approved input.",
                "updated_at": "2026-10-23T10:00:00Z",
                "labels": [{"name": "forge"}],
            },
            "issues/7/comments": [],
            "commits": [
                {
                    "sha": COMMIT,
                    "parents": [{"sha": PARENT}],
                    "commit": {"message": "Implementation\n\n" + MARKER},
                }
            ],
            "collaborators/reviewer/permission": {
                "permission": "write",
                "user": {"id": 42, "login": "reviewer", "type": "User"},
            },
        }

    def __call__(self, request):
        self.calls.append(request)
        assert request.headers.get("authorization", "").startswith("Bearer ")
        if request.url.path == "/app/installations/900/access_tokens":
            body = json.loads(request.content)
            assert body["repository_ids"] == [123]
            assert set(body["permissions"].values()) == {"read"}
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-read-token",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "repositories": [{"id": 123}],
                    "repository_selection": "selected",
                    "permissions": body["permissions"],
                },
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                self.metadata_status,
                json={"total_count": 1, "repositories": [self.metadata]},
                headers={"x-github-request-id": "AB:12"},
            )
        prefix = "/repos/" + self.metadata["full_name"] + "/"
        assert request.url.path.startswith(prefix), request.url
        resource = request.url.path[len(prefix) :]
        response = self.routes.get(resource, httpx.Response(404, json={"message": "not found"}))
        if callable(response):
            response = response(request)
        return (
            response
            if isinstance(response, httpx.Response)
            else httpx.Response(200, json=response, headers={"x-github-request-id": "CD:34"})
        )


@pytest.fixture
async def provider(app_key):
    wire = Wire()
    transport = Transport(transport=httpx.MockTransport(wire))
    auth = AppAuth("conn-1", "app-9", app_key, {"123": 900}, Clock(), transport)
    api = API(auth, transport)
    try:
        yield (
            GitHubRepo(api),
            GitHubSubject(api, bot=BOT),
            GitHubActor(api, bot=BOT, approver_ids={"42"}),
            wire,
        )
    finally:
        await transport.close()


def comment(identifier=10, body=MARKER, *, user=None):
    return {
        "id": identifier,
        "body": body,
        "user": user or {"id": 99, "login": "forge[bot]", "type": "Bot"},
        "issue_url": "https://api.github.com/repos/new/repository/issues/7",
    }


async def test_frozen_tree_keeps_commit_identity_and_native_repo_resolution(provider):
    repo, _, _, wire = provider
    tree = await repo.read_tree(REPO, COMMIT, "src")
    assert tree.sha == COMMIT and tree.sha != TREE
    assert [entry.path for entry in tree.entries] == ["src", "src/file.txt"]
    assert (await repo.read_tree(REPO, COMMIT)).entries[-1].kind == "commit"
    paths = [request.url.path for request in wire.calls if request.method == "GET"]
    assert all("old/name" not in value for value in paths)
    assert paths.count("/installation/repositories") == 4
    assert all(
        request.url.params.get("recursive") == "1"
        for request in wire.calls
        if "/git/trees/" in request.url.path
    )


@pytest.mark.parametrize(
    "change",
    [
        {"sha": "0" * 40},
        {"truncated": True},
        {"truncated": 0},
        {"tree": None},
        {"tree": [{"path": "../escape", "type": "blob", "mode": "100644", "sha": BLOB}]},
        {"tree": [{"path": ".git/config", "type": "blob", "mode": "100644", "sha": BLOB}]},
        {"tree": [{"path": "src/file", "type": "tree", "mode": "100644", "sha": BLOB}]},
    ],
)
async def test_tree_incomplete_malformed_or_unconfined_data_is_not_a_snapshot(provider, change):
    repo, _, _, wire = provider
    wire.routes["git/trees/" + TREE] = {**wire.routes["git/trees/" + TREE], **change}
    with pytest.raises(ReadUnavailable):
        await repo.read_tree(REPO, COMMIT)


async def test_blob_bytes_have_native_git_hash_size_and_frozen_commit_proof(provider):
    repo, _, _, wire = provider
    assert await repo.read_blob(REPO, COMMIT, "src/file.txt") == BODY
    assert await repo.read_blob(REPO, COMMIT, "missing.txt") is None
    last = wire.calls[-1]
    assert last.url.path.endswith("/contents/missing.txt") and last.url.params["ref"] == COMMIT
    with pytest.raises(ReadUnavailable):
        await repo.read_blob(REPO, COMMIT, "vendor/lib")
    with pytest.raises(ReadUnavailable):
        await repo.read_blob(REPO, COMMIT, "../secret")


@pytest.mark.parametrize(
    "change",
    [
        {"content": base64.b64encode(b"different").decode()},
        {"sha": "0" * 40},
        {"size": len(BODY) + 1},
        {"size": True},
        {"encoding": "utf-8"},
        {"content": "!!!!"},
        {"content": "AA\u00a0=="},
    ],
)
async def test_blob_corruption_or_ambiguous_encoding_is_refused(provider, change):
    repo, _, _, wire = provider
    wire.routes["git/blobs/" + BLOB] = {**wire.routes["git/blobs/" + BLOB], **change}
    with pytest.raises(ReadUnavailable):
        await repo.read_blob(REPO, COMMIT, "src/file.txt")


async def test_repository_access_failures_are_never_resource_absence(provider):
    repo, _, _, wire = provider
    wire.metadata_status = 404
    for operation in (repo.head(REPO, "main"), repo.read_blob(REPO, COMMIT, "missing.txt")):
        with pytest.raises(ReadUnavailable) as failure:
            await operation
        assert "synthetic-read-token" not in str(failure.value) + failure.value.evidence
    wire.metadata_status = 200
    wire.routes["git/ref/heads/main"] = httpx.Response(404)
    assert await repo.head(REPO, "main") is None
    assert isinstance(await repo.probe_branch(REPO, "main", PARENT), Inconclusive)


async def test_repo_scope_and_native_id_mismatch_fail_before_old_display_name_is_used(provider):
    repo, _, _, wire = provider
    foreign = REPO.model_copy(
        update={"identity": REPO.identity.model_copy(update={"connection_id": "other"})}
    )
    with pytest.raises(ReadUnavailable):
        await repo.head(foreign, "main")
    assert not wire.calls
    wire.metadata["id"] = 999
    with pytest.raises(ReadUnavailable):
        await repo.head(REPO, "main")
    assert not any("/repos/" in request.url.path for request in wire.calls)


async def test_commit_probe_is_parent_bound_and_duplicate_or_missing_markers_are_inconclusive(
    provider,
):
    repo, _, _, wire = provider
    candidate = validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=PARENT,
            driver_exit="completed",
            entries=(CreateFile("src/file.txt", BODY.decode()),),
        ),
        base_contents={},
        branch="main",
        commit_message="Implementation",
    )
    after = wire.routes["git/trees/" + TREE]["tree"]
    old_tree = "e" * 40
    wire.routes["git/commits/" + PARENT] = {"sha": PARENT, "tree": {"sha": old_tree}}
    wire.routes["git/trees/" + old_tree] = {
        "sha": old_tree,
        "truncated": False,
        "tree": [entry for entry in after if entry["path"] != "src/file.txt"],
    }
    result = await repo.probe_commit(REPO, "main", MARKER, PARENT, candidate)
    assert (
        isinstance(result, Found)
        and result.value.kind == "committed"
        and result.value.sha == COMMIT
    )
    original = next(entry for entry in after if entry["path"] == "src/file.txt")
    original["mode"] = "100755"
    assert isinstance(
        await repo.probe_commit(REPO, "main", MARKER, PARENT, candidate), Inconclusive
    )
    original["mode"] = "100644"
    after.append({"path": "unapproved", "type": "blob", "mode": "100644", "sha": BLOB})
    assert isinstance(
        await repo.probe_commit(REPO, "main", MARKER, PARENT, candidate), Inconclusive
    )
    after.pop()
    conflict = await repo.probe_commit(REPO, "main", MARKER, "0" * 40, candidate)
    assert isinstance(conflict, Found) and conflict.value.kind == "conflict"
    wire.routes["commits"].append(
        {"sha": "f" * 40, "parents": [{"sha": PARENT}], "commit": {"message": MARKER}}
    )
    assert isinstance(
        await repo.probe_commit(REPO, "main", MARKER, PARENT, candidate), Inconclusive
    )
    with pytest.raises(ReadUnavailable):
        await repo.find_commit_by_marker(REPO, "main", MARKER)
    wire.routes["commits"] = [{"sha": COMMIT, "parents": [], "commit": {"message": "no marker"}}]
    assert isinstance(
        await repo.probe_commit(REPO, "main", MARKER, PARENT, candidate), Inconclusive
    )
    assert all(
        request.url.params["sha"] == COMMIT
        for request in wire.calls
        if request.url.path.endswith("/commits")
    )


async def test_subject_snapshot_and_bot_comment_marker_have_exact_native_bindings(provider):
    _, subject, _, wire = provider
    task = await subject.get_task(SUBJECT)
    assert task.subject == SUBJECT and task.labels == ("forge",) and len(task.version) == 64
    wire.routes["issues/7/comments"] = [comment()]
    result = await subject.probe_comment(SUBJECT, MARKER)
    assert (
        isinstance(result, Found)
        and isinstance(result.value, Applied)
        and result.value.value.native_id == "10"
    )
    assert await subject.find_comment(SUBJECT, MARKER) == result.value.value
    wire.routes["issues/comments/10"] = comment()
    updated = await subject.probe_comment_update(
        CommentRef(subject=SUBJECT, native_id="10"), digest({"body": MARKER})
    )
    assert isinstance(updated, Found)
    wire.routes["issues/comments/10"] = comment(body="human edit")
    assert isinstance(
        await subject.probe_comment_update(
            CommentRef(subject=SUBJECT, native_id="10"), digest({"body": MARKER})
        ),
        Inconclusive,
    )


async def test_marker_spoof_duplicates_and_negative_visibility_never_authorize_replay(provider):
    _, subject, _, wire = provider
    for rows in (
        [comment(user={"id": 42, "login": "forge[bot]", "type": "User"})],
        [comment(), comment(11)],
        [comment(body="prefix " + MARKER)],
        [comment(body=MARKER + "\n" + MARKER)],
        [],
    ):
        wire.routes["issues/7/comments"] = rows
        assert isinstance(await subject.probe_comment(SUBJECT, MARKER), Inconclusive)
        with pytest.raises(ReadUnavailable):
            await subject.find_comment(SUBJECT, MARKER)


async def test_label_probe_requires_exact_current_state_and_issue_identity(provider):
    _, subject, _, wire = provider
    result = await subject.probe_label(SUBJECT, "forge", True)
    assert isinstance(result, Found) and result.value.value.present
    assert isinstance(await subject.probe_label(SUBJECT, "forge", False), Inconclusive)
    wire.routes["issues/7"]["number"] = 8
    with pytest.raises(ReadUnavailable):
        await subject.get_task(SUBJECT)
    assert isinstance(await subject.probe_label(SUBJECT, "forge", True), Inconclusive)


async def test_approver_needs_numeric_allowlist_login_identity_and_current_write_permission(
    provider,
):
    _, _, actor, wire = provider
    assert actor.bot_identity() == BOT
    assert await actor.is_approver(HUMAN, SUBJECT)
    wire.routes["collaborators/reviewer/permission"]["permission"] = "read"
    assert not await actor.is_approver(HUMAN, SUBJECT)
    wire.routes["collaborators/reviewer/permission"] = {
        "permission": "admin",
        "user": {"id": 666, "login": "reviewer", "type": "User"},
    }
    with pytest.raises(ReadUnavailable):
        await actor.is_approver(HUMAN, SUBJECT)
    before = len(wire.calls)
    assert not await actor.is_approver(HUMAN.model_copy(update={"native_id": "666"}), SUBJECT)
    assert not await actor.is_approver(HUMAN.model_copy(update={"is_bot": True}), SUBJECT)
    assert not await actor.is_approver(HUMAN.model_copy(update={"connection_id": "other"}), SUBJECT)
    assert len(wire.calls) == before
    with pytest.raises(ValueError):
        GitHubActor(actor.api, bot=BOT, approver_ids="42")


async def test_rate_limit_redirect_and_http_failures_remain_unknown_reads(provider):
    repo, _, actor, wire = provider
    for response in (
        httpx.Response(429, headers={"retry-after": "10"}),
        httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
        httpx.Response(302, headers={"location": "https://attacker.example"}),
        httpx.Response(500),
    ):
        wire.routes["git/ref/heads/main"] = response
        with pytest.raises(ReadUnavailable):
            await repo.head(REPO, "main")
    wire.routes["collaborators/reviewer/permission"] = httpx.Response(403)
    with pytest.raises(ReadUnavailable):
        await actor.is_approver(HUMAN, SUBJECT)


async def test_unsupported_resource_path_stays_a_failed_read(provider):
    repo, _, _, _ = provider
    with pytest.raises(ReadUnavailable):
        await repo.head(REPO, "topic\x7f")
    with pytest.raises(ReadUnavailable):
        await repo.read_blob(REPO, COMMIT, "invalid\x00file.txt")
    assert await repo.read_blob(REPO, COMMIT, "missing file.txt") is None
