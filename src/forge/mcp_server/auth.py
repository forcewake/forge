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

R19 closes the classic-tools gap in the same model: every registered
surface (classic tools, resources, prompts) goes through one guard that
resolves the caller's principal and enforces a required scope BEFORE the
body runs — default deny. The classic family previously shipped with no
per-call check, so a read-only principal could drive the shared GitLab
token to write (comments, issue creation). Repository-target authorization
rides the same guard: a principal may carry an explicit repo allowlist
(``FORGE_MCP_TOKEN_REPOS``) checked against the caller-supplied project.
A06 extends the same primitive to the durable run surface
(:mod:`forge.mcp_server.tools_runs`), where the checked target is the
RUN's canonical subject resolved from durable state — a scoped token
allowlisted for repo A must not read (or discover via ``run_list``) the
runs, plans or evidence of repo B.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from starlette.datastructures import MutableMapping
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
        super().__init__(f"forbidden: principal {principal_name!r} lacks required scope {scope!r}")


@dataclass(frozen=True)
class McpPrincipal:
    """An authenticated MCP caller: a name, a scope set, and repo targets.

    ``repo_patterns`` is the principal's repository-target allowlist (R19):
    ``None`` means unrestricted — the default, so existing scoped-token
    configs keep working unchanged — while an empty tuple denies every
    repo target (an explicit-but-empty allowlist fails closed).
    """

    name: str
    scopes: frozenset[str]
    repo_patterns: tuple[str, ...] | None = None

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
        principals[token] = McpPrincipal(name=_token_label(token), scopes=frozenset(scopes))
    return principals


