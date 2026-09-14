# MCP Ecosystem & Best Practices for forge — Research (2026-09)

Scope: drives forge v0.6+ MCP design. forge is an agentic software factory (durable runs: plan → human gate → coding agents in CI → trusted publisher → Draft MR/PR). It has a legacy MCP server (fail-closed, mounted only with a bearer key, older transport, tools acting via the platform token) and a legacy MCP client used only by reactive flows. Everything below is as of 2026-09-14, checked against the official spec (modelcontextprotocol.io), the MCP GitHub org, vendor docs, and the Python SDK docs. Tags: [documented] = verified against the cited source; [inference] = my judgment. Each section ends with its sources.

Context headline: the MCP spec moved twice since the revision forge's legacy server likely targets. **2025-06-18** (auth overhaul, OAuth 2.1 resource-server model) → **2025-11-25** (sessions refined, URL-mode elicitation) → **2026-07-28** (current; stateless core, sessions and the initialize handshake REMOVED, MRTR replaces server-initiated requests). The deployed ecosystem floor is still the initialization-era protocol: GitLab's own MCP server supports up to 2025-11-25 and answers 2026-07-28 requests with 2025-11-25 [documented]. Design for the interop matrix, not just the latest spec.

---

## 1. Current spec state (2025-06-18 / 2025-11-25 / 2026-07-28)

### Revision timeline [documented]

- **2024-11-05**: original; HTTP+SSE transport (separate `/sse` endpoint + POST endpoint).
- **2025-03-26**: Streamable HTTP transport introduced (single MCP endpoint, POST per message, optional per-request SSE response), replacing HTTP+SSE.
- **2025-06-18**: authorization rebuilt on OAuth 2.1 (MCP server = resource server, Protected Resource Metadata per RFC 9728), `MCP-Protocol-Version` header required, structured tool output (`structuredContent`/`outputSchema`), elicitation introduced, tool annotations (`readOnlyHint` etc.).
- **2025-11-25**: session refinements; URL-mode elicitation (`elicitationId`, later removed in 2026-07-28); soft-deprecation of `includeContext` `"thisServer"`/`"allServers"`.
- **2026-07-28**: current revision. Major rework — see below. Formal feature lifecycle policy: Active → Deprecated → Removed, minimum **12-month deprecation window**, registry of deprecated features at `/specification/2026-07-28/deprecated`.

### 2026-07-28 core changes [documented — spec changelog]

Stateless protocol core (the headline):
- **Protocol-level sessions removed**: no `Mcp-Session-Id` header, no `initialize`/`notifications/initialized` handshake. Every request is self-describing via `_meta`: `io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientCapabilities`; clients SHOULD send `io.modelcontextprotocol/clientInfo`; servers SHOULD return `io.modelcontextprotocol/serverInfo` in each result's `_meta`. Version mismatch → `UnsupportedProtocolVersionError` (-32022).
- **`server/discover`**: a new RPC servers MUST implement, advertising supported protocol versions, capabilities (including extensions), and identity. Clients may call it before anything else.
- **State handles replace session state**: servers needing cross-call state mint explicit handles returned in tool results and passed back as ordinary tool arguments (e.g., `run_id`). Spec has a normative "Stateful Tools" guidance section: a handle is a *name, not a capability* — validate caller authorization against the handle on every call; opaque, entropy-backed, bounded lifetime, explicit expiry errors.
- **All results carry a required `resultType`**: `"complete"` or `"input_required"` (clients MUST treat missing field as `"complete"` for older servers).

Transport (Streamable HTTP, revised):
- Single MCP endpoint, POST only. **GET stream removed.** Response per request is either `application/json` or a request-scoped `text/event-stream` (SSE as a *response format*, carrying request-related `notifications/progress`/`notifications/message` then the final response — SSE the encoding is alive; SSE as a transport is not).
- **Required mirrored headers** (SEP-2243): `MCP-Protocol-Version`, `Mcp-Method` (all requests), `Mcp-Name` (`tools/call`, `resources/read`, `prompts/get`). Servers MUST validate header↔body agreement and reject with HTTP 400 + JSON-RPC `-32020 HeaderMismatch`. Rationale: gateways/LBs/WAFs route and meter without parsing bodies.
- `x-mcp-header` schema annotation: servers MAY mark primitive tool params to be mirrored into `Mcp-Param-{Name}` headers; clients MUST support it and MUST reject invalid tool definitions; sensitive values SHOULD NOT be marked (headers are visible to intermediaries). Base64 sentinel encoding `=?base64?...?=` for non-ASCII values.
- **SSE resumability removed** (`Last-Event-ID` gone): a broken response stream loses the in-flight request; clients MUST re-issue with a new request ID.
- Long-lived change notifications via **`subscriptions/listen`**: one long-lived POST-response SSE stream; client opts into `toolsListChanged` / `promptsListChanged` / `resourcesListChanged` / `resourceSubscriptions`; server acks and tags notifications with `io.modelcontextprotocol/subscriptionId`. Replaces HTTP GET stream + `resources/subscribe`.
- Removed: `ping`; `logging/setLevel` (log level now per-request `_meta` `io.modelcontextprotocol/logLevel`); `notifications/roots/list_changed`. Security requirements retained: Origin header validation (403 on invalid; DNS-rebinding defense), localhost binding SHOULD, auth SHOULD.
- stdio: unchanged in spirit — newline-delimited JSON-RPC over a client-launched subprocess; custom transports over byte streams SHOULD reuse stdio framing. Cancellation on stdio via `notifications/cancelled`; on Streamable HTTP by closing the response stream.

