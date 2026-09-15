# ADR-0022: MCP server provisioning for harness lanes

Status: accepted (2026-09-15)
Context: forge's implementer harnesses (Claude Code, Grok Build, opencode,
GitHub Copilot CLI) run headless in ephemeral CI lanes. Coding agents are
dramatically more useful with live documentation lookups (Context7,
Microsoft Learn) and other MCP tool surfaces, but today a lane has no way
to receive MCP servers — and an uncontrolled way would be a prompt-injection
hole (a repo-supplied `.mcp.json` is auto-loaded by several drivers even in
untrusted `-p` runs, per the harness security research). Live verification
on the lab ([research/mcp-lane-live-evidence.md](../research/mcp-lane-live-evidence.md)):
claude-code and opencode both invoke Context7 and Microsoft Learn tools
natively end-to-end (job 651: 4 `mcp__*` tool_use events, both servers
`connected` at init; opencode job 655: native `<server>_<tool>` calls).
grok 1.0.30 does not connect http servers from settings.json headlessly —
documented as a vendor gap; the provisioning stays (future versions) and
the lane still reaches the servers.

## Decision

1. **One canonical config, per-driver rendering.** A single project CI
   variable `FORGE_HARNESS_MCP` — JSON in the Claude `mcpServers`
   interchange schema (the one shape every vendor accepts, with small
   translations) — is the ONLY source of MCP servers for a lane. The
   canonical form:

       {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}

   Rendering per driver (implemented in `src/forge/harnesses/mcp.py` for
   the Actions lane; mirrored as self-contained shell in the GitLab
   templates):
   - **claude-code** — verbatim `{"mcpServers": ...}` file via
     `--mcp-config`; tool grants appended per server
     (`mcp__<name>__*`, `mcp__<name>`);
   - **grok-build** — verbatim `mcpServers` into `~/.grok/settings.json`
     (Grok follows the Gemini-CLI conventions; claude-shaped `http`
     entries pass through);
   - **copilot** — verbatim `mcpServers` into `~/.copilot/mcp-config.json`
     (documented Copilot CLI location), plus an explicit
     `--allow-tool <server>` grant per server (everything not granted is
     auto-denied in `-p` mode);
   - **opencode** — schema-translated under the `mcp` key of the same
     generated `opencode.json` that carries the permission map:
     `http` → `remote` (Streamable HTTP), `stdio` → `local` with a
     command array.
   The transforms are unit-tested; the templates are pinned by contract
   tests (`TestMcpProvisioning`), so a lane cannot silently drift away
   from the module behavior.

2. **Strict isolation is the constant, servers are the variable.** The
   claude lane ALWAYS runs with `--mcp-config <file> --strict-mcp-config`
   — with an empty config when `FORGE_HARNESS_MCP` is unset. Strict mode
   locks out the target repository's own `.mcp.json`, user configs, and
   any other source: MCP servers, like every other input, cross the
   project boundary as CI variables (ADR-0012), never ride in repo
   content. This is the same posture as `--setting-sources ''`.

3. **Secrets stay in masked variables.** The config JSON may reference
   environment variables with `${VAR}` — the drivers expand them at
   runtime from the job environment. Server API keys therefore live in
   separate masked+protected CI variables and never in
   `FORGE_HARNESS_MCP` itself.

4. **Fail-closed parsing.** A malformed config (bad JSON, non-object,
   unknown entry type, missing url/command) refuses the lane — the run
   fails with a clear reason instead of silently executing without the
   servers the plan may depend on.

5. **MCP never weakens the proposal-only contract.** The mechanical
   deny rules (`--disallowedTools`/`--deny`/permission maps/`--deny-tool`)
   and the `FORBIDDEN` push URL are unaffected by MCP grants; an MCP
   server cannot grant the lane a write capability it does not have. The
   residual risk of MCP is exfiltration and prompt injection via tool
   results — addressed by the same boundary as everything else: the lane
   holds no forge secrets (ADR-0016), so a malicious tool server can
   reach only the project's own harness credentials, and servers are
   chosen by the project owner, not by repo content (Decision 2).

## Consequences

- Documentation-lookup harnesses (the common "implementer needs current
  library/API knowledge" case) work out of the box on every driver with
  one CI variable.
- The same variable travels to the GitHub Actions lane via
  `vars.FORGE_HARNESS_MCP` in the harness workflow; the Actions lane's
  `forge.harness_entry` renders it with the identical module.
- Non-goals (v1): forge-side MCP-client consumption inside the run loop
  (the durable core talks first-party APIs — ADR-0021 §8.2); per-tool
  allowlisting granularity in `FORGE_HARNESS_MCP` (server-level is enough
  for doc lookups; tighten later if needed); auto-discovery of MCP
  servers from repo config — permanently rejected as an injection
  surface; grok-native MCP support (vendor gap in 1.0.30 — the settings
  write remains so the lane upgrades cleanly when fixed upstream).