def parse_token_repos(raw: str | None) -> dict[str, tuple[str, ...]]:
    """Parse ``FORGE_MCP_TOKEN_REPOS`` (JSON: token-or-label → repo globs).

    Keys are the same token strings as in ``FORGE_MCP_SCOPED_TOKENS`` or the
    stable ``tok-<hash>`` label the audit logs show (no secret duplication is
    required — either form resolves). Values are fnmatch patterns matched
    against the caller-supplied project string (``group/app-*``); an EMPTY
    list denies every target. Malformed config raises at parse time — a typo
    must not silently widen access.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FORGE_MCP_TOKEN_REPOS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("FORGE_MCP_TOKEN_REPOS must be a JSON object token → repo patterns")
    repos: dict[str, tuple[str, ...]] = {}
    for token, patterns in data.items():
        if not isinstance(token, str) or not token:
            raise ValueError("FORGE_MCP_TOKEN_REPOS: token keys must be non-empty strings")
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            raise ValueError(
                "FORGE_MCP_TOKEN_REPOS: repo patterns for a token must be a list "
                "of non-empty strings"
            )
        repos[token] = tuple(patterns)
    return repos


def apply_repo_allowlist(
    principals: dict[str, McpPrincipal],
    token_repos: dict[str, tuple[str, ...]],
) -> dict[str, McpPrincipal]:
    """Return *principals* with their per-token repo allowlist attached.

    A token (or its ``tok-<hash>`` label) absent from *token_repos* stays
    unrestricted — the back-compat contract: only tokens the operator lists
    get narrowed.
    """
    if not token_repos:
        return principals
    resolved: dict[str, McpPrincipal] = {}
    for token, principal in principals.items():
        if token in token_repos:
            patterns: tuple[str, ...] | None = token_repos[token]
        else:
            patterns = token_repos.get(_token_label(token))
        resolved[token] = replace(principal, repo_patterns=patterns)
    return resolved


def repo_target_allowed(principal: McpPrincipal, project_ref: str) -> bool:
    """May *principal* target the caller-supplied project string?

    Patterns match the string AS SUPPLIED (case-sensitive fnmatch), so a
    restricted principal should address projects by path (``group/app``).
    Numeric IDs only pass against an explicitly digit pattern — a restricted
    principal cannot escape its allowlist by resolving through IDs.
    """
    if principal.repo_patterns is None:
        return True
    ref = project_ref.strip()
    return any(fnmatch.fnmatchcase(ref, pattern) for pattern in principal.repo_patterns)


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


def stash_principal(scope: MutableMapping[str, Any], principal: McpPrincipal) -> None:
    scope[_PRINCIPAL_SCOPE_KEY] = principal


def stash_session_factory(scope: MutableMapping[str, Any]) -> None:
    """Copy the parent app's session factory onto the ASGI scope."""
    parent_app = scope.get("app")
    state = getattr(parent_app, "state", None)
    scope[SESSION_FACTORY_SCOPE_KEY] = (
        getattr(state, "session_factory", None) if state is not None else None
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


def principal_from_context() -> McpPrincipal | None:
    """The principal for the in-flight MCP request, any protocol surface.

    The SDK binds the starlette request into a request-scoped contextvar
    for EVERY handler — tools, resources and prompts alike — so the guard
    needs no ``Context`` parameter (and tool signatures stay untouched).
    Outside a request, or without a middleware-stashed principal, this is
    None: callers must deny.
    """
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    request = context.request if context is not None else None
    if request is None:
        return None
    return principal_from_request(request)


def audit(principal: McpPrincipal, tool: str, target: str, outcome: str) -> None:
    """One structured line per authorized call — the attribution layer."""
    audit_logger.info(
        "principal=%s tool=%s target=%s outcome=%s",
        principal.name,
        tool,
        target,
        outcome,
    )


def audit_denied(
    principal: McpPrincipal | None,
    tool: str,
    target: str,
    required_scope: str,
) -> None:
    """WARNING line per denied call: who tried what, and what they lacked."""
    audit_logger.warning(
        "principal=%s tool=%s target=%s outcome=denied required_scope=%s",
        principal.name if principal is not None else "anonymous",
        tool,
        target,
        required_scope,
    )


def _deny_text(scope_needed: str, principal_name: str) -> str:
    """The model-actionable denial (same channel as the run surface)."""
    return f"FORBIDDEN: {McpAuthzError(scope_needed, principal_name)}"


def guard_fn(
    fn: Callable[..., Any],
    *,
    name: str,
    scope_needed: str,
    repo_arg: str | None = None,
) -> Callable[..., Awaitable[str]]:
    """Wrap an async MCP surface function with default-deny enforcement.

    BEFORE the wrapped body runs, the helper resolves the caller's principal
    (from the in-flight request) and requires *scope_needed*; when *repo_arg*
    names a project parameter, the supplied value is checked against the
    principal's repo allowlist. Every denial logs a WARNING audit line with
    principal, tool and required scope; every allowed call logs the standard
    INFO line. ``functools.wraps`` keeps the wrapped signature, so FastMCP
    schemas, tool names and descriptions are unchanged. The wrapped callable
    is typed loosely (the SDK's resource/prompt fns carry wide unions);
    every real call site is async and returns str.
    """

    @functools.wraps(fn)
    async def guarded(**kwargs: Any) -> str:
        principal = principal_from_context()
        target = str(kwargs.get(repo_arg, "")) if repo_arg else ""
        if principal is None or not principal.has(scope_needed):
            audit_denied(principal, name, target, scope_needed)
            return _deny_text(scope_needed, principal.name if principal else "anonymous")
        if repo_arg and not repo_target_allowed(principal, str(kwargs.get(repo_arg, ""))):
            audit_denied(principal, name, target, f"{scope_needed} (repo allowlist)")
            return f"FORBIDDEN: principal {principal.name!r} may not target {target!r}"
        result = await fn(**kwargs)
        audit(principal, name, target, "ok")
        return str(result)

    return guarded


def guarded_tool(
    mcp: FastMCP,
    scope_needed: str,
    *,
    repo_arg: str | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    """Register an async tool on *mcp* behind scope + repo-target enforcement.

    The registration half of :func:`guard_fn` — the ONE door every classic
    tool goes through. Scope mapping (closed set): list/get/read tools need
    ``forge:read``; anything mutating GitLab needs ``forge:runs:write``;
    approve/cancel actions would need ``forge:approvals:write`` (none exist
    on the classic surface yet); the master key holds all scopes.
    """

    def decorate(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        tool_name = name or fn.__name__
        return mcp.tool()(
            guard_fn(fn, name=tool_name, scope_needed=scope_needed, repo_arg=repo_arg)
        )

    return decorate