Server-initiated interaction → **MRTR (Multi Round-Trip Requests, SEP-2322)**:
- Replaces `elicitation/create`, `sampling/createMessage`, `roots/list` as JSON-RPC requests. A server needing input returns `InputRequiredResult` (`resultType: "input_required"`) with an `inputRequests` map plus optional `requestState`; the client gathers input and **retries the original request** with `inputResponses` (new JSON-RPC id). `notifications/elicitation/complete` and URL-mode `elicitationId` (2025-11-25) removed — correlation is via server-opaque `requestState`.

**Deprecated (still functional, ≥12-month window) [documented]**:
- **Roots, Sampling, Logging** (SEP-2577). Migrations: pass paths via tool params/resources; call LLM provider APIs directly instead of sampling; stderr/OTel instead of logging. *Directly relevant to forge: do not build model-provider integration on MCP sampling.*
- **HTTP+SSE transport** (deprecated since 2025-03-26, now formally in the lifecycle policy, SEP-2596). Interop: server may host old endpoints alongside; clients detect era by POSTing first and falling back to `initialize` on non-modern errors.
- **OAuth Dynamic Client Registration (RFC 7591)** deprecated in favor of **Client ID Metadata Documents (CIMD)**.
- 2025-03-26→2025-11-25 Streamable HTTP mechanics (sessions, GET stream, DELETE, resumability) preserved only in those revisions; 2026 servers SHOULD ignore `Mcp-Session-Id`/`Last-Event-ID` and 405 GET/DELETE.

Tasks (async) — now an official extension (see §4):
- `io.modelcontextprotocol/tasks` extension: `resultType: "task"` handles, poll `tasks/get`, `tasks/update` for mid-flight input, `tasks/cancel`; `tasks/list` removed; optional `notifications/tasks` via `subscriptions/listen`.

Other [documented]: caching hints `ttlMs` + `cacheScope` (`public`/`private`) required on `tools/list`, `prompts/list`, `resources/list`, `resources/read` results (SEP-2549); servers SHOULD return tools in deterministic order (prompt-cache friendly); `inputSchema`/`outputSchema` allow any JSON Schema 2020-12 keywords, `structuredContent` any JSON value; error-code allocation policy (`-32000..-32019` implementation, `-32020..-32099` spec); OTel trace-context conventions for `_meta` (`traceparent`, `tracestate`, `baggage`). SDKs at launch: TypeScript, Python, Go, C# Tier 1 (Python v2 is a breaking rework), Rust beta; ecosystem endorsements: AWS AgentCore, Cloudflare, Google Cloud, Microsoft Foundry, Anthropic, Sentry, Linear, FastMCP [documented — MCP blog].

