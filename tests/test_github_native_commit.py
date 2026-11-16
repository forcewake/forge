"""Selected App HTTP authority plus real native Git publication and recovery."""

import json
from datetime import timedelta

import httpx
import pytest

from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.port_values import Found, Inconclusive
from forge.providers.git_process import GitFailure
from forge.providers.github.auth import AppAuth
from forge.providers.github.client import API
from forge.providers.github.commit import GitHubCommitter
from forge.providers.github.repo import GitHubRepo
from forge.providers.github.transport import Endpoint, Transport
from tests import test_github_reads as reads
from tests import test_native_git_publication as native_git

app_key, native = reads.app_key, native_git.native
BRANCH, git = native_git.BRANCH, native_git.git


class Wire:
    def __init__(self, server):
        self.server, self.calls = server, []
        self.metadata = {
            "id": 123,
            "full_name": "owner/repository",
            "default_branch": "main",
            "archived": False,
            "disabled": False,
        }
        self.protected, self.rules_status, self.rules_body = False, 200, []

    def head(self):
        return git(self.server, "rev-parse", "refs/heads/" + BRANCH).decode()

    def route(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            requested = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-credential",
                    "expires_at": (reads.Clock().now() + timedelta(minutes=45)).isoformat(),
                    "repository_selection": "selected",
                    "repositories": [{"id": 123}],
                    "permissions": requested["permissions"],
                },
            )
        if path == "/installation/repositories":
            return httpx.Response(200, json={"total_count": 1, "repositories": [self.metadata]})
        prefix = "/repos/owner/repository/"
        assert path.startswith(prefix)
        resource = path.removeprefix(prefix)
        if resource == "branches/" + BRANCH:
            value = {"name": BRANCH, "commit": {"sha": self.head()}, "protected": self.protected}
        elif resource == "rules/branches/" + BRANCH:
            return httpx.Response(self.rules_status, json=self.rules_body)
        elif resource == "git/ref/heads/" + BRANCH:
            value = {
                "ref": "refs/heads/" + BRANCH,
                "object": {"type": "commit", "sha": self.head()},
            }
        elif resource == "commits":
            value = [
                {
                    "sha": sha,
                    "parents": [
                        {"sha": parent}
                        for parent in git(self.server, "show", "-s", "--format=%P", sha)
                        .decode()
                        .split()
                    ],
                    "commit": {
                        "message": git(self.server, "show", "-s", "--format=%B", sha).decode()
                    },
                }
                for sha in git(self.server, "rev-list", self.head()).decode().splitlines()
            ]
        elif resource.startswith("git/commits/"):
            sha = resource.removeprefix("git/commits/")
            value = {
                "sha": sha,
                "tree": {"sha": git(self.server, "rev-parse", sha + "^{tree}").decode()},
            }
        elif resource.startswith("git/trees/"):
            sha = resource.removeprefix("git/trees/")
            entries = []
            for row in git(self.server, "ls-tree", "-r", "-z", sha, raw=True).split(b"\0"):
                if row:
                    attrs, name = row.split(b"\t", 1)
                    mode, kind, oid = attrs.decode().split()
                    entries.append({"path": name.decode(), "mode": mode, "type": kind, "sha": oid})
            value = {"sha": sha, "truncated": False, "tree": entries}
        else:
            raise AssertionError(resource)
        assert request.method == "GET"
        return httpx.Response(200, json=value)


@pytest.fixture
async def connected(native, app_key):
    server, seed, base, calls = native
    wire = Wire(server)
    transport = Transport(
        Endpoint(git_url="https://fixture.test"), transport=httpx.MockTransport(wire.route)
    )
    auth = AppAuth("conn-1", "app-9", app_key, {"123": 900}, reads.Clock(), transport)
    api = API(auth, transport)
    repo = GitHubRepo(api)
    writer = GitHubCommitter(api, auth, repo, reads.Clock(), bot=reads.BOT)
    try:
        yield writer, repo, wire, server, seed, base, calls
    finally:
        await transport.close()


