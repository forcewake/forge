"""Native MCP configuration from frozen grants; no ambient config or credentials."""

import asyncio
import json
import re
from typing import Any

import httpx

from forge_protocol import ExecutionPolicy, McpServerPolicy

from .errors import ExitCode, LaneFailure

MAX_METADATA_BYTES = 1024 * 1024
VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})


def unavailable(classification: str = "mcp_capability_unavailable") -> LaneFailure:
    return LaneFailure(
        ExitCode.DRIVER,
        classification,
        "Approved MCP capability is unavailable; execution was refused",
    )


def public_servers(policy: ExecutionPolicy) -> tuple[McpServerPolicy, ...]:
    for server in policy.mcp_servers:
        if server.transport != "http" or server.credential_ref is not None:
            raise unavailable()
    return policy.mcp_servers


def for_claude(policy: ExecutionPolicy) -> dict[str, dict[str, Any]]:
    claude_tools(policy)
    return {server.name: {"type": "http", "url": server.url} for server in public_servers(policy)}


def for_copilot(policy: ExecutionPolicy) -> dict[str, dict[str, Any]]:
    return {
        "mcpServers": {
            server.name: {"type": "http", "url": server.url, "tools": list(server.tools)}
            for server in public_servers(policy)
        }
    }


def claude_tools(policy: ExecutionPolicy) -> frozenset[str]:
    names = frozenset(
        f"mcp__{server.name}__{tool}" for server in public_servers(policy) for tool in server.tools
    )
    if any(len(name) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None for name in names):
        raise unavailable()
    return names


def verify_native_status(policy: ExecutionPolicy, status: Any) -> None:
    servers = status.get("mcpServers") if isinstance(status, dict) else None
    if not isinstance(servers, list) or any(not isinstance(item, dict) for item in servers):
        raise unavailable("mcp_readiness_unavailable")
    names = [item.get("name") for item in servers]
    if (
        any(not isinstance(name, str) for name in names)
        or len(names) != len(set(names))
        or set(names) != {item.name for item in policy.mcp_servers}
    ):
        raise unavailable("mcp_isolation_failed")
    for grant in public_servers(policy):
        actual = next(item for item in servers if item["name"] == grant.name)
        tools = actual.get("tools")
        if actual.get("status") != "connected" or not isinstance(tools, list):
            raise unavailable("mcp_readiness_unavailable")
        offered = {
            item["name"]
            for item in tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if not set(grant.tools) <= offered:
            raise unavailable("mcp_tool_unavailable")
        config = actual.get("config")
        if config is not None and (
            not isinstance(config, dict)
            or config.get("type") != "http"
            or config.get("url") != grant.url
        ):
            raise unavailable("mcp_isolation_failed")


async def _metadata(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    async with http.stream("POST", url, headers=headers, json=body) as response:
        response.raise_for_status()
        raw = bytearray()
        async for part in response.aiter_bytes():
            raw.extend(part)
            if len(raw) > MAX_METADATA_BYTES:
                raise unavailable("mcp_metadata_too_large")
        session = response.headers.get("mcp-session-id")
        if session is not None and (
            not session
            or len(session) > 512
            or any(ord(char) < 33 or ord(char) > 126 for char in session)
        ):
            raise unavailable("mcp_protocol_error")
        if "id" not in body:
            return {}, session
        if response.headers.get("content-type", "").split(";")[0] == "text/event-stream":
            documents = [
                json.loads(
                    "\n".join(
                        line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                    )
                )
                for event in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n")
                if any(line.startswith("data:") for line in event.splitlines())
            ]
        else:
            documents = [json.loads(raw)]
        replies = [
            item for item in documents if isinstance(item, dict) and item.get("id") == body["id"]
        ]
        if (
            len(replies) != 1
            or replies[0].get("jsonrpc") != "2.0"
            or "error" in replies[0]
            or not isinstance(replies[0].get("result"), dict)
        ):
            raise unavailable("mcp_protocol_error")
        return replies[0]["result"], session


async def verify_public_servers(
    policy: ExecutionPolicy, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Read only metadata using a clean client; never call an MCP tool or model."""
    async with httpx.AsyncClient(
        transport=transport, timeout=5, follow_redirects=False, trust_env=False
    ) as http:
        for server in public_servers(policy):
            assert server.url is not None
            headers = {"Accept": "application/json, text/event-stream"}
            session = None
            try:
                result, session = await _bounded_metadata(
                    http,
                    server.url,
                    headers,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "forge-lane-metadata", "version": "0.1.0"},
                        },
                    },
                )
                version = result.get("protocolVersion")
                if version not in VERSIONS or not isinstance(
                    result.get("capabilities", {}).get("tools"), dict
                ):
                    raise unavailable("mcp_protocol_error")
                headers["MCP-Protocol-Version"] = version
                if session is not None:
                    headers["Mcp-Session-Id"] = session
                await _bounded_metadata(
                    http,
                    server.url,
                    headers,
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                tools, _ = await _bounded_metadata(
                    http,
                    server.url,
                    headers,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                offered = tools.get("tools")
                if not isinstance(offered, list) or tools.get("nextCursor") is not None:
                    raise unavailable("mcp_tool_unavailable")
                names = {item.get("name") for item in offered if isinstance(item, dict)}
                if not set(server.tools) <= names:
                    raise unavailable("mcp_tool_unavailable")
            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
                AttributeError,
                RecursionError,
                TimeoutError,
            ):
                raise unavailable("mcp_readiness_unavailable") from None
            finally:
                if session is not None:
                    try:
                        async with asyncio.timeout(1):
                            await http.delete(
                                server.url, headers={**headers, "Mcp-Session-Id": session}
                            )
                    except (httpx.HTTPError, TimeoutError):
                        pass


async def _bounded_metadata(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    return await asyncio.wait_for(_metadata(http, url, headers, body), timeout=5)