Tools surface (stable across 2025-06-18 → 2026-07-28) [documented]:
- `tools/list` → `{ resultType, tools: [{ name, title?, description?, icons?, inputSchema, outputSchema?, annotations?, _meta? }], nextCursor?, ttlMs, cacheScope }`; pagination via cursors. Tool list MAY vary **by authorization presented on the request** (scopes) but MUST NOT vary per-connection.
- `tools/call` → `{ content: [text|image|audio|resource_link|resource], structuredContent?, isError? }`; two error channels: JSON-RPC protocol errors (`-32602` unknown tool) vs tool execution errors (`isError: true` with model-actionable text).
- Annotations: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` — clients MUST treat them as **untrusted** unless from a trusted server; they are hints, not authorization.

Protocol versioning/negotiation [documented]: initialization-era revisions negotiate in `initialize` (client sends its version, server responds with the one it supports; no automatic downgrade). 2026-07-28 does it per-request via `_meta` protocolVersion + `server/discover`, with an era-detection fallback (POST a modern request; if the 400 body is not a recognized modern JSON-RPC error, fall back to `initialize`). Servers supporting pre-2025-06-18 clients MAY treat a missing `MCP-Protocol-Version` header as 2025-03-26.

Sources:
- https://modelcontextprotocol.io/specification/ (spec index, security principles)
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://blog.modelcontextprotocol.io/posts/2026-07-28/ (release announcement)
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports and .../basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- https://modelcontextprotocol.io/extensions/tasks/overview
- https://modelcontextprotocol.io/community/feature-lifecycle

---

## 2. Security best practices (2026-07-28 official page)

The official page is at `modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices` (per-version docs trees exist for 2025-11-25 etc.). All items below are [documented] unless tagged.

**Confused deputy** (MCP proxy servers): a proxy using a static client ID toward a third-party AS, plus per-client DCR, plus the AS's consent cookie, lets an attacker skip consent and harvest authorization codes to their own `redirect_uri`. Mitigations REQUIRED of proxies: per-client consent registry checked *before* forwarding; consent UI identifying client name, scopes, redirect_uri, CSRF protection, clickjacking defense (`frame-ancestors`/`X-Frame-Options: DENY`); consent cookies `__Host-` prefixed, `Secure`/`HttpOnly`/`SameSite=Lax`, bound to `client_id`; exact-string `redirect_uri` matching; `state` generated and stored **only after** consent approval, single-use, short expiry. *Forge relevance: forge's MCP server is a proxy of sorts over forge's API; anything browser-OAuth-shaped it adds must implement the per-client consent pattern, not cookie-trust.*

**Token passthrough — explicitly forbidden**: an MCP server MUST NOT accept tokens not issued for it and MUST NOT forward unvalidated tokens downstream. Risks enumerated: circumvention of downstream rate limits/validation, broken accountability (downstream logs show the wrong identity — exactly what happens today when forge tools act via the platform token [inference]), lateral trust compromise, and "future compatibility". Audience binding per RFC 8707 (`resource` parameter) and RFC 9068; servers MUST validate the audience claim. The 2026-07-28 authorization spec: clients MUST send `Authorization: Bearer` on every request, tokens MUST NOT be in query strings; servers MUST 401 invalid/expired tokens, 403 insufficient scope (with `WWW-Authenticate: Bearer error="insufficient_scope", scope="..."` for step-up).

**State handle hijacking** (new for the stateless era): handles (`run_id`, cart IDs) are names, not capabilities. Servers MUST verify all inbound requests against the token-derived principal and MUST NOT treat handle possession as authentication; SHOULD bind stored state as `<user_id>:<handle>` keyed from the verified token, use CSPRNG opaque handles, expire them. *Forge's `run_id`s are exactly such handles — bind runs to the calling principal server-side.*

**SSRF in MCP clients** (directly relevant to forge's outbound MCP client, §5): OAuth discovery URLs (`resource_metadata`, `authorization_servers`, AS metadata) come from a potentially malicious server; clients deployed server-side MUST mitigate: HTTPS-only (loopback exception for dev), block private/loopback/link-local ranges (169.254.169.254 cloud metadata!), don't hand-roll IP parsers (encoding tricks), validate redirect hops, prefer egress proxies (Stripe Smokescreen cited), pin DNS between check and use. Also: authorization servers fetching CIMD documents face the same SSRF surface.

**Local MCP server compromise** (consent for one-click installs, sandboxing, dangerous-command highlighting) and **OAuth authorization URL validation** (`javascript:`/`data:` schemes, no shell execution when opening URLs, CSP) and **stdio-in-proxy escalation** (XSS → stolen proxy token → arbitrary child process): aimed at desktop/IDE clients; forge's server-side exposure is low, but forge's coding-agent images that run MCP clients should still follow the URL-validation and SSRF rules [inference].

**Mix-up attacks**: clients MUST validate RFC 9207 `iss` on authorization responses before code redemption (SEP-2468; ASes SHOULD emit `iss`, expected to become MUST). **Localhost redirect impersonation under CIMD**: a metadata URL proves domain control, not which local process listens — ASes must warn/display redirect hostnames. **CIMD trust policies**: allowlists, reputation, domain-age checks are AS-side options.

**Scope minimization**: publish minimal `scopes_supported`, challenge precisely per operation (`scope="..."` in 401/403), let clients accumulate via step-up; anti-patterns: omnibus scopes (`*`, `full-access`), whole-catalog challenges, treating scopes as sufficient without server-side authz logic.

**What an MCP server exposing repository operations must implement** (synthesis of the page + the Tools spec's "Security Considerations", which are normative for servers) [documented + inference]:
1. Per-tool authorization: validate the token audience (issued *for this server*), map scopes to tools, return only authorized tools from `tools/list` (explicitly allowed: "MAY vary by the authorization presented on the request"), enforce authz on every call including handle arguments.
2. Audit: log tool usage (Tools spec: clients SHOULD; operator guidance: log elevation events with correlation IDs; token-passthrough section explains audit breakdown when identity is forwarded). Emit who (principal), what (tool+args), on which resource, outcome.
3. Input validation, output sanitization, rate limiting per tool — server MUSTs in the Tools spec.
4. Server-vs-user identity separation: the MCP server is an OAuth resource server for its own API; when it calls downstream systems (GitHub/GitLab) it must do so under an identity that preserves the caller's context (forge-minted per-caller tokens, or an explicitly attributed service identity), never by relaying foreign tokens. Downstream actions should be attributable to (forge principal, human actor) pairs.
5. Tool descriptions/annotations are untrusted input for anything aggregating third-party servers; for forge's *own* server they're trusted self-descriptions, but forge's clients aggregating GitHub/GitLab servers must not trust their hints blindly.

Sources:
- https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools (Security Considerations)
- https://www.practical-devsecops.com/glossary/confused-deputy-attack-mcp/ (secondary corroboration)

---

## 3. Reference implementations

### github-mcp-server (GitHub official) [documented]

- **Tool surface (~100+ tools) in toolsets**: `repos` (contents, branches, commits, releases, search, push files, create/delete repo), `issues` (CRUD, comments, labels, sub-issues, search), `pull_requests` (create/update/merge, diffs, files, commits, reviews, threads, search), `actions` (workflows, runs, jobs, logs, artifacts, triggering — i.e., a CI "start/status/logs/cancel" surface exists as a mainstream precedent), `code_security` + `dependabot` + `secret_protection` (alert reads), `users`/`orgs` (search), `notifications`, `discussions`, `gists`, plus `context` (`get_me`, teams), `copilot`, `git`, `governance` (rulesets), `labels`, `projects`, `security_advisories`, `stargazers`, `code_quality`; remote-only extras (`copilot_spaces`, support docs search).
- **Modes**: remote hosted at `https://api.githubcopilot.com/mcp/` (OAuth or PAT; `insiders` path/header for experimental tools; Enterprise Cloud data-residency hosts `copiator-api.<subdomain>.ghe.com/mcp`); local via Docker image `ghcr.io/github/github-mcp-server` or Go binary; GitHub Enterprise Server requires the local server. Toolsets also bundle MCP Resources and Prompts.
- **Scoping switches**: `--read-only` / `GITHUB_READ_ONLY=1` — "will only offer read-only tools, preventing any modifications"; takes priority even if write tools are explicitly named. `--toolsets` / `GITHUB_TOOLSETS` allow-list (`all`, `default` = context+repos+issues+pull_requests+users); `--tools` for exact tool names (startup fails on unknown names). Stated rationale: limiting toolsets "can help the LLM with tool choice and reduce the context size".
- **Credential scoping guidance**: fine-grained PATs with only necessary permissions; **separate tokens per project/environment**; rotation; env-var storage, `chmod 600`, never committed. Each tool declares required OAuth scopes (`repo`, `read:org`, `security_events`, ...). "Lockdown mode" filters content surfaced from public repos by author push-access — documented as a best-effort **prompt-injection mitigation, explicitly not an authorization boundary**.
- Takeaway pattern for forge: a small static tool list, coarse toolsets narrowed by configuration, a global read-only switch, per-tool scope documentation, and configuration-time (not discovery-time) trust decisions. [inference from documented behavior]