async def test_selected_app_publication_and_exact_native_probe(connected):
    writer, repo, wire, server, _, base, calls = connected
    candidate = native_git.candidate(base)
    result = await writer.commit(reads.REPO, candidate, base, "native-effect")
    assert isinstance(result, Committed) and result.sha == wire.head()
    recovered = await repo.probe_commit(reads.REPO, BRANCH, "native-effect", base, candidate)
    assert isinstance(recovered, Found) and recovered.value.sha == result.sha
    assert git(server, "show", result.sha + ":src/link") == b"file.txt"
    assert len([args for args in calls if args[0] == "push"]) == 1
    tokens = [
        json.loads(request.content)
        for request in wire.calls
        if request.url.path.endswith("/access_tokens")
    ]
    assert all(row["repository_ids"] == [123] for row in tokens)
    assert any(row["permissions"]["contents"] == "write" for row in tokens)


@pytest.mark.parametrize("policy", ("protected", "archived", "disabled", "default", "rules"))
async def test_native_publication_refuses_policy_before_git(connected, policy):
    writer, _, wire, _, _, base, calls = connected
    if policy == "protected":
        wire.protected = True
    elif policy == "default":
        wire.metadata["default_branch"] = BRANCH
    elif policy == "rules":
        wire.rules_body = [{"type": "pull_request"}]
    else:
        wire.metadata[policy] = True
    assert isinstance(
        await writer.commit(reads.REPO, native_git.candidate(base), base, "native"), Unknown
    )
    assert wire.head() == base and not calls


async def test_free_private_rules_403_still_publishes(connected):
    writer, _, wire, _, _, base, calls = connected
    wire.rules_status = 403
    result = await writer.commit(reads.REPO, native_git.candidate(base), base, "native")
    assert isinstance(result, Committed) and calls


async def test_native_publication_stale_head_preserves_human(connected):
    writer, _, wire, server, seed, base, calls = connected
    human = native_git.human_commit(seed, server, base)
    git(server, "update-ref", "refs/heads/" + BRANCH, human, base)
    result = await writer.commit(reads.REPO, native_git.candidate(base), base, "stale")
    assert isinstance(result, Conflict) and result.actual_head == human
    assert wire.head() == human and not calls


async def test_lost_native_response_recovers_historical_exact_tree_and_preserves_human(
    connected, monkeypatch
):
    writer, repo, wire, server, seed, base, _ = connected
    from forge.providers.github import commit as module

    actual = module.publish

    async def lost(*args, **kwargs):
        await actual(*args, **kwargs)
        raise GitFailure("git_operation_unavailable")

    monkeypatch.setattr(module, "publish", lost)
    candidate = native_git.candidate(base)
    assert isinstance(await writer.commit(reads.REPO, candidate, base, "lost"), Unknown)
    accepted = wire.head()
    git(seed, "fetch", "--quiet", str(server), "refs/heads/" + BRANCH)
    git(seed, "reset", "--hard", "--quiet", accepted)
    (seed / "human.txt").write_text("Human after publication\n")
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "Human after publication")
    git(seed, "push", "--quiet", str(server), "HEAD:refs/heads/" + BRANCH)
    human = wire.head()
    assert human != accepted
    recovered = await repo.probe_commit(reads.REPO, BRANCH, "lost", base, candidate)
    assert isinstance(recovered, Found) and isinstance(recovered.value, Committed)
    assert recovered.value.sha == accepted and wire.head() == human
    wrong = native_git.candidate(base)
    from dataclasses import replace

    wrong = replace(
        wrong, manifest=(replace(wrong.manifest[0], new_content="forged"), *wrong.manifest[1:])
    )
    assert isinstance(
        await repo.probe_commit(reads.REPO, BRANCH, "lost", base, wrong), Inconclusive
    )
