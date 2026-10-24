"""Concrete native adapters over bounded synthetic HTTP and exact Git object bytes."""

import base64
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from forge.providers.github.auth import AppAuth
from forge.providers.github.ci import GitHubCI
from forge.providers.github.client import API
from forge.providers.github.repo import GitHubRepo
from forge.providers.github.review import GitHubReview
from forge.providers.github.subject import GitHubSubject
from forge.providers.github.transport import Transport
from forge.providers.github.write import GitHubWrites
from tests import gitlab_engine_fixture as gl
from tests import test_github_reads as gh
from tests import test_github_rest as rest

app_key = gh.app_key


class Clock:
    def __init__(self):
        self.value = gh.Clock().now()

    def now(self):
        return self.value


class GitHubWire(rest.Wire):
    def __init__(self, clock):
        super().__init__()
        self.clock, self.mints = clock, []
        self.metadata_response = None

    def __call__(self, request):
        if request.url.path.endswith("/access_tokens"):
            self.calls.append(request)
            self.mints.append(request)
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-token-" + str(len(self.mints)),
                    "expires_at": (self.clock.now() + timedelta(hours=1)).isoformat(),
                    "repository_selection": "selected",
                    "repositories": [{"id": 123}],
                    "permissions": body["permissions"],
                },
            )
        if request.url.path == "/installation/repositories" and self.metadata_response:
            self.calls.append(request)
            value = self.metadata_response
            return value(request) if callable(value) else value
        return super().__call__(request)


@pytest.fixture
async def github(app_key):
    clock = Clock()
    wire = GitHubWire(clock)
    transport = Transport(transport=httpx.MockTransport(wire))
    auth = AppAuth("conn-1", "app-9", app_key, {"123": 900}, clock, transport)
    api = API(auth, transport)
    repositories, reviews = GitHubRepo(api), GitHubReview(api, bot=gh.BOT)

    async def lookup(repo, nonce):
        return (
            rest.DISPATCH
            if repo.identity == gh.REPO.identity and nonce == rest.DISPATCH.nonce
            else None
        )

    ci = GitHubCI(
        api,
        clock,
        dispatch_lookup=lookup,
        lane_workflow_path=rest.WORKFLOW,
        verification_workflow_paths={".github/workflows/ci.yml"},
    )
    writes = GitHubWrites(
        api,
        bot=gh.BOT,
        repositories=repositories,
        reviews=reviews,
        ci=ci,
        committer=rest.NoCommitter(),
        requirements_url="https://release.example/pins",
        requirements_sha256="a" * 64,
    )
    try:
        yield SimpleNamespace(
            api=api,
            auth=auth,
            transport=transport,
            wire=wire,
            clock=clock,
            repo=gh.REPO,
            subject=gh.SUBJECT,
            review=rest.REVIEW,
            repositories=repositories,
            subjects=GitHubSubject(api, bot=gh.BOT),
            reviews=reviews,
            ci=ci,
            writes=writes,
            dispatch=rest.DISPATCH,
            path="src/file.txt",
            sha=gh.COMMIT,
            tree=gh.TREE,
        )
    finally:
        await transport.close()


@pytest.fixture
async def gitlab():
    wire = gl.Wire()
    api, repositories, reviews, subjects, ci, writes = gl.adapters(wire)
    try:
        yield SimpleNamespace(
            api=api,
            transport=api.transport,
            wire=wire,
            repo=gl.REPO,
            subject=gl.SUBJECT,
            review=gl.REVIEW,
            repositories=repositories,
            subjects=subjects,
            reviews=reviews,
            ci=ci,
            writes=writes,
            dispatch=gl.DISPATCH,
            path="old.txt",
            sha=gl.SHA,
            tree=gl.SHA,
        )
    finally:
        await api.transport.close()


@pytest.fixture(params=["github", "gitlab"])
def blob_case(request):
    return request.getfixturevalue(request.param)


def blob(env, body, *, encoding="base64", path=None):
    """Install an exact immutable native tree/blob pair, including empty/binary bytes."""
    oid = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    document = {
        "sha": oid,
        "size": len(body),
        "encoding": encoding,
        "content": base64.b64encode(body).decode(),
    }
    if env.repo.identity.provider == "github":
        env.wire.routes["git/trees/" + env.tree]["tree"] = [
            {"path": path or env.path, "sha": oid, "mode": "100644", "type": "blob"}
        ]
        env.wire.routes["git/blobs/" + oid] = document
    else:
        env.wire.routes["repository/tree"] = [
            {"path": path or env.path, "id": oid, "mode": "100644", "type": "blob"}
        ]
        env.wire.routes["repository/blobs/" + oid] = document
    return document


