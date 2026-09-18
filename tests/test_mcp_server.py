import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.lowlevel.server import RequestContext, request_ctx
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from forge.config import Settings
from forge.gitlab.client import GitLabClient
from forge.mcp_server.auth import (
    MASTER_SCOPES,
    MCP_SCOPES,
    McpPrincipal,
    apply_repo_allowlist,
    parse_token_repos,
    repo_target_allowed,
    stash_principal,
)
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


def master_principal() -> McpPrincipal:
    return McpPrincipal(name="master", scopes=MASTER_SCOPES)


@contextmanager
def principal_context(principal: McpPrincipal):
    """Bind a stub request carrying *principal* into the SDK request ctx.

    The guard resolves the caller through the same contextvar the real
    streamable-HTTP mount populates; direct `.fn` calls need it faked.
    """
    scope: dict = {}
    stash_principal(scope, principal)
    context = RequestContext(
        request_id=1,
        meta=None,
        session=MagicMock(),
        lifespan_context=None,
        request=SimpleNamespace(scope=scope),
    )
    token = request_ctx.set(context)
    try:
        yield
    finally:
        request_ctx.reset(token)


async def call_tool_fn(
    mcp,
    name: str,  # type: ignore[no-untyped-def]
    principal: McpPrincipal | None,
    **kwargs: object,
) -> str:
    """Invoke a registered tool directly, as the given (or no) principal."""
    tool_fn = mcp._tool_manager._tools[name].fn
    if principal is None:
        return await tool_fn(**kwargs)  # no request context -> default deny
    with principal_context(principal):
        return await tool_fn(**kwargs)


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

        tool_fn_result = await call_tool_fn(
            mcp,
            "list_merge_requests",
            master_principal(),
            project="42",
            state="opened",
            max_results=10,
        )
        assert "!1" in tool_fn_result
        assert "Test MR" in tool_fn_result

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

        result = await call_tool_fn(mcp, "get_issue", master_principal(), project="42", issue_iid=5)
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

        result = await call_tool_fn(
            mcp,
            "create_issue",
            master_principal(),
            project="42",
            title="New issue",
            description="Details",
        )
        assert "#10" in result
        assert "New issue" in result

    async def test_tool_error_handling(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/nonexistent%2Fproject",
            status_code=404,
            text="Project not found",
        )

        result = await call_tool_fn(
            mcp,
            "list_merge_requests",
            master_principal(),
            project="nonexistent/project",
            state="opened",
            max_results=10,
        )
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

        result = await call_tool_fn(
            mcp,
            "get_pipeline_status",
            master_principal(),
            project="42",
            ref="main",
        )
        assert "Pipeline #500" in result
        assert "success" in result


class TestGuardedTools:
    """R19: the classic family enforces scopes per call (default deny)."""

    @pytest.fixture()
    def mcp(self, mcp_settings_no_auth: Settings):
        return create_mcp_server(mcp_settings_no_auth)

    async def test_no_principal_denies_write(self, mcp):
        result = await call_tool_fn(mcp, "post_merge_request_comment", None)
        assert "FORBIDDEN" in result

    async def test_no_principal_denies_read(self, mcp):
        result = await call_tool_fn(mcp, "get_issue", None)
        assert "FORBIDDEN" in result

    async def test_read_scope_cannot_write(self, mcp):
        reader = McpPrincipal(name="tok-reader", scopes=frozenset({"forge:read"}))
        result = await call_tool_fn(
            mcp,
            "post_merge_request_comment",
            reader,
            project="42",
            mr_iid=1,
            body="hi",
        )
        assert "FORBIDDEN" in result
        assert "forge:runs:write" in result

    async def test_denial_audits_warning(self, mcp, caplog):
        reader = McpPrincipal(name="tok-reader", scopes=frozenset({"forge:read"}))
        with caplog.at_level(logging.WARNING, logger="forge.mcp_server.audit"):
            await call_tool_fn(mcp, "create_issue", reader, project="42", title="t")
        assert any(
            "principal=tok-reader" in record.message
            and "tool=create_issue" in record.message
            and "outcome=denied" in record.message
            and "required_scope=forge:runs:write" in record.message
            for record in caplog.records
        )

    async def test_allowed_call_audits_actor(self, mcp, httpx_mock: HTTPXMock, caplog):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/issues",
            method="POST",
            json={"id": 1, "iid": 2, "title": "t", "state": "opened"},
        )
        writer = McpPrincipal(name="tok-writer", scopes=frozenset({"forge:runs:write"}))
        with caplog.at_level(logging.INFO, logger="forge.mcp_server.audit"):
            result = await call_tool_fn(mcp, "create_issue", writer, project="42", title="t")
        assert "Issue created" in result
        assert any(
            "principal=tok-writer" in record.message
            and "tool=create_issue" in record.message
            and "outcome=ok" in record.message
            and "target=42" in record.message
            for record in caplog.records
        )

    async def test_repo_allowlist_denies_off_target(self, mcp):
        scoped = McpPrincipal(
            name="tok-narrow",
            scopes=frozenset({"forge:read"}),
            repo_patterns=("allowed/*",),
        )
        result = await call_tool_fn(mcp, "get_issue", scoped, project="other/repo", issue_iid=1)
        assert "FORBIDDEN" in result
        assert "tok-narrow" in result

    async def test_repo_allowlist_admits_matching_target(self, mcp, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/allowed%2Frepo",
            json={"id": 42, "name": "repo", "path_with_namespace": "allowed/repo"},
        )
        httpx_mock.add_response(
            url=f"{GL_BASE}/projects/42/issues/5",
            json={"id": 1, "iid": 5, "title": "Bug", "state": "opened"},
        )
        scoped = McpPrincipal(
            name="tok-narrow",
            scopes=frozenset({"forge:read"}),
            repo_patterns=("allowed/*",),
        )
        result = await call_tool_fn(mcp, "get_issue", scoped, project="allowed/repo", issue_iid=5)
        assert "#5" in result


