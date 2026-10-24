"""Sanitized native CE HTTP shapes with mutable remote state and explicit faults."""

import base64
from datetime import datetime, timezone
import hashlib

import httpx
from forge_protocol import LaneDispatch
from pydantic import SecretStr

from forge.domain.plan import RepositoryIdentity
from forge.port_values import Actor, RepoRef, ReviewRef, SubjectRef
from forge.providers.gitlab.ci import GitLabCI
from forge.providers.gitlab.client import API, Credentials
from forge.providers.gitlab.repo import GitLabRepo
from forge.providers.gitlab.review import GitLabReview
from forge.providers.gitlab.subject import GitLabSubject
from forge.providers.gitlab.transport import Endpoint, Transport
from forge.providers.gitlab.write import GitLabWrites

SHA, OTHER = "a" * 40, "b" * 40
BODY = b"exact\r\n"
BLOB = hashlib.sha1(b"blob " + str(len(BODY)).encode() + b"\0" + BODY).hexdigest()
MARKER = "<!-- forge:effect:test -->"
DISCUSSION = "d" * 40
REPO = RepoRef(
    identity=RepositoryIdentity(provider="gitlab", connection_id="ce-1", repository_id="123"),
    full_name="old/name",
)
SUBJECT = SubjectRef(repo=REPO, number=7)
REVIEW = ReviewRef(repo=REPO, number=8)
BOT = Actor(
    provider="gitlab",
    connection_id="ce-1",
    native_id="99",
    login="project_123_bot_forge",
    is_bot=True,
)
HUMAN = Actor(
    provider="gitlab", connection_id="ce-1", native_id="42", login="reviewer", is_bot=False
)
DISPATCH = LaneDispatch(
    forge_run_id="run-1",
    attempt=0,
    generation=2,
    server_url="https://forge.example",
    provider="gitlab",
    repository="new/repository",
    ref="forge/run-1",
    base_sha=SHA,
    approved_digest="1" * 64,
    driver="claude_code",
    model="synthetic",
    timeout_seconds=60,
    nonce="nonce-123",
)


class Clock:
    def now(self):
        return datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


class NoCommitter:
    async def commit(self, *args):
        raise AssertionError("REST mutations cannot synthesize a native Git commit")


def user(bot=True):
    return {"id": 99 if bot else 42, "username": BOT.login if bot else HUMAN.login, "bot": bot}


def note(ident=10, body=MARKER, *, review=False, bot=True, **changes):
    value = {
        "id": ident,
        "body": body,
        "author": user(bot),
        "system": False,
        "noteable_type": "MergeRequest" if review else "Issue",
        "noteable_id": 80 if review else 70,
        "noteable_iid": 8 if review else 7,
        "project_id": 123,
        "created_at": "2026-10-23T10:00:00Z",
    }
    value.update(changes)
    return value


def mr(**changes):
    value = {
        "id": 80,
        "iid": 8,
        "project_id": 123,
        "source_project_id": 123,
        "target_project_id": 123,
        "source_branch": DISPATCH.ref,
        "target_branch": "main",
        "sha": SHA,
        "draft": True,
        "state": "opened",
        "author": user(),
    }
    value.update(changes)
    return value


def pipeline(ident=700, **changes):
    value = {
        "id": ident,
        "project_id": 123,
        "ref": DISPATCH.ref,
        "sha": SHA,
        "source": "api",
        "status": "success",
        "yaml_errors": None,
        "user": user(),
    }
    value.update(changes)
    return value


def variables():
    return [
        {"key": "FORGE_LANE_NONCE", "value": DISPATCH.nonce, "variable_type": "env_var"},
        {
            "key": "FORGE_LANE_DISPATCH",
            "value": DISPATCH.model_dump_json(),
            "variable_type": "env_var",
        },
    ]


class Wire:
    def __init__(self):
        self.calls = []
        self.metadata = {
            "id": 123,
            "path_with_namespace": "new/repository",
            "default_branch": "main",
            "archived": False,
            "ci_config_path": None,
            "http_url_to_repo": "https://ce.example/new/repository.git",
        }
        self.routes = {
            "repository/branches/main": {"name": "main", "protected": True, "commit": {"id": SHA}},
            "repository/branches/forge/run-1": {
                "name": DISPATCH.ref,
                "protected": False,
                "commit": {"id": SHA},
            },
            "repository/commits/" + SHA: {"id": SHA, "parent_ids": [OTHER]},
            "repository/tree": [{"path": "old.txt", "id": BLOB, "mode": "100755", "type": "blob"}],
            "repository/blobs/" + BLOB: {
                "sha": BLOB,
                "size": len(BODY),
                "encoding": "base64",
                "content": base64.b64encode(BODY).decode(),
            },
            "repository/commits": [
                {"id": SHA, "message": "change\n\n" + MARKER, "parent_ids": [OTHER]}
            ],
            "issues/7": {
                "id": 70,
                "iid": 7,
                "project_id": 123,
                "title": "Task",
                "description": "Exact body",
                "labels": ["forge"],
                "updated_at": "2026-10-23T10:00:00Z",
            },
            "issues/7/notes": [],
            "merge_requests": [],
            "merge_requests/8": mr(),
            "merge_requests/8/notes": [],
            "merge_requests/8/discussions": [],
            "protected_branches": [],
            "members/all/42": {
                "id": 42,
                "username": HUMAN.login,
                "state": "active",
                "access_level": 30,
                "expires_at": None,
            },
            "pipelines": [pipeline()],
            "pipelines/700": pipeline(),
            "pipelines/700/variables": variables(),
            "pipelines/700/jobs": [
                {
                    "id": 701,
                    "name": "forge-lane-v1",
                    "status": "success",
                    "pipeline": {"id": 700, "sha": SHA},
                    "commit": {"id": SHA},
                }
            ],
        }
        self.mutations = {}
        self.user = user()

    def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == "ce.example"
        if request.url.path == "/api/v4/user":
            assert request.headers["private-token"] == "synthetic-write"
            return httpx.Response(200, json=self.user)
        assert request.url.path.startswith("/api/v4/projects/123")
        if request.url.path == "/api/v4/projects/123":
            assert request.method == "GET"
            return httpx.Response(200, json=self.metadata)
        resource = request.url.path.removeprefix("/api/v4/projects/123/")
        if request.method != "GET":
            assert request.headers["private-token"] == "synthetic-write"
            result = self.mutations.get((request.method, resource))
            if result is None:
                raise AssertionError("unexpected mutation " + request.method + " " + resource)
        else:
            assert request.headers["private-token"] == "synthetic-read"
            result = self.routes.get(resource, httpx.Response(404, json={"message": "not found"}))
        if callable(result):
            return result(request)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)


def adapters(wire):
    transport = Transport(Endpoint("https://ce.example"), transport=httpx.MockTransport(wire))
    api = API(
        "ce-1",
        transport,
        {"123": Credentials(SecretStr("synthetic-read"), SecretStr("synthetic-write"), BOT)},
    )
    repo, review, subjects = GitLabRepo(api), GitLabReview(api), GitLabSubject(api)

    async def lookup(repository, nonce):
        return DISPATCH if repository == REPO and nonce == DISPATCH.nonce else None

    ci = GitLabCI(api, Clock(), dispatch_lookup=lookup)
    writes = GitLabWrites(
        api,
        repositories=repo,
        reviews=review,
        ci=ci,
        committer=NoCommitter(),
        requirements_url="https://releases.example/requirements.txt",
        requirements_sha256="1" * 64,
    )
    return api, repo, review, subjects, ci, writes
