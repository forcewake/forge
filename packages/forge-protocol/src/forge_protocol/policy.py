"""Explicit execution authority carried inside a frozen approved specification."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _no_controls(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("policy strings cannot contain control characters")
    if "${" in value:
        raise ValueError("policy strings cannot interpolate ambient environment variables")
    return value


def _tool_name(value: str) -> str:
    if "__" in value:
        raise ValueError("double underscores are reserved for native MCP tool qualification")
    return value


Name = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
    AfterValidator(_tool_name),
]
Tool = Annotated[
    str,
    Field(min_length=1, max_length=96, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"),
    AfterValidator(_tool_name),
]
Argument = Annotated[str, Field(max_length=4096), AfterValidator(_no_controls)]


class PolicyValue(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        validate_default=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class McpServerPolicy(PolicyValue):
    """A trusted server definition, never instructions to discover ambient config.

    Credential references name separately granted MCP credentials. They are not
    model keys, OIDC tokens or arbitrary environment variable names. A driver
    still has to prove support for this transport and exact tool permission.
    """

    name: Name
    transport: Literal["http", "stdio"]
    tools: Annotated[tuple[Tool, ...], Field(min_length=1, max_length=64)]
    required: bool = True
    url: Annotated[str, Field(max_length=4096), AfterValidator(_no_controls)] | None = None
    command: Annotated[str, Field(max_length=4096), AfterValidator(_no_controls)] | None = None
    args: Annotated[tuple[Argument, ...], Field(max_length=64)] = ()
    credential_ref: Name | None = None

    @model_validator(mode="after")
    def explicit_transport(self) -> Self:
        if len(set(self.tools)) != len(self.tools) or tuple(sorted(self.tools)) != self.tools:
            raise ValueError("MCP tools must be unique and sorted")
        if self.transport == "http":
            if self.url is None or self.command is not None or self.args:
                raise ValueError("HTTP MCP requires only a URL")
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("MCP URL must be HTTPS without credentials, query or fragment")
        elif (
            self.command is None
            or not self.command.startswith("/")
            or self.url is not None
            or any(part in ("", ".", "..") for part in self.command.split("/")[1:])
            or "\\" in self.command
        ):
            raise ValueError("stdio MCP requires an absolute preinstalled executable path")
        return self


class ExecutionPolicy(PolicyValue):
    """The fixed file-only profile plus individually approved MCP capabilities.

    This value authorizes no shell, repository writes, inherited MCP servers or
    ambient credentials. Presence is not a driver capability or trust attestation.
    """

    schema_version: Literal["forge.execution-policy/1"] = Field(
        default="forge.execution-policy/1", alias="schema"
    )
    profile: Literal["workspace-files-v1"] = "workspace-files-v1"
    mcp_servers: Annotated[tuple[McpServerPolicy, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def servers_are_unique(self) -> Self:
        names = tuple(server.name for server in self.mcp_servers)
        if len(set(names)) != len(names) or tuple(sorted(names)) != names:
            raise ValueError("MCP servers must have unique names in sorted order")
        return self
