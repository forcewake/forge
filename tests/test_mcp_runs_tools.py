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

SCOPED_JSON = json.dumps({READ_TOKEN: ["forge:read", "forge:runs:write"]})


def mcp_settings(tmp_path, scoped: str | None = SCOPED_JSON) -> Settings:
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
        response = await call_tool(
            client, "run_list", {"status": "planning"}, MASTER_KEY
        )
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
        response = await call_tool(
            client, "run_evidence_get", {"run_id": "run-1"}, MASTER_KEY
        )
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