### GitLab official MCP server [documented]

- **Status: Beta.** Experiment in GitLab 18.3 (feature flags) → beta in 18.6 → moved from Premium to **Free** in 19.2. Available on GitLab.com, Self-Managed, Dedicated; must be enabled at top-level group/instance.
- **Transport**: Streamable HTTP at `https://<gitlab.example.com>/api/v4/mcp` (recommended); stdio via `mcp-remote` proxy (Node 20+). Tool-name prefix via `X-Gitlab-Mcp-Server-Tool-Name-Prefix` header (truncated at 32 chars).
- **Auth**: OAuth 2.0 with **Dynamic Client Registration** (clients self-register; rate-limited to 10 registrations/IP/hour — an explicit nudge toward shared pre-registered apps) or pre-registered OAuth applications (instance/group/user scope, `mcp` scope). No PAT support documented.
- **Protocol versions**: supports 2025-03-26, 2025-06-18, 2025-11-25; a 2026-07-28 request is answered with 2025-11-25 because stateless features aren't implemented — the concrete data point for the "ecosystem floor" claim.
- **Security caveats documented by GitLab itself**: users are "responsible for guarding against prompt injection"; GitLab doesn't verify which client software presents a `clientId`; REST-created pre-registered apps don't enforce PKCE (clients should send `code_challenge`; GitLab accepts but doesn't require).

### Credential-scoping synthesis [inference]

Both reference servers authenticate the *user* (OAuth) and scope the *tool list* (read-only flags, toolsets, per-tool scope declarations). Neither accepts a platform token from the client and relays it — the pattern forge's legacy server uses (tools acting via the platform token) has no precedent among the reference implementations and matches the anti-patterns in §2.

Sources:
- https://github.com/github/github-mcp-server (README)
- https://docs.gitlab.com/user/model_context_protocol/mcp_server/
- https://docs.gitlab.com/user/gitlab_duo/model_context_protocol/
- https://gitlab.com/gitlab-org/gitlab/-/issues/561564 (feedback tracker)

---

## 4. MCP for factory operations (agent runs, gates, approvals)

What exists publicly:

- **MCP Tasks extension** (`io.modelcontextprotocol/tasks`) is *explicitly designed* for forge's shape [documented]. The spec's own "When to use Tasks" list: long-running operations ("CI pipelines ... minutes or hours"), **human-in-the-loop workflows ("Approval gates, review steps")**, external job systems ("wrap an API that already uses job IDs ... return a task when you create the job"), unreliable connections ("task IDs survive disconnects"), batch processing. Mechanics: `tools/call` (with tasks capability in `_meta`) → `CreateTaskResult` (`resultType: "task"`, `taskId`, status, `ttlMs`, `pollIntervalMs`) → poll `tasks/get` → states `working | input_required | completed | failed | cancelled` (last three terminal) → mid-flight input via `tasks/update` with `inputResponses` → `tasks/cancel` is *cooperative* (server acknowledges but isn't obligated). Server must durably create the task before responding; clients must persist task IDs to survive restarts. This maps 1:1 onto forge's durable runs [inference].
- **Temporal MCP servers** (community, not first-party): `GethosTheWalrus/temporal-mcp` (listed in Temporal's Code Exchange) and `alisaitteke/temporal-mcp` — tools for start workflow / status / signal / query / cancel / list / schedules / namespaces over the Temporal HTTP API. Temporal's blog "Durable MCP" and the `mcp-agent` "Durable Agents" pattern (Temporal workflows calling nested MCP servers with human-in-the-loop elicitation) show the durable-engine-as-MCP-server genre. None is official Temporal product [documented as listed; status inference: community-grade].
- **CI/CD precedent**: github-mcp-server's `actions` toolset (runs, jobs, logs, artifacts, trigger) — run start/status/logs/cancel as MCP tools is mainstream [documented].

Pattern assessment for forge [inference unless noted]:

- **Sensible**: `run_start` / `run_get` / `run_list` / `run_cancel` as tools; runs return task handles (Tasks extension) so any compliant client gets polling semantics for free; evidence bundles and plan diffs as **structured tool output** (`outputSchema`) and/or **resources** (`forge://runs/{id}/evidence`, `forge://runs/{id}/events`); `subscriptions/listen` (2026-07-28) or the run-events resource for change push; run events also work as plain paginated reads for 2025-era clients.
- **Approvals**: the protocol-native shape is the task state `input_required` + `tasks/update` (or MRTR `InputRequiredResult` for synchronous tools). BUT: an approval gate is an *authorization decision by a human*, not a model choice. Elicitation/MRTR input delivered to an LLM client must never be treated as human consent — the approver's identity must come from the authenticated principal at the forge API (the human clicking in forge's UI or an IDE client rendering the elicitation to a human and asserting their identity), never from model-generated `inputResponses`. Recommendation: expose `gate_approve`/`gate_reject` as tools guarded by a dedicated scope + audit trail, and treat `input_required` as a *notification convenience*, not the security boundary. [documented for mechanics; inference for the boundary]
- **Gimmick to avoid**: raw log tailing as tools (context flooding), "do-everything" mega-tools, push-only designs assuming all clients support subscriptions (polling is the default; `subscriptions/listen` is opt-in [documented]), exposing the trusted-publisher step over MCP at all.

Sources:
- https://modelcontextprotocol.io/extensions/tasks/overview
- https://temporal.io/code-exchange/temporal-mcp-server
- https://github.com/GethosTheWalrus/temporal-mcp and https://github.com/alisaitteke/temporal-mcp
- https://temporal.io/blog/durable-mcp-how-to-give-agentic-systems-superpowers
- https://docs.mcp-agent.com/mcp-agent-sdk/advanced/durable-agents
- https://github.com/github/github-mcp-server (actions toolset)

---

## 5. MCP clients inside server-side services (Python)

forge's model-provider and external tool-server integrations would consume MCP from Python server processes. Current SDK reality [documented]:

- **Python SDK v2** (stable line, breaking rework): `Client("https://host/mcp")` — a URL means Streamable HTTP. `Client` is an async context manager; constructing only picks the transport, `async with` opens it. High-level calls: `await client.list_tools()`, `await client.call_tool(name, args)` → result with `result.structured_content`. In-memory transport (`Client(server_object)`) for tests/embedding — real protocol layer without a network hop.
- **You own the HTTP client for auth/headers/timeouts/proxies**: build an `httpx2.AsyncClient(headers={"Authorization": "Bearer ..."}, timeout=httpx2.Timeout(30.0, read=300.0))` and pass it to `streamable_http_client(url, http_client=...)`. Defaults: 30s connect/write/pool, **300s read** (servers may hold response streams). `streamable_http_client` no longer accepts `headers=`/`timeout=` (TypeError). The SDK never closes a client it didn't create. Redirects: only same-origin (scheme/host/port) or http→https same-host are followed; anything else raises `MCPError`. TLS via OS trust store (`truststore`); in slim containers set `SSL_CERT_FILE` or `verify=`.
- **Auth persistence**: `OAuthClientProvider` is an `httpx2.Auth` that automates discovery (parses `WWW-Authenticate`, fetches Protected Resource Metadata, validates issuer) → registration → PKCE authorization → token exchange → silent refresh. You supply a `TokenStorage` (protocol: `get/set_tokens`, `get/set_client_info`) — **persist `client_info` too**, otherwise you mint a fresh registration per run. Per SEP-2352, key persisted credentials by issuer. CIMD: pass `client_metadata_url=` for 2026-07-28 servers advertising it. Machine-to-machine: `ClientCredentialsOAuthProvider` (client_id/secret + issuer) and `PrivateKeyJWTOAuthProvider`. Static bearer tokens: just put the header on your own `AsyncClient` — no MCP-level auth code involved.
- **stdio transport** (for local tool servers): `StdioServerParameters(command=..., args=..., env=...)`; the child gets a *minimal env allow-list* (HOME, PATH, ...), NOT your environment — pass secrets explicitly via `env=`; stderr redirectable via `stdio_client(params, errlog=...)`; process killed on context exit.
- **Lifecycle in long-running services**: v2 documents no auto-reconnect. The spec (2026-07-28) says a broken response stream loses the in-flight request and the client MUST re-issue [documented]. Practical shape for forge: long-lived `Client` per tool-server per worker process (or per-task contexts), call-wrap with reconnect/backoff, tool-list caching honoring `ttlMs` (and `cacheScope`), health check = `server/discover` where available (2026-07-28) else a cheap `tools/list` probe (also your schema-version canary). Concurrency: one async context per event loop; do not share an open client across event loops. [inference for the operational pattern]
- **v1 line** (still widely deployed): `mcp.client.streamable_http.streamablehttp_client(url, headers=...)` → `(read, write, get_session_id)` + `ClientSession` + explicit `await session.initialize()`. Forge's legacy client is presumably this shape; it keeps working against initialization-era servers, and the era-detection fallback (§1) is the migration bridge.

Sources:
- https://github.com/modelcontextprotocol/python-sdk
- https://py.sdk.modelcontextprotocol.io/client/transports/
- https://py.sdk.modelcontextprotocol.io/client/oauth-clients/
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http (re-issue semantics)
- Security best practices page (§2) for SSRF/egress-proxy requirements on server-side clients

---

## 6. Registries & discovery

- **Official MCP Registry**: launched in preview 2025-09-08 [documented]; Registry API reached **freeze v0.1** 2025-10-24 ("stable, no breaking changes for at least a month") with v1 GA to follow [documented as of the repo README]; live at `registry.modelcontextprotocol.io`, docs at `modelcontextprotocol.io/registry/about`. Community-driven under the `modelcontextprotocol` org (Registry Working Group: Stacklok lead, PulseMCP, TeamSpark, Ravenmail) [documented].
- **server.json**: standardized metadata: namespaced `name` (`io.github.<user>/...` or `me.<domain>/...`), version, description, `packages` (installables), `remotes` (hosted endpoints), environment-variable declarations. Published via `mcp-publisher` CLI.
- **Verification**: `io.github.*` namespaces require GitHub OAuth login or GitHub Actions OIDC; other domains require DNS or HTTP challenge proof of ownership. Ecosystem vision: official registry + federated community/sub-registries. Consumers: MCP clients for discovery; GitHub added registry-based allowlist controls for VS Code [documented].
- **Does discovery matter for forge?** Mostly no [inference]. Forge's MCP server is private infrastructure behind forge auth — it should not be publicly registered, and forge's clients should not install tool servers from a public registry into CI without pinning (supply-chain: registry metadata ≠ code attestation; GitHub applies deterministic package scans, but that's their pipeline, not forge's). Where discovery *does* help forge: (a) if forge ever ships a public/edge MCP surface, a server.json entry + verified domain is cheap credibility; (b) an *internal* catalog (forge config listing approved tool servers with pinned versions) mirrors the server.json shape without the public trust model.

Sources:
- https://github.com/modelcontextprotocol/registry
- https://modelcontextprotocol.io/registry/about
- https://registry.modelcontextprotocol.io/

---

## 7. ACP vs MCP — current status

- **ACP = Agent Client Protocol**, created by Zed: open protocol (JSON-RPC over stdio) connecting an *editor/client* to an *interactive coding-agent session* — session lifecycle, prompts, streaming updates, permission requests, file edits, terminal output. Standing slogan in coverage: "ACP connects the editor to the agent; MCP connects the agent to tools and data" — complementary, not competing [documented as the common framing].
- **Agent-side adoption (2026-09)**: **Gemini CLI** — native ACP [documented, first major adopter]; **GitHub Copilot CLI** — native (`copilot --acp`) [documented]; **opencode** — native (`opencode acp serve`) [documented, opencode.ai/docs/acp]; **Claude Code** — via the Zed-maintained adapter `@zed-industries/claude-code-acp` (wraps the Claude Agent SDK; not a native CLI flag) [documented]; **Grok/xAI coding agent** — reported ACP-speaking per third-party coverage and ACP ecosystem listings [documented as report; treat with mild caution]. Codex appears throughout ACP tooling [documented as ecosystem presence].
- **Client-side adoption**: long official list at agentclientprotocol.com/get-started/clients — Zed, JetBrains AI Assistant, Neovim plugins (CodeCompanion, avante.nvim, ...), Emacs (agent-shell), VS Code extensions, Sublime, Qt Creator, dozens of desktop/mobile/messaging clients; plus an ACP Registry for agent distribution and bridges (ACP→MCP gateway, ACP→AG-UI, stdio→WebSocket) [documented].
- **For forge**: ACP governs *interactive* editor↔agent sessions; forge's product is *durable, unattended* CI runs with human gates — a different lifecycle (crash-resilient task handles vs. live streams). ACP does not replace MCP for forge's server surface, and forge doesn't need to *speak* ACP for v0.6–0.8. Where ACP becomes relevant: a future "attach your editor to a forge run" feature (spawn `claude`/`opencode`/`copilot` as ACP agents from forge, or expose forge's coding agents to IDEs over ACP) — monitor, don't build. [inference]

Sources:
- https://agentclientprotocol.com/get-started/clients
- https://opencode.ai/docs/acp/
- https://zed.dev/acp
- https://www.npmjs.com/package/@zed-industries/claude-code-acp
- https://circleci.com/blog/acp-vs-mcp-whats-the-difference-for-agentic-coding/
- https://www.philschmid.de/acp-overview

---

## 8. Verdict for forge

### 8.1 What forge's durable factory should EXPOSE over MCP

Recommended static tool list (small, deterministic order, toolset-style grouping), with an authz matrix. Scopes: `forge:read` (discovery+reads), `forge:runs:write` (start/cancel), `forge:approvals:write` (gate decisions), `forge:admin` (config; default-ungranted). The trusted publisher is **never** exposed over MCP [inference].

| Tool | Scope | Notes |
|---|---|---|
| `run_start` | `forge:runs:write` | Returns Tasks-extension handle (`taskId`→`run_id`) when client opts in; otherwise returns `run_id` synchronously. Idempotency key param. |
| `run_get` | `forge:read` | Status, stage, linked plan/MR, `structuredContent` per `outputSchema`. |
| `run_list` | `forge:read` | Filtered by status/repo; paginated. |
| `run_cancel` | `forge:runs:write` | Cooperative semantics, like `tasks/cancel` — document that cancellation is best-effort. |
| `plan_get` / `plan_diff` | `forge:read` | The human-gate artifact; structured output. |
| `gate_approve` / `gate_reject` | `forge:approvals:write` | Step-up scope (403 + `WWW-Authenticate scope=`); audit principal; reject reason required. |
| `run_evidence_get` | `forge:read` | Evidence bundle: CI logs refs, test results, SBOM/provenance pointers — links, not blobs. |

Resources: `forge://runs/{run_id}/events` (append-only event log; read + subscribe under `resourceSubscriptions`), `forge://runs/{run_id}/evidence`, `forge://plans/{plan_id}`. Resource links (`resource_link` content type) from `run_get` to evidence/events so clients can pull lazily. Approvals-as-`input_required` is a convenience for IDE clients, never the authz boundary (§4) [inference].

Server obligations (from §2, restated as forge requirements): OAuth 2.1 resource server with RFC 9728 Protected Resource Metadata when browser flows exist; otherwise scoped bearer keys (current fail-closed model) with **audience bound to forge's MCP server**; per-tool scope enforcement; tools/list filtered by caller scopes; audit log (principal, tool, args, target, outcome, correlation ID); handle authorization keyed `<principal>:<run_id>` (state-handle hijacking); input validation + rate limits per tool. Kill the platform-token passthrough: tools acting via one forge platform token breaks audience binding, audit attribution, and blast-radius containment — mint per-caller forge tokens or execute under an explicitly attributed service identity. [documented anti-pattern → inference for the fix]

Legacy server migration targets: HTTP+SSE / session-era transport → Streamable HTTP via current SDK; keep the fail-closed mount; keep the tool list static (don't chase dynamic tool discovery); annotations as hints only. Support the initialization-era protocol for 2025-era clients (the ecosystem floor, per GitLab's behavior) while adding `server/discover` + per-request `_meta` for 2026 clients when the SDK makes it cheap. [inference from documented facts]

### 8.2 What forge should CONSUME over MCP

- **Coding agents in CI**: consuming GitHub/GitLab MCP servers is fine for interactive-flavored flows (IDE-side assistants, human-gate reviewers with AI helpers) — that's what those servers optimize for. For forge's *own* CI agents, first-party REST/GraphQL APIs with forge's scoped tokens remain the better default: typed clients, retries, provenance, no tool-selection LLM overhead in the hot path. MCP indirection pays off where a model picks the tools, not where code calls them. [inference]
- **Forge's MCP client** (legacy reactive flows): keep, modernize with SDK v2 — owned `httpx2.AsyncClient` (bearer header or `ClientCredentialsOAuthProvider`), issuer-keyed token persistence, reconnect-and-reissue wrapper (broken stream = lost request), `tools/list` cached per `ttlMs`, health probe via `server/discover`→`tools/list`, SSRF guardrails (no private ranges, egress proxy) since it runs server-side. Do not rebuild model-provider integration on MCP **sampling** — deprecated in 2026-07-28; call provider APIs directly. [documented deprecation; inference for architecture]
- **ACP**: monitor only (§7). **Registry**: internal catalog with pinned versions; no public listing; no registry-driven auto-install into CI. [inference]

### 8.3 What to avoid (condemned list)

1. Platform-token passthrough / single shared downstream identity (§2 anti-pattern, §8.1).
2. New builds on HTTP+SSE transport, protocol sessions (`Mcp-Session-Id`), GET streams, `Last-Event-ID` resumability, Sampling, Roots, Logging — all deprecated; ≥12-month removal clock running [documented].
3. Treating tool annotations, descriptions, or elicitation answers as authorization signals.
4. Trusting handle possession (run IDs) without principal binding.
5. Eliciting approvals *from the model* as the gate mechanism.
6. Exposing publisher/deploy or secret-signing operations as MCP tools.
7. Public-registry discovery as a trust source for CI-installed tool servers.

### 8.4 Phased plan (v0.6+)

- **Phase 0 — v0.6 (foundation, security-first)**: port legacy server to Streamable HTTP on the current Python SDK; keep fail-closed bearer mount but make tokens forge-scoped with audience validation; per-tool authz table (matrix above); audit log; static deterministic tool list; read-only global switch (github-mcp-server pattern); era-compat fallback for 2025-era clients.
- **Phase 1 — v0.7 (durable surface)**: full read surface (`run_get/list`, `plan_get/diff`, `run_evidence_get`) with `outputSchema` structured outputs; resources for events/evidence; Tasks-extension integration for `run_start` (persist task IDs; polling first, subscriptions opt-in later).
- **Phase 2 — v0.8 (gates + clients)**: `gate_approve/gate_reject` with step-up scope and human-identity binding; `subscriptions/listen` for run-event push; modernize the MCP client (issuer-keyed token storage, health probes, `ttlMs` caching); OAuth PRM/CIMD onboarding if browser-interactive clients appear.
- **Phase 3 — v0.9+ (2026-spec alignment, optional)**: adopt stateless core (`server/discover`, per-request `_meta`, MRTR) once Tier-1 SDK + peer servers (GitHub/GitLab) move; ACP evaluation only if "editor attaches to forge run" becomes a requirement; public registry listing only if forge ever exposes an external MCP surface.

Sources:
- §8 synthesizes all prior sections; the normative anchors are the 2026-07-28 changelog, security best practices, authorization, Tools, and Tasks pages cited above, plus github/gitlab implementation docs.

---

*Compiled 2026-09-14. Spec dates and vendor statuses verified against live official sources on this date; re-verify the 2026-07-28 adoption state of GitHub/GitLab servers before finalizing v0.9.*