class TestRepoTargetAuthz:
    """R19: FORGE_MCP_TOKEN_REPOS parsing + matching semantics."""

    def test_blank_config_parses_empty(self):
        assert parse_token_repos(None) == {}
        assert parse_token_repos("") == {}

    def test_valid_config(self):
        repos = parse_token_repos('{"tok-a": ["group/*"], "tok-b": []}')
        assert repos == {"tok-a": ("group/*",), "tok-b": ()}

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("not-json", "not valid JSON"),
            ('["tok"]', "must be a JSON object"),
            ('{"": ["x"]}', "non-empty strings"),
            ('{"tok": "group/*"}', "must be a list"),
            ('{"tok": [""]}', "non-empty strings"),
        ],
    )
    def test_malformed_config_fails_closed(self, raw: str, message: str):
        with pytest.raises(ValueError, match=message):
            parse_token_repos(raw)

    def test_unrestricted_principal_targets_anything(self):
        principal = McpPrincipal(name="t", scopes=frozenset(MCP_SCOPES))
        assert repo_target_allowed(principal, "any/repo")
        assert repo_target_allowed(principal, "42")

    def test_glob_match_and_miss(self):
        principal = McpPrincipal(
            name="t",
            scopes=frozenset({"forge:read"}),
            repo_patterns=("group/app-*", "sandbox/exact"),
        )
        assert repo_target_allowed(principal, "group/app-1")
        assert repo_target_allowed(principal, "sandbox/exact")
        assert not repo_target_allowed(principal, "group/other")
        assert not repo_target_allowed(principal, "sandbox/exact/sub")

    def test_numeric_id_needs_explicit_pattern(self):
        principal = McpPrincipal(
            name="t",
            scopes=frozenset({"forge:read"}),
            repo_patterns=("group/*",),
        )
        assert not repo_target_allowed(principal, "42")
        explicit = McpPrincipal(
            name="t",
            scopes=frozenset({"forge:read"}),
            repo_patterns=("42",),
        )
        assert repo_target_allowed(explicit, "42")

    def test_apply_repo_allowlist_by_token_and_label(self):
        from forge.mcp_server.auth import _token_label

        principals = {
            "tok-a": McpPrincipal(name=_token_label("tok-a"), scopes=frozenset({"forge:read"})),
            "tok-b": McpPrincipal(name=_token_label("tok-b"), scopes=frozenset({"forge:read"})),
        }
        resolved = apply_repo_allowlist(
            principals,
            {"tok-a": ("x/*",), _token_label("tok-b"): ("y/*",)},
        )
        assert resolved["tok-a"].repo_patterns == ("x/*",)
        assert resolved["tok-b"].repo_patterns == ("y/*",)
