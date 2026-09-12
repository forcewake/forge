# Upstream provenance

forge is a fork-in-spirit of [Codeward](https://github.com/Relrin/codeward),
an open-source AI agent platform for GitLab CE. This file records where the
code came from, what was imported, and how forge tracks upstream.

## Origin

| | |
|---|---|
| Upstream repository | <https://github.com/Relrin/codeward> |
| Pinned commit | `fd63ec8` (April 2026) |
| Upstream license | BSD 3-Clause — Copyright (c) 2026, Valeryi Savich |
| Notices | See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |

The import was performed once from the pinned commit; forge does not track
upstream `main` continuously.

## What was imported at M0

- Webhook gateway: secret validation, event parsing and routing
- Agents: code reviewer (including incremental review state), chat assistant,
  pipeline debugger, security triage
- Context engine: token budgets and redaction of sensitive data
- LiteLLM-based model routing configuration
- MCP server (`/mcp`) and MCP client support for external MCP servers
- Persistence (SQLAlchemy/Alembic), the Redis-backed worker, and flow
  scaffolding

forge renames user-facing and configuration surfaces: environment variables
are prefixed `FORGE_*`, the service configuration file is `forge.yml`,
per-project overrides live in `.forge.yml`, and the mention pattern defaults
to `@forge`.

## What was deliberately not carried forward as-is

The upstream YAML multi-agent flow engine (issue → MR `/implement` and
friends) has known defects and is **not** advertised as working in forge. It
will be superseded by a typed, durable controller; the design is recorded in
[docs/adr/0004](docs/adr/0004-controller-owns-lifecycle-implementer-proposes.md)
and [docs/adr/0005](docs/adr/0005-durable-execution-and-unknown-outcome.md).

Upstream queue semantics (non-atomic task claim, immediate requeue on failure,
TTL-only deduplication) are likewise insufficient for a write path that creates
commits and merge requests; they are replaced by the durable execution design
in ADR-0005.

## Policy

- **Upstream status.** Upstream is dormant: its last commit dates to April
  2026. forge does not depend on upstream activity.
- **Tracking fixes.** forge tracks upstream fixes opportunistically: when a
  relevant fix appears upstream, it is evaluated and cherry-picked if
  applicable. There is no automatic sync.
- **Recording divergences.** Every deliberate divergence from upstream
  behavior is recorded as an architecture decision record in
  [docs/adr/](docs/adr/0000-record-architecture-decisions.md).
- **Dependencies.** The dependency tree imported with the code is not assumed
  to carry the same license as Codeward; each dependency is evaluated as
  usual.
