"""MCP server provisioning for harness lanes (ADR-0022).

Forge exposes ONE canonical config to every lane: the CI variable
``FORGE_HARNESS_MCP`` — JSON in the Claude ``mcpServers`` interchange
schema (the de-facto standard all four drivers accept with small
translations):

    {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
     "local-thing": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}}

Per-driver rendering:

- **claude-code** — the config verbatim under ``{"mcpServers": ...}`` in a
  temp file passed via ``--mcp-config``; ``--strict-mcp-config`` means ONLY
  these servers load (a repo's own ``.mcp.json`` is ignored — the same
  injection-surface reduction as ``--setting-sources ''``). ``${VAR}``
  references inside the config are expanded by claude at runtime from the
  job environment, so secrets stay in separate masked CI variables.
- **grok-build** — verbatim under ``mcpServers`` in ``~/.grok/settings.json``
  (Grok follows the Gemini-CLI conventions: claude-shaped ``http``/``sse``
  entries).
- **copilot** — verbatim under ``mcpServers`` in ``~/.copilot/mcp-config.json``
  (documented Copilot CLI location; tool access scoped with
  ``--allow-tool 'SERVER(tool)'``).
- **opencode** — TRANSLATED under the ``mcp`` key of opencode.json:
  claude's ``http`` becomes opencode's ``remote`` (Streamable HTTP), and a
  stdio entry ``{command, args}`` becomes ``{"type": "local", "command":
  [command, *args]}``.

Fail-closed: a malformed ``FORGE_HARNESS_MCP`` (bad JSON, non-object,
unknown entry shape) raises :class:`McpConfigError` — the lane refuses to
start the driver rather than silently running without (or with broken)
servers.
"""

from __future__ import annotations

import json
from typing import Any


class McpConfigError(ValueError):
    """The FORGE_HARNESS_MCP value is not a usable MCP server config."""


def parse_servers(raw: str | None) -> dict[str, dict[str, Any]]:
    """Parse the canonical ``mcpServers`` map out of the CI variable value.

    Accepts both the bare object (``{"context7": {...}}``) and the wrapped
    form (``{"mcpServers": {...}}``). An empty/blank value means "no MCP
    servers" (an empty map — still rendered, so strict isolation holds).
    """
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"FORGE_HARNESS_MCP is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigError("FORGE_HARNESS_MCP must be a JSON object")
    if set(data.keys()) == {"mcpServers"}:
        data = data["mcpServers"]
        if not isinstance(data, dict):
            raise McpConfigError("FORGE_MCP mcpServers must be a JSON object")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise McpConfigError(f"MCP server {name!r}: entry must be an object")
        etype = entry.get("type")
        if etype in (None, "http", "sse"):
            if not entry.get("url"):
                raise McpConfigError(f"MCP server {name!r}: http/sse entry needs a url")
        elif etype == "stdio":
            if not entry.get("command"):
                raise McpConfigError(f"MCP server {name!r}: stdio entry needs a command")
        else:
            raise McpConfigError(f"MCP server {name!r}: unknown type {etype!r}")
    return data


def for_claude(servers: dict[str, dict[str, Any]]) -> str:
    """File content for ``--mcp-config`` (canonical schema, verbatim)."""
    return json.dumps({"mcpServers": servers}, indent=2)


def for_grok(servers: dict[str, dict[str, Any]]) -> str:
    """Content of ``~/.grok/settings.json`` (claude-shaped entries pass through)."""
    return json.dumps({"mcpServers": servers}, indent=2)


def for_copilot(servers: dict[str, dict[str, Any]]) -> str:
    """Content of ``~/.copilot/mcp-config.json`` (same mcpServers structure)."""
    return json.dumps({"mcpServers": servers}, indent=2)


def for_opencode(servers: dict[str, dict[str, Any]]) -> str:
    """The ``mcp`` key value for opencode.json (schema-translated)."""
    translated: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        etype = entry.get("type")
        if etype in ("http", "sse", None):
            translated[name] = {
                "type": "remote",
                "url": entry["url"],
                **({"headers": entry["headers"]} if entry.get("headers") else {}),
                "enabled": True,
            }
        elif etype == "stdio":
            translated[name] = {
                "type": "local",
                "command": [entry["command"], *(entry.get("args") or [])],
                **({"environment": entry["env"]} if entry.get("env") else {}),
                "enabled": True,
            }
        else:  # parse_servers already rejected this; defensive only
            raise McpConfigError(f"MCP server {name!r}: unknown type {etype!r}")
    return json.dumps(translated, indent=2)
