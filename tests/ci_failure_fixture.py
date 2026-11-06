"""Concrete read-only native CI APIs with exact repository/run/job report bindings."""

from datetime import timedelta
import json

import httpx
from pydantic import SecretStr

from forge.port_values import Actor
from forge.providers.github.auth import AppAuth
from forge.providers.github.ci import GitHubCI
from forge.providers.github.client import API as GitHubAPI
from forge.providers.github.transport import Transport as GitHubTransport
from forge.providers.gitlab.ci import GitLabCI
from forge.providers.gitlab.client import API as GitLabAPI, Credentials
from forge.providers.gitlab.transport import Transport as GitLabTransport, Endpoint

JUNIT = (
    '<testsuites><testsuite name="unit" tests="1" failures="1" errors="0" skipped="0">'
    '<testcase classname="tests.test_parser" name="test_rejects_trailing">'
    '<failure message="expected rejection">assert False</failure></testcase></testsuite></testsuites>'
)


class NativeCI:
    def __init__(self, repo, sha, clock, app_key):
        self.repo, self.sha, self.clock, self.calls = repo, sha, clock, []
        self.routes = {}
        self.ended = clock.now().isoformat()
        self.job = dict(
            id=901,
            name="unit",
            status="failed",
            failure_reason="script_failure",
            pipeline={"id": 700, "sha": sha},
            commit={"id": sha},
            finished_at=self.ended,
        )
        self.summary = {
            "total": dict(count=1, success=0, failed=1, skipped=0, error=0, suite_error=None),
            "test_suites": [
                dict(
                    name="unit",
                    total_count=1,
                    success_count=0,
                    failed_count=1,
                    skipped_count=0,
                    error_count=0,
                    build_ids=[901],
                    suite_error=None,
                )
            ],
        }
        identity = repo.identity
        self.run = dict(
            id=700,
            project_id=int(identity.repository_id),
            source="push",
            sha=sha,
            ref="forge/owned",
            yaml_errors=None,
            repository={"id": int(identity.repository_id)},
            head_repository={"id": int(identity.repository_id)},
            head_sha=sha,
            path=".github/workflows/ci.yml",
            run_attempt=1,
            check_suite_id=800,
        )
        self.check = dict(
            id=901,
            name="unit",
            head_sha=sha,
            status="completed",
            conclusion="failure",
            check_suite={"id": 800},
            app={"id": 15368, "slug": "github-actions"},
            completed_at=self.ended,
            output={"text": JUNIT},
        )
        if identity.provider == "github":
            self.job.update(
                run_id=700,
                run_attempt=1,
                head_sha=sha,
                status="completed",
                conclusion="failure",
                completed_at=self.ended,
                check_run_url="https://api.github.com/repos/" + repo.full_name + "/check-runs/901",
            )
            self.http = GitHubTransport(transport=httpx.MockTransport(self.reply))
            api = GitHubAPI(
                AppAuth(
                    identity.connection_id,
                    "owned",
                    app_key,
                    {identity.repository_id: 900},
                    clock,
                    self.http,
                ),
                self.http,
            )
            self.ci = GitHubCI(
                api,
                clock,
                dispatch_lookup=self.lookup,
                lane_workflow_path=".github/workflows/lane.yml",
                verification_workflow_paths=(".github/workflows/ci.yml",),
                verification_failure_jobs=("unit",),
            )
        else:
            self.http = GitLabTransport(
                Endpoint("https://ce.example"), transport=httpx.MockTransport(self.reply)
            )
            bot = Actor(
                provider="gitlab",
                connection_id=identity.connection_id,
                native_id="99",
                login="project_" + identity.repository_id + "_bot_forge",
                is_bot=True,
            )
            api = GitLabAPI(
                identity.connection_id,
                self.http,
                {
                    identity.repository_id: Credentials(
                        SecretStr("synthetic-read"), SecretStr("synthetic-write"), bot
                    )
                },
            )
            self.ci = GitLabCI(
                api, clock, dispatch_lookup=self.lookup, verification_failure_jobs=("unit",)
            )

    async def lookup(self, repo, nonce):
        return None

    async def close(self):
        await self.http.close()

    def reply(self, request):
        self.calls.append(request)
        native_id = int(self.repo.identity.repository_id)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-owned-token",
                    "expires_at": (self.clock.now() + timedelta(hours=1)).isoformat(),
                    "repository_selection": "selected",
                    "repositories": [{"id": native_id}],
                    "permissions": json.loads(request.content)["permissions"],
                },
            )
        assert request.method == "GET"
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [{"id": native_id, "full_name": self.repo.full_name}],
                },
            )
        if self.repo.identity.provider == "github":
            prefix = "/repos/" + self.repo.full_name + "/"
            assert request.url.path.startswith(prefix)
            resource = request.url.path.removeprefix(prefix)
            values = {
                "actions/runs": {"total_count": 1, "workflow_runs": [self.run]},
                "actions/runs/700/attempts/1/jobs": {"total_count": 1, "jobs": [self.job]},
                "check-runs/901": self.check,
            }
        else:
            prefix = "/api/v4/projects/" + str(native_id)
            if request.url.path == prefix:
                return httpx.Response(
                    200,
                    json={
                        "id": native_id,
                        "path_with_namespace": self.repo.full_name,
                        "ci_config_path": None,
                    },
                )
            assert request.url.path.startswith(prefix + "/")
            resource = request.url.path.removeprefix(prefix + "/")
            values = {
                "pipelines": [self.run],
                "pipelines/700": self.run,
                "pipelines/700/variables": [],
                "pipelines/700/jobs": [self.job],
                "pipelines/700/test_report_summary": self.summary,
            }
        value = self.routes.get(resource, values.get(resource, httpx.Response(404)))
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
