"""Native MCP config and permission boundaries without model or vendor calls."""

import json
from pathlib import Path

import httpx
import pytest

from forge.domain.approved_input import ApprovedInput
from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.drivers.copilot_cli import CopilotCLIDriver
from forge_lane.errors import LaneFailure
from forge_lane.mcp import for_claude, for_copilot, verify_public_servers
from forge_protocol import ExecutionPolicy, McpServerPolicy
from tests.test_domain_approved_input import IDENTITY, spec
from tests.test_lane_claude_code import FakeClient, FakeSDK, grant
from tests.test_lane_frozen_policy import frozen_inputs
from tests.test_lane_runner_v1 import WireServer, checkout, execute

POLICY = ExecutionPolicy(
    mcp_servers=(
        McpServerPolicy(
            name="docs",
            transport="http",
            url="https://docs.invalid/mcp",
            tools=("lookup", "search"),
        ),
    )
)


class MetadataServer:
    def __init__(self, fault=None):
        self.requests = []
        self.fault = fault

    def handle(self, request):
        self.requests.append(request)
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == "owned-metadata-session"
            return httpx.Response(204)
        body = json.loads(request.content)
        method = body["method"]
        if self.fault == "redirect":
            return httpx.Response(307, headers={"location": "https://unapproved.invalid/mcp"})
        if self.fault == "auth":
            return httpx.Response(401)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "docs", "version": "1"},
            }
            if self.fault == "version":
                result["protocolVersion"] = "9999-01-01"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": result},
                headers={"mcp-session-id": "owned-metadata-session"},
            )
        assert request.headers["mcp-session-id"] == "owned-metadata-session"
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/list"
        result = {"tools": [{"name": "lookup"}, {"name": "search"}]}
        if self.fault == "missing":
            result["tools"] = [{"name": "different"}]
        elif self.fault == "pagination":
            result["nextCursor"] = "unread-tools"
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.fault == "oversized":
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        if self.fault == "sse":
            return httpx.Response(
                200,
                content="data: " + json.dumps(reply) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=reply)


def test_native_config_goldens_contain_only_frozen_grants():
    assert for_claude(POLICY) == {"docs": {"type": "http", "url": "https://docs.invalid/mcp"}}
    assert for_copilot(POLICY) == {
        "mcpServers": {
            "docs": {
                "type": "http",
                "url": "https://docs.invalid/mcp",
                "tools": ["lookup", "search"],
            }
        }
    }
    assert for_claude(ExecutionPolicy()) == {} and for_copilot(ExecutionPolicy()) == {
        "mcpServers": {}
    }


