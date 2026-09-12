import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock
from unittest.mock import AsyncMock

from forge.config import Settings
from forge.gitlab.client import GitLabClient
from forge.mcp_server.server import (
    MCPAuthMiddleware,
    create_mcp_server,
    resolve_project_id,
)

GL_BASE = "https://gitlab.test/api/v4"


@pytest.fixture()
def mcp_settings() -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret"),
        FORGE_MCP_KEY=SecretStr("test-mcp-key"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
    )


@pytest.fixture()
def mcp_settings_no_auth() -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
    )


@pytest.fixture()
def gitlab_client():
    return GitLabClient(
        base_url="https://gitlab.test",
        token="test-token",
        timeout=5.0,
    )


class TestResolveProjectId:
    async def test_numeric_string_passthrough(self, gitlab_client: GitLabClient):
        pid = await resolve_project_id(gitlab_client, "42", redis=None)
        assert pid == 42

    async def test_path_resolution(self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/mygroup%2Fmyproject",
            json={
                "id": 42,
                "name": "myproject",
                "path_with_namespace": "mygroup/myproject",
            },
        )

        async with gitlab_client as gl:
            pid = await resolve_project_id(gl, "mygroup/myproject", redis=None)

        assert pid == 42

    async def test_redis_cache_hit(self, gitlab_client: GitLabClient):
        redis = AsyncMock()
        redis.get.return_value = "42"

        pid = await resolve_project_id(gitlab_client, "mygroup/myproject", redis=redis)

        assert pid == 42
        redis.get.assert_awaited_once_with("mcp:project:mygroup/myproject")

    async def test_redis_cache_miss_then_set(
        self, httpx_mock: HTTPXMock, gitlab_client: GitLabClient
    ):
        redis = AsyncMock()
        redis.get.return_value = None

        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/mygroup%2Fmyproject",
            json={
                "id": 42,
                "name": "myproject",
                "path_with_namespace": "mygroup/myproject",
            },
        )

        async with gitlab_client as gl:
            pid = await resolve_project_id(gl, "mygroup/myproject", redis=redis)

        assert pid == 42
        redis.set_ex.assert_awaited_once_with("mcp:project:mygroup/myproject", "42", ex=3600)


class TestMCPAuthMiddleware:
    @pytest.fixture()
    def dummy_app(self):
        """A minimal ASGI app that returns 200."""

        async def app(scope, receive, send):
            from starlette.responses import JSONResponse

            response = JSONResponse({"ok": True})
            await response(scope, receive, send)

        return app

    async def test_valid_token(self, dummy_app):
        from httpx import ASGITransport, AsyncClient

        wrapped = MCPAuthMiddleware(dummy_app, "secret-key")
        async with AsyncClient(
            transport=ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get("/", headers={"Authorization": "Bearer secret-key"})

        assert resp.status_code == 200

    async def test_missing_token(self, dummy_app):
        from httpx import ASGITransport, AsyncClient

        wrapped = MCPAuthMiddleware(dummy_app, "secret-key")
        async with AsyncClient(
            transport=ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get("/")

        assert resp.status_code == 401

    async def test_wrong_token(self, dummy_app):
        from httpx import ASGITransport, AsyncClient

        wrapped = MCPAuthMiddleware(dummy_app, "secret-key")
        async with AsyncClient(
            transport=ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get("/", headers={"Authorization": "Bearer wrong-key"})

        assert resp.status_code == 401


class TestMCPServerCreation:
    def test_create_mcp_server(self, mcp_settings: Settings):
        mcp = create_mcp_server(mcp_settings)
        assert mcp is not None
        assert mcp._forge_settings is mcp_settings  # type: ignore[attr-defined]
        assert mcp._forge_redis is None  # type: ignore[attr-defined]

    def test_create_mcp_server_with_redis(self, mcp_settings: Settings):
        redis = AsyncMock()
        mcp = create_mcp_server(mcp_settings, redis_manager=redis)
        assert mcp._forge_redis is redis  # type: ignore[attr-defined]

    def test_tools_registered(self, mcp_settings: Settings):
        mcp = create_mcp_server(mcp_settings)
        # Check that key tools are registered by listing tool names
        tool_names = list(mcp._tool_manager._tools.keys())
        expected = [
            "list_merge_requests",
            "get_merge_request",
            "get_merge_request_diff",
            "post_merge_request_comment",
            "list_issues",
            "get_issue",
            "create_issue",
            "get_file_content",
            "get_repository_tree",
            "search_code",
            "get_pipeline_status",
            "get_failed_pipeline_logs",
            "list_project_labels",
        ]
        for name in expected:
            assert name in tool_names, f"Tool '{name}' not registered"


class TestMCPTools:
    @pytest.fixture()
    def mcp(self, mcp_settings_no_auth: Settings):
        return create_mcp_server(mcp_settings_no_auth)

    async def test_list_merge_requests_tool(self, mcp, httpx_mock: HTTPXMock):
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
                    "web_url": "https://gitlab.test/g/proj/-/merge_requests/1",
                },
            ],
        )

        tool_fn = mcp._tool_manager._tools["list_merge_requests"].fn
        result = await tool_fn(project="42", state="opened", max_results=10)
        assert "!1" in result
        assert "Test MR" in result

    async def test_get_issue_tool(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/issues/5",
            json={
                "id": 200,
                "iid": 5,
                "title": "Bug report",
                "state": "opened",
                "labels": ["bug"],
                "description": "Something is broken",
            },
        )

        tool_fn = mcp._tool_manager._tools["get_issue"].fn
        result = await tool_fn(project="42", issue_iid=5)
        assert "#5" in result
        assert "Bug report" in result
        assert "Something is broken" in result

    async def test_create_issue_tool(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/issues",
            method="POST",
            json={
                "id": 300,
                "iid": 10,
                "title": "New issue",
                "state": "opened",
                "web_url": "https://gitlab.test/g/proj/-/issues/10",
            },
        )

        tool_fn = mcp._tool_manager._tools["create_issue"].fn
        result = await tool_fn(project="42", title="New issue", description="Details")
        assert "#10" in result
        assert "New issue" in result

    async def test_tool_error_handling(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/nonexistent%2Fproject",
            status_code=404,
            text="Project not found",
        )

        tool_fn = mcp._tool_manager._tools["list_merge_requests"].fn
        result = await tool_fn(project="nonexistent/project", state="opened", max_results=10)
        assert "Error:" in result

    async def test_get_pipeline_status_tool(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/pipelines?per_page=1&order_by=id&sort=desc&ref=main",
            json=[
                {
                    "id": 500,
                    "status": "success",
                    "ref": "main",
                    "web_url": "https://gitlab.test/g/proj/-/pipelines/500",
                },
            ],
        )

        tool_fn = mcp._tool_manager._tools["get_pipeline_status"].fn
        result = await tool_fn(project="42", ref="main")
        assert "Pipeline #500" in result
        assert "success" in result
