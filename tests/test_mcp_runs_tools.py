"""v0.8 MCP run-surface tests: scoped authz + durable read tools (ADR-0021 §4).

The run surface reads durable state ONLY — no provider token, no platform
passthrough. Authorization is per-call from the principal the ASGI
middleware stashed; the tests exercise the full matrix (master key,
scoped tokens, denials, audit lines) over the sqlite harness.
"""

import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun, RunSpec
from forge.main import create_app

MASTER_KEY = "forge-master-key"
READ_TOKEN = "forge-read-token"
NARROW_TOKEN = "forge-narrow-token"
READONLY_TOKEN = "forge-readonly-token"  # classic surface: read, never write
WRITE_TOKEN = "forge-write-token"  # classic surface: may write GitLab

SCOPED_JSON = json.dumps(
    {
        READ_TOKEN: ["forge:read", "forge:runs:write"],
        READONLY_TOKEN: ["forge:read"],
        WRITE_TOKEN: ["forge:runs:write"],
    }
)

REPOS_JSON = json.dumps({READONLY_TOKEN: ["allowed/*"]})

GL_BASE = "https://gitlab.test/api/v4"


def mcp_settings(
    tmp_path,
    scoped: str | None = SCOPED_JSON,
    repos: str | None = None,
) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/mcp-runs.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_MCP_ENABLED=True,
        FORGE_MCP_KEY=SecretStr(MASTER_KEY),
        FORGE_MCP_SCOPED_TOKENS=SecretStr(scoped) if scoped else None,
        FORGE_MCP_TOKEN_REPOS=SecretStr(repos) if repos else None,
        FORGE_MCP_ALLOWED_HOSTS="testserver",
    )


def seeded_run(run_id: str = "run-1", status: str = "ready_for_human") -> FlowRun:
    return FlowRun(
        id=run_id,
        project_id=42,
        issue_iid=7,
        provider="gitlab",
        status=status,
        evidence={"plan_summary": "add tests"},
    )


async def seed_db(application, runs: list[FlowRun], with_spec: bool = True) -> None:
    async with application.state.session_factory() as session:
        async with session.begin():
            for run in runs:
                session.add(run)
            if with_spec:
                session.add(
                    RunSpec(
                        run_id="run-1",
                        document={"steps": ["write tests"]},
                        digest="d" * 64,
                    )
                )
    return None


async def call_tool(client: AsyncClient, name: str, arguments: dict, token: str) -> dict:
    """One tools/call over the streamable-HTTP mount."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = await client.post(
        "/mcp/mcp",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    return response


def result_text(response) -> str:
    """The tool result text from a JSON or SSE-encoded streamable response."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())["result"]["content"][0]["text"]
        raise AssertionError(f"no data line in SSE body: {response.text[:200]}")
    return response.json()["result"]["content"][0]["text"]


@pytest.fixture()
async def app(tmp_path):
    reset_engine()
    application = create_app(settings=mcp_settings(tmp_path))
    async with application.router.lifespan_context(application):
        application.state.task_queue = AsyncMock()
        yield application
    reset_engine()


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture()
async def repos_app(tmp_path):
    """Same app with a repo-target allowlist on READONLY_TOKEN (R19)."""
    reset_engine()
    application = create_app(settings=mcp_settings(tmp_path, repos=REPOS_JSON))
    async with application.router.lifespan_context(application):
        application.state.task_queue = AsyncMock()
        yield application
    reset_engine()