@pytest.mark.parametrize("mode", [None, "sse"])
async def test_metadata_preflight_is_credential_free_bounded_and_closes_only_its_session(
    mode, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "model-secret-must-not-be-an-mcp-credential")
    monkeypatch.setenv("FORGE_LANE_SESSION_TOKEN", "session-secret-must-not-be-an-mcp-credential")
    metadata = MetadataServer(mode)
    await verify_public_servers(POLICY, transport=httpx.MockTransport(metadata.handle))
    assert [request.method for request in metadata.requests] == ["POST", "POST", "POST", "DELETE"]
    for request in metadata.requests:
        assert request.url == "https://docs.invalid/mcp"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert b"secret" not in request.content
    assert [json.loads(request.content)["method"] for request in metadata.requests[:-1]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert json.loads(metadata.requests[0].content)["params"]["capabilities"] == {}


@pytest.mark.parametrize(
    "fault", ["missing", "redirect", "auth", "version", "pagination", "oversized"]
)
async def test_metadata_failure_never_calls_tools_or_follows_redirects(fault):
    metadata = MetadataServer(fault)
    with pytest.raises(LaneFailure, match="Approved MCP"):
        await verify_public_servers(POLICY, transport=httpx.MockTransport(metadata.handle))
    assert all(request.url.host == "docs.invalid" for request in metadata.requests)
    assert not any(
        json.loads(request.content).get("method") == "tools/call"
        for request in metadata.requests
        if request.content
    )
    if fault in ("version", "pagination", "missing", "oversized"):
        assert metadata.requests[-1].method == "DELETE"


@pytest.mark.parametrize(
    "status", ["connected", "pending", "extra", "missing_tool", "changed_url", "forbidden_tool"]
)
async def test_claude_native_status_and_exact_tool_hooks_gate_the_first_query(
    tmp_path, monkeypatch, status
):
    workspace = tmp_path / "work"
    frozen = spec(base_sha=checkout(workspace), execution_policy=POLICY)
    brief = ApprovedInput.from_spec(frozen).to_brief(IDENTITY, frozen)
    sdk = FakeSDK(tmp_path)
    metadata = MetadataServer()
    native = {
        "mcpServers": [
            {
                "name": "docs",
                "status": "connected",
                "tools": [{"name": "lookup"}, {"name": "search"}],
                "config": {"type": "http", "url": "https://docs.invalid/mcp"},
            }
        ]
    }
    if status == "pending":
        native["mcpServers"][0]["status"] = "pending"
    elif status == "extra":
        native["mcpServers"].append({"name": "ambient", "status": "connected", "tools": []})
    elif status == "missing_tool":
        native["mcpServers"][0]["tools"] = [{"name": "unapproved"}]
    elif status == "changed_url":
        native["mcpServers"][0]["config"]["url"] = "https://different.invalid/mcp"

    async def native_status(self):
        assert not self.prompts
        return native

    monkeypatch.setattr(FakeClient, "get_mcp_status", native_status, raising=False)
    monkeypatch.setattr(FakeSDK.ClaudeSDKClient, "get_mcp_status", native_status, raising=False)
    original_query = FakeClient.query
    permissions = []

    async def query(self, prompt):
        name = "mcp__docs__lookup" if status == "connected" else "mcp__docs__lookupalike"
        permissions.append(await self.options.can_use_tool(name, {}, None))
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        permissions.append(await hook({"tool_name": name, "tool_input": {}}, None, None))
        await original_query(self, prompt)

    monkeypatch.setattr(FakeClient, "query", query)
    monkeypatch.setenv("FORGE_HARNESS_MCP", '{"unapproved":{"command":"must-not-launch"}}')
    driver = ClaudeCodeDriver(sdk, mcp_transport=httpx.MockTransport(metadata.handle))
    if status in ("pending", "extra", "missing_tool", "changed_url"):
        with pytest.raises(LaneFailure, match="Approved MCP"):
            await driver.start(brief, workspace, DriverEnv("test-model", grant()))
        assert sdk.clients[0].prompts == []
    else:
        session = await driver.start(brief, workspace, DriverEnv("test-model", grant()))
        if status == "forbidden_tool":
            with pytest.raises(LaneFailure) as refused:
                _ = [event async for event in session.events()]
            assert refused.value.classification == "tool_policy_refused"
            assert permissions[0].behavior == "deny"
        else:
            assert [event async for event in session.events()][-1].kind == "completed"
            assert permissions[0].behavior == "allow"
            assert permissions[1] == {"continue_": True}
    options = sdk.clients[0].options
    assert options.mcp_servers == for_claude(POLICY) and options.strict_mcp_config
    assert options.allowed_tools == [] and options.setting_sources == []
    assert options.env["FORGE_HARNESS_MCP"] == ""
    assert not Path(options.env["HOME"]).is_relative_to(workspace)
    assert not Path(options.env["HOME"]).exists() and sdk.clients[0].disconnected


async def test_failed_http_metadata_refuses_before_model_grant_or_native_process(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "work"
    dispatch, brief = frozen_inputs(
        checkout(workspace), driver="claude_code", execution_policy=POLICY
    )
    server, sdk = WireServer(dispatch, brief), FakeSDK(tmp_path)
    monkeypatch.setattr(FakeSDK.ClaudeSDKClient, "get_mcp_status", lambda: None, raising=False)
    metadata = MetadataServer("missing")
    driver = ClaudeCodeDriver(sdk, mcp_transport=httpx.MockTransport(metadata.handle))
    result = await execute(dispatch, server, driver, workspace)
    assert result.classification == "mcp_tool_unavailable"
    assert server.model_keys == 0 and not sdk.clients and not server.candidates


@pytest.mark.parametrize(
    "path", [".mcp.json", ".github/mcp.json", ".github/hooks/config.json", ".copilot/config.json"]
)
async def test_copilot_repository_config_is_refused_before_token_or_process(tmp_path, path):
    workspace = tmp_path / "work"
    dispatch, brief = frozen_inputs(
        checkout(workspace), driver="copilot_cli", metering="copilot-scoped-unmetered"
    )
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"command":"unapproved"}')

    async def forbidden(*args):
        raise AssertionError("No process may start")

    server = WireServer(dispatch, brief)
    result = await execute(
        dispatch, server, CopilotCLIDriver(transport_factory=forbidden), workspace
    )
    assert result.classification == "repository_driver_config_refused" and server.model_keys == 0


def test_native_interpolation_and_ambiguous_tool_names_never_become_authority():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="interpolate"):
        McpServerPolicy(
            name="docs",
            transport="http",
            tools=("lookup",),
            url="https://docs.invalid/${ANTHROPIC_AUTH_TOKEN}",
        )
    for tool in ("lookup.name", "x" * 64):
        policy = ExecutionPolicy(
            mcp_servers=(
                McpServerPolicy(
                    name="docs", transport="http", tools=(tool,), url="https://docs.invalid/mcp"
                ),
            )
        )
        with pytest.raises(LaneFailure):
            for_claude(policy)