def missing(env, response):
    if env.repo.identity.provider == "github":
        env.wire.routes["git/trees/" + env.tree]["tree"] = []
        env.wire.routes["contents/" + env.path] = response
    else:
        env.wire.routes["repository/tree"] = []
        env.wire.routes["repository/files/" + env.path] = response


def resource_calls(env, suffix, method=None):
    return [
        r
        for r in env.wire.calls
        if r.url.path.endswith(suffix) and (method is None or r.method == method)
    ]


def timeout(request):
    raise httpx.ReadTimeout("synthetic timeout", request=request)


def jobs(env, *, path=".github/workflows/ci.yml", name="required"):
    env.wire.routes["actions/runs"] = {"total_count": 1, "workflow_runs": [rest.run(path=path)]}
    env.wire.routes["actions/runs/700/attempts/1/jobs"] = {
        "total_count": 1,
        "jobs": [
            {
                "id": 901,
                "run_id": 700,
                "run_attempt": 1,
                "head_sha": env.sha,
                "name": name,
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }


class NativeGitLabWire:
    """Only HTTP observation is synthetic; all Git objects/ref transactions are real."""

    def __init__(self, server):
        self.server, self.calls = server, []

    def __call__(self, request):
        from tests import test_native_git_publication as proof

        self.calls.append(request)
        resource = request.url.path.removeprefix("/api/v4")
        if resource == "/user":
            assert request.headers["private-token"] == "synthetic-write"
            value = gl.user()
        elif resource == "/projects/123":
            value = {
                "id": 123,
                "path_with_namespace": "owner/repository",
                "http_url_to_repo": proof.URL,
                "default_branch": "main",
                "archived": False,
            }
        else:
            assert resource.startswith("/projects/123/") and request.method == "GET"
            resource = resource.removeprefix("/projects/123/")
            head = proof.git(self.server, "rev-parse", "refs/heads/" + proof.BRANCH).decode()
            if resource.startswith("repository/branches/"):
                value = {"name": proof.BRANCH, "commit": {"id": head}}
            elif resource == "repository/commits":
                value = [
                    self.commit(oid)
                    for oid in proof.git(self.server, "rev-list", request.url.params["ref_name"])
                    .decode()
                    .splitlines()
                ]
            elif resource.startswith("repository/commits/"):
                value = self.commit(resource.rsplit("/", 1)[-1])
            elif resource == "repository/tree":
                value = []
                for row in proof.git(
                    self.server, "ls-tree", "-r", "-z", request.url.params["ref"], raw=True
                ).split(b"\0"):
                    if row:
                        attrs, name = row.split(b"\t", 1)
                        mode, kind, oid = attrs.decode().split()
                        value.append({"path": name.decode(), "mode": mode, "type": kind, "id": oid})
            else:
                raise AssertionError(resource)
        return httpx.Response(200, json=value)

    def commit(self, oid):
        from tests import test_native_git_publication as proof

        return {
            "id": oid,
            "message": proof.git(self.server, "show", "-s", "--format=%B", oid).decode(),
            "parent_ids": proof.git(self.server, "show", "-s", "--format=%P", oid).decode().split(),
        }


@pytest.fixture
async def gitlab_native(native):
    from pydantic import SecretStr

    from forge.providers.gitlab.client import API, Credentials
    from forge.providers.gitlab.commit import GitLabCommitter
    from forge.providers.gitlab.repo import GitLabRepo
    from forge.providers.gitlab.transport import Endpoint, Transport
    from tests import test_native_git_publication as proof

    server, seed, base, calls = native
    wire = NativeGitLabWire(server)
    transport = Transport(Endpoint("https://fixture.test"), transport=httpx.MockTransport(wire))
    api = API(
        "ce-1",
        transport,
        {"123": Credentials(SecretStr("synthetic-read"), SecretStr("synthetic-write"), gl.BOT)},
    )
    repositories = GitLabRepo(api)
    writer = GitLabCommitter(api, repositories, gl.Clock())
    # Native fixture substitutes its one explicitly owned URL for a local bare receiver.
    try:
        yield SimpleNamespace(
            writer=writer,
            repositories=repositories,
            repo=gl.REPO,
            api=api,
            wire=wire,
            server=server,
            seed=seed,
            base=base,
            calls=calls,
            candidate=proof.candidate(base),
        )
    finally:
        await transport.close()
