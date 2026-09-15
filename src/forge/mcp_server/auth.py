"""Scoped authorization for the MCP surface (ADR-0021 §4, R3 §8.1).

The legacy mount had ONE bearer key whose tools acted with the privileged
platform token — the documented MCP anti-pattern (single shared identity:
no audience binding, no audit attribution, unbounded blast radius). The v0.8
model:

- **Scoped tokens**: ``FORGE_MCP_SCOPED_TOKENS`` (JSON: ``{"<token>":
  ["forge:read", ...]}``) names a principal per token with an explicit scope
  set. The legacy ``FORGE_MCP_KEY`` keeps working as the master principal
  (all scopes) so existing deployments don't break.
- **Call-time enforcement**: every run-surface tool checks the caller's
  scopes BEFORE touching durable state — a token without ``forge:read``
  gets a model-actionable denial, never a stack trace. (tools/list
  filtering by caller scopes is SDK-gated; enforcement here is the
  security boundary, not the advertisement.)
- **Audit**: every authorized call logs principal, tool, target and outcome
  to the ``forge.mcp_server.audit`` logger — the attribution the shared
  platform token could never provide.

Scopes are a closed set; unknown scopes in the config are rejected at parse
time (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

#: Closed scope set (R3 §8.1 authz matrix). ``forge:admin`` is reserved for
#: config-surface tools and is default-ungranted; the trusted publisher is
#: NEVER exposed over MCP.
MCP_SCOPES: tuple[str, ...] = (
    "forge:read",
    "forge:runs:write",
    "forge:approvals:write",
    "forge:admin",
)

#: The scope key the ASGI middleware stashes the resolved principal under.
_PRINCIPAL_SCOPE_KEY = "forge.mcp.principal"

#: The scope key for the PARENT app's session factory — the mounted FastMCP
#: sub-app overwrites ``scope["app"]`` with itself, so durable-state tools
#: reach the database through this stash instead.
SESSION_FACTORY_SCOPE_KEY = "forge.mcp.session_factory"

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("forge.mcp_server.audit")


class McpAuthzError(Exception):
    """The caller's token does not grant the scope the tool requires."""

    def __init__(self, scope: str, principal_name: str) -> None:
        self.scope = scope
        self.principal_name = principal_name
        super().__init__(
            f"forbidden: principal {principal_name!r} lacks required scope {scope!r}"
        )


@dataclass(frozen=True)
class McpPrincipal:
    """An authenticated MCP caller: a name and an explicit scope set."""

    name: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


MASTER_SCOPES = frozenset(MCP_SCOPES)


def _token_label(token: str) -> str:
    """Stable, non-reversible principal label for a token (audit-safe)."""
    return "tok-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def parse_scoped_tokens(raw: str | None) -> dict[str, McpPrincipal]:
    """Parse ``FORGE_MCP_SCOPED_TOKENS`` (JSON: token → scope list).

    Unknown scopes reject the WHOLE config at parse time (fail closed) —
    a typo must not silently grant nothing (or worse, everything). Returns
    an empty map when unset or blank.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_MCP_SCOPED_TOKENS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("FORGE_MCP_SCOPED_TOKENS must be a JSON object token → scopes")
    principals: dict[str, McpPrincipal] = {}
    for token, scopes in data.items():
        if not isinstance(token, str) or not token:
            raise ValueError("FORGE_MCP_SCOPED_TOKENS: token keys must be non-empty strings")
        if scopes == "*":
            scopes = list(MCP_SCOPES)
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ValueError("FORGE_MCP_SCOPED_TOKENS: scopes for a token must be a list")
        unknown = [s for s in scopes if s not in MCP_SCOPES]
        if unknown:
            raise ValueError(
                f"FORGE_MCP_SCOPED_TOKENS: unknown scopes {unknown} "
                f"(valid: {', '.join(MCP_SCOPES)})"
            )
        principals[token] = McpPrincipal(
            name=_token_label(token), scopes=frozenset(scopes)
        )
    return principals


def resolve_principal(
    bearer_token: str,
    master_key: str | None,
    scoped_principals: dict[str, McpPrincipal],
) -> McpPrincipal | None:
    """Map a bearer token to its principal, or None when unknown.

    The master key (legacy ``FORGE_MCP_KEY``) wins as the all-scope
    principal; scoped tokens resolve to their configured scope set.
    """
    if master_key and bearer_token == master_key:
        return McpPrincipal(name="master", scopes=MASTER_SCOPES)
    principal = scoped_principals.get(bearer_token)
    if principal is not None:
        # Re-label scoped principals consistently with the master path.
        return principal
    return None


def stash_principal(scope: dict, principal: McpPrincipal) -> None:
    scope[_PRINCIPAL_SCOPE_KEY] = principal


def stash_session_factory(scope: dict) -> None:
    """Copy the parent app's session factory onto the ASGI scope."""
    parent_app = scope.get("app")
    scope[SESSION_FACTORY_SCOPE_KEY] = getattr(parent_app, "state", None) and (
        parent_app.state.session_factory
    )


def session_factory_from_request(request: Request) -> Any:
    return request.scope.get(SESSION_FACTORY_SCOPE_KEY)


def principal_from_request(request: Request) -> McpPrincipal | None:
    return request.scope.get(_PRINCIPAL_SCOPE_KEY)


def require_scope(request: Request, scope_needed: str) -> McpPrincipal:
    """Return the caller's principal or raise :class:`McpAuthzError`."""
    principal = principal_from_request(request)
    if principal is None:
        # Unguarded mount (deployment fronts /mcp with its own auth) — the
        # run surface still refuses without a resolved principal.
        raise McpAuthzError(scope_needed, "anonymous")
    if not principal.has(scope_needed):
        raise McpAuthzError(scope_needed, principal.name)
    return principal


def audit(principal: McpPrincipal, tool: str, target: str, outcome: str) -> None:
    """One structured line per authorized call — the attribution layer."""
    audit_logger.info(
        "principal=%s tool=%s target=%s outcome=%s",
        principal.name,
        tool,
        target,
        outcome,
    )