@pytest.fixture()
async def repos_client(repos_app) -> AsyncClient:
    transport = ASGITransport(app=repos_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


class TestScopedAuthz:
    async def test_unknown_token_is_401(self, client: AsyncClient):
        response = await call_tool(client, "run_list", {}, "wrong-token")
        assert response.status_code == 401

    async def test_missing_token_is_401(self, client: AsyncClient):
        response = await client.post(
            "/mcp/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 401

    async def test_master_key_grants_everything(self, app, client: AsyncClient):
        await seed_db(app, [seeded_run()])
        response = await call_tool(client, "run_get", {"run_id": "run-1"}, MASTER_KEY)
        assert response.status_code == 200
        assert "ready_for_human" in result_text(response)

    async def test_scoped_read_token_reads(self, app, client: AsyncClient):
        await seed_db(app, [seeded_run()])
        response = await call_tool(client, "run_get", {"run_id": "run-1"}, READ_TOKEN)
        assert response.status_code == 200
        assert "ready_for_human" in result_text(response)

    async def test_unknown_scope_rejects_whole_config_at_startup(self, tmp_path):
        from forge.mcp_server.auth import parse_scoped_tokens

        with pytest.raises(ValueError, match="unknown scopes"):
            parse_scoped_tokens(json.dumps({"tok": ["forge:read", "forge:admin:root"]}))

    async def test_blank_config_parses_empty(self):
        from forge.mcp_server.auth import parse_scoped_tokens

        assert parse_scoped_tokens(None) == {}
        assert parse_scoped_tokens("") == {}


class TestRunSurface:
    async def test_run_list_filters_by_status(self, app, client: AsyncClient):
        await seed_db(
            app,
            [seeded_run("run-1"), seeded_run("run-2", status="planning")],
            with_spec=False,
        )
        response = await call_tool(client, "run_list", {"status": "planning"}, MASTER_KEY)
        text = result_text(response)
        assert "run-2" in text
        assert "run-1" not in text

    async def test_run_get_unknown_run(self, app, client: AsyncClient):
        await seed_db(app, [], with_spec=False)
        response = await call_tool(client, "run_get", {"run_id": "nope"}, MASTER_KEY)
        assert "NOT FOUND" in result_text(response)

    async def test_plan_get_returns_frozen_document(self, app, client: AsyncClient):
        await seed_db(app, [seeded_run()])
        response = await call_tool(client, "plan_get", {"run_id": "run-1"}, MASTER_KEY)
        text = result_text(response)
        assert "write tests" in text
        assert "d" * 64 in text

    async def test_evidence_get_includes_steps(self, app, client: AsyncClient):
        await seed_db(app, [seeded_run()])
        response = await call_tool(client, "run_evidence_get", {"run_id": "run-1"}, MASTER_KEY)
        text = result_text(response)
        assert "plan_summary" in text


class TestAudit:
    async def test_authorized_calls_leave_audit_lines(self, app, client: AsyncClient, caplog):
        import logging

        await seed_db(app, [seeded_run()])
        with caplog.at_level(logging.INFO, logger="forge.mcp_server.audit"):
            await call_tool(client, "run_get", {"run_id": "run-1"}, READ_TOKEN)
        assert any(
            "tool=run_get" in record.message and "principal=tok-" in record.message
            for record in caplog.records
        )


class TestClassicSurface:
    """R19: the classic GitLab family enforces scopes + repo targets.

    Before this finding, a read-only principal could drive the shared
    platform token to write (comments, issue creation) — the tools now go
    through the same default-deny guard as the run surface.
    """

    async def test_readonly_token_classic_read_ok(self, app, client: AsyncClient, httpx_mock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/merge_requests?state=opened&per_page=10",
            json=[
                {
                    "id": 100,
                    "iid": 1,
                    "title": "Test MR",
                    "state": "opened",
                    "source_branch": "feat",
                    "target_branch": "main",
                },
            ],
        )
        response = await call_tool(client, "list_merge_requests", {"project": "42"}, READONLY_TOKEN)
        assert response.status_code == 200
        assert "Test MR" in result_text(response)

    async def test_readonly_token_classic_write_forbidden(self, app, client: AsyncClient):
        # No GitLab response registered: reaching the API would fail the test.
        response = await call_tool(
            client,
            "post_merge_request_comment",
            {"project": "42", "mr_iid": 1, "body": "hi"},
            READONLY_TOKEN,
        )
        assert response.status_code == 200
        assert "FORBIDDEN" in result_text(response)
        assert "forge:runs:write" in result_text(response)

    async def test_write_scope_token_can_comment(self, app, client: AsyncClient, httpx_mock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/merge_requests/1/notes",
            method="POST",
            json={"id": 9, "body": "hi"},
        )
        response = await call_tool(
            client,
            "post_merge_request_comment",
            {"project": "42", "mr_iid": 1, "body": "hi"},
            WRITE_TOKEN,
        )
        assert response.status_code == 200
        assert "Comment posted" in result_text(response)

    async def test_write_scope_token_cannot_read(self, app, client: AsyncClient):
        response = await call_tool(
            client,
            "list_merge_requests",
            {"project": "42"},
            WRITE_TOKEN,
        )
        assert "FORBIDDEN" in result_text(response)

    async def test_denied_classic_write_is_audited(self, app, client: AsyncClient, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="forge.mcp_server.audit"):
            await call_tool(
                client,
                "create_issue",
                {"project": "42", "title": "sneaky"},
                READONLY_TOKEN,
            )
        assert any(
            "outcome=denied" in record.message
            and "tool=create_issue" in record.message
            and "principal=tok-" in record.message
            and "required_scope=forge:runs:write" in record.message
            for record in caplog.records
        )

    async def test_allowed_classic_write_audits_actor(
        self, app, client: AsyncClient, httpx_mock, caplog
    ):
        import logging

        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/issues",
            method="POST",
            json={"id": 300, "iid": 10, "title": "New issue", "state": "opened"},
        )
        with caplog.at_level(logging.INFO, logger="forge.mcp_server.audit"):
            response = await call_tool(
                client,
                "create_issue",
                {"project": "42", "title": "New issue"},
                WRITE_TOKEN,
            )
        assert "Issue created" in result_text(response)
        assert any(
            "outcome=ok" in record.message
            and "tool=create_issue" in record.message
            and "principal=tok-" in record.message
            for record in caplog.records
        )

    async def test_readonly_token_still_reads_runs(self, app, client: AsyncClient):
        # No regression: the run surface keeps its forge:read contract.
        await seed_db(app, [seeded_run()])
        response = await call_tool(client, "run_get", {"run_id": "run-1"}, READONLY_TOKEN)
        assert response.status_code == 200
        assert "ready_for_human" in result_text(response)


class TestRepoTargetAuthz:
    """R19: FORGE_MCP_TOKEN_REPOS constrains the caller-supplied project."""

    async def test_wrong_repo_target_forbidden(self, repos_app, repos_client: AsyncClient):
        response = await call_tool(
            repos_client,
            "list_merge_requests",
            {"project": "other/repo"},
            READONLY_TOKEN,
        )
        assert response.status_code == 200
        assert "FORBIDDEN" in result_text(response)

    async def test_numeric_id_escapes_no_allowlist(self, repos_app, repos_client: AsyncClient):
        # Allowlisted by path -> a numeric ID does not match any pattern.
        response = await call_tool(
            repos_client,
            "list_merge_requests",
            {"project": "42"},
            READONLY_TOKEN,
        )
        assert "FORBIDDEN" in result_text(response)

    async def test_matching_repo_target_allowed(
        self, repos_app, repos_client: AsyncClient, httpx_mock
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/allowed%2Frepo",
            json={"id": 42, "name": "repo", "path_with_namespace": "allowed/repo"},
        )
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/merge_requests?state=opened&per_page=10",
            json=[],
        )
        response = await call_tool(
            repos_client,
            "list_merge_requests",
            {"project": "allowed/repo"},
            READONLY_TOKEN,
        )
        assert response.status_code == 200
        assert "No merge requests found." in result_text(response)

    async def test_master_key_bypasses_repo_allowlist(
        self, repos_app, repos_client: AsyncClient, httpx_mock
    ):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/anything%2Felse",
            json={"id": 7, "name": "else", "path_with_namespace": "anything/else"},
        )
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/7/issues/1",
            json={"id": 1, "iid": 1, "title": "T", "state": "opened"},
        )
        response = await call_tool(
            repos_client,
            "get_issue",
            {"project": "anything/else", "issue_iid": 1},
            MASTER_KEY,
        )
        assert response.status_code == 200
        assert "#1" in result_text(response)

    async def test_unrestricted_token_keeps_legacy_behavior(
        self, repos_app, repos_client: AsyncClient, httpx_mock
    ):
        # Tokens absent from FORGE_MCP_TOKEN_REPOS stay unrestricted.
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/merge_requests?state=opened&per_page=10",
            json=[],
        )
        response = await call_tool(
            repos_client,
            "list_merge_requests",
            {"project": "42"},
            READ_TOKEN,
        )
        assert response.status_code == 200
        assert "No merge requests found." in result_text(response)


class TestDeliveryLadder:
    async def test_ladder_buckets_runs_by_status(self, app, client: AsyncClient):
        from forge.gateway.router import _delivery_ladder

        ladder = _delivery_ladder(
            {
                "accepted": 2,
                "waiting_approval": 1,
                "waiting_ci": 3,
                "reviewing": 1,
                "ready_for_human": 2,
                "failed": 1,
            }
        )
        assert ladder == {
            "started": 3,  # accepted + failed
            "planned": 1,
            "gate_approved": 0,
            "candidate_published": 3,
            "ci_passed": 1,
            "ready_for_human": 2,
        }

    async def test_prometheus_exposes_the_ladder(self, app, client: AsyncClient):
        await seed_db(app, [seeded_run("run-1")], with_spec=False)
        response = await client.get("/metrics.prometheus")
        assert response.status_code == 200
        assert 'forge_delivery_ladder{stage="ready_for_human"} 1' in response.text
