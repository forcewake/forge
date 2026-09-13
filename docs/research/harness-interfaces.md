# Coding-Agent Harness Programmatic Interfaces — Research (September 2026)

Purpose: implementation reference for a common Python `HarnessDriver` protocol wrapping five coding-agent harnesses (OpenAI Codex CLI, Claude Code / Claude Agent SDK, Grok Build CLI, OpenCode, plus ACP as a future common transport). Everything below was checked against official documentation in September 2026. Items marked **[uncertain]** could not be confirmed from official docs and must be verified against the live binary/spec before implementation freeze.

Note on doc hosts: `developers.openai.com/codex/**` returns 308 redirects to `learn.chatgpt.com/docs/**` — the latter is the current official Codex doc site. Claude Code docs moved to `code.claude.com` (old `docs.claude.com/en/docs/claude-code/**` paths redirect there). xAI's Grok Build docs live at `docs.x.ai/build/**`; the open-source repo is `xai-org/grok-build` (GitHub), which also mirrors user-guide docs. OpenCode docs are at `opencode.ai/docs/**`; source at `sst/opencode`.

---

## 1. OpenAI Codex CLI

### Install
- npm: `npm install -g @openai/codex`; Homebrew: `brew install --cask codex`; standalone installers `curl -fsSL https://chatgpt.com/codex/install.sh | sh` (macOS/Linux) and a PowerShell one-liner for Windows; static release binaries on GitHub Releases (e.g. `codex-x86_64-unknown-linux-musl.tar.gz`).
- Repo `openai/codex` is **Apache-2.0**. The binary is free software; *using* it against OpenAI backends requires either "Sign in with ChatGPT" (Plus/Pro/Business/Edu/Enterprise plans, credentials in `~/.codex/auth.json`) or an API key. Per-run key env var: `CODEX_API_KEY` (works with `codex exec`, `codex review`, the TS SDK, and `codex exec-server --remote`).
- Official GitHub Action: **`openai/codex-action@v1`** (repo `openai/codex-action`). It installs the CLI and, when given an API key, starts a **secure Responses-API proxy** so `OPENAI_API_KEY` is never handed to the job environment.

### Headless / programmatic invocation
- `codex exec [PROMPT|-]` is the non-interactive mode. `PROMPT` may be `-` to read the full prompt from stdin; if stdin is piped AND a prompt argument is given, the argument is the instruction and stdin is context.
- Key flags (from the current CLI reference and non-interactive docs):
  - `--json` (alias `--experimental-json`) — stdout becomes a JSONL event stream. Without it, progress goes to **stderr** and only the final agent message goes to stdout.
  - `-C, --cd <dir>` — set working directory.
  - `-m, --model <model>` — model override (global flag, also valid on `exec`).
  - `-s, --sandbox <read-only|workspace-write|danger-full-access>` — read-only is the default for `exec`.
  - `-a, --ask-for-approval <policy>` — documented values on the reference page today: `on-request | never` (**[uncertain]** the older `untrusted`/`on-failure` values no longer appear in the current reference; verify against the installed binary with `codex exec --help`).
  - `--dangerously-bypass-approvals-and-sandbox` (`--yolo`) — no sandbox, no approvals; docs: "Only use inside an externally hardened environment."
  - `--full-auto` — **deprecated** compatibility flag (prints warning); use `--sandbox workspace-write`.
  - `--ephemeral` — do not persist session rollout files to disk (good for CI).
  - `--skip-git-repo-check` — allow running outside a git repo.
  - `-o, --output-last-message <path>` — write final assistant message to a file (still also printed to stdout).
  - `--output-schema <path>` — final response must conform to a JSON Schema file (structured outputs).
  - `--ignore-user-config`, `--ignore-rules` — skip `$CODEX_HOME/config.toml` / execpolicy rules (deterministic CI runs).
  - `-c key=value` — inline config override; `--profile/-p` named profile; `--image/-i` attachments.
  - **[uncertain]** `--include-plan-tool` is no longer documented anywhere in the current reference; treat as removed.
- A second programmatic surface exists: the **TypeScript Codex SDK** (`@openai/codex-sdk`, lives in `openai/codex` at `sdk/typescript`) — `new Codex()` → `codex.startThread()` → `await thread.run(prompt)` with event streaming; plus `codex app-server` / `codex exec-server` JSON-RPC modes used by IDE integrations (**[uncertain]** app-server protocol stability is not documented for external consumers; prefer `codex exec --json` for a Python wrapper).

### Output format (events)
`codex exec --json` emits JSONL with a stable `type` discriminator. Documented event types: `thread.started`, `turn.started`, `item.started`, `item.completed` (also `item.*` family; `item.updated` appears in the SDK/streaming path but is not shown in the exec docs sample — **[uncertain]** for exec), `turn.completed`, `turn.failed`, `error`. Item types: agent message, reasoning, command execution, file change, MCP tool call, web search, plan update.

### Result extraction
- Final message: parse last `item.completed` of agent-message type, or just use `-o/--output-last-message file`, or read stdout in non-JSON mode.
- Structured result: `--output-schema schema.json` constrains the final message to your JSON Schema.
- Exit codes: **not enumerated in the docs** — **[uncertain]**; treat `0` = success, non-zero = failure, and additionally check for a terminal `error`/`turn.failed` event.

### Session / resume
`codex exec resume <SESSION_ID> "<prompt>"`; `codex exec resume --last "<prompt>"` (most recent in cwd); `--all` widens the search to any directory. Sessions are persisted as "rollout" files under `~/.codex` unless `--ephemeral`. `thread.started` carries the thread/session id for later resume.

### Permission / approval for unattended runs
For unattended: `--sandbox workspace-write` (or `danger-full-access` in an isolated container) + `--ask-for-approval never` (or `--dangerously-bypass-approvals-and-sandbox`). No interactive fallback exists in `exec`; approval prompts cannot be answered. `--ephemeral` avoids polluting session state.

### Usage / token reporting
Per-turn only: `turn.completed` carries `usage: { input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens }`. There is **no documented session-aggregate usage event** in the exec JSONL — the driver must sum per-turn usage across a run. **[uncertain]** whether cost (USD) is emitted at all in exec events; not documented.

### Cancellation
No documented cancel API for `exec` — kill the process (SIGINT/SIGTERM). Session state persists, so `resume` after a kill is possible. **[uncertain]** exact behavior of partial `rollout` files after SIGKILL.

### CI / containers & ToS
- The GitHub Action docs explicitly recommend `openai/codex-action` over manual installs in Actions. Action inputs: `prompt`/`prompt-file` (exactly one), `codex-args` (JSON array or shell string of extra CLI flags), `model`, `effort`, `sandbox` (`workspace-write|read-only|danger-full-access`), `output-file`, `output-schema`/`output-schema-file`, `permission-profile`, `codex-version`, `codex-home`, `openai-api-key`, `safety-strategy` (`drop-sudo` default | `unprivileged-user`+`codex-user` | `read-only` | `unsafe` — Windows requires `unsafe`), `allow-users`/`allow-bots`. Output: `final-message`.
- **Two-job pattern (official)**: job 1 runs the action with `contents: read`, `persist-credentials: false` on `refs/pull/N/merge`, exposes `final_message` and/or uploads artifacts (e.g. via `output-file`); job 2 (`needs: codex`, gated on the message being non-empty) holds the write permissions (`issues: write`, `pull-requests: write`) and posts the comment / opens the PR — with **no API key present**. This is the officially blessed privilege split: the patch/diff produced in job 1 is the handoff artifact. (Earlier Codex-era workflows exported a `codex.patch` artifact that job 2 applied with `git apply`; the current docs describe the pattern in terms of `output-file`/final-message artifacts rather than a named `patch` output — **[uncertain]** whether a first-class patch output exists in `codex-action@v1` today; the README lists only `final-message`.)
- Security rules from official docs: never set `OPENAI_API_KEY`/`CODEX_API_KEY` as a job-level env var in workflows that run repo-controlled code; prefer workload-identity federation; treat `~/.codex/auth.json` "like a password" and never use ChatGPT-plan auth in public-repo CI. No official Docker image is documented — **[uncertain]**; the action's `drop-sudo` strategy (revoke sudo, `no_new_privs`, empty capabilities) is the official containerization-adjacent hardening, and docs call `danger-full-access` appropriate only in "an isolated CI runner or container".

Sources: openai/codex README (github.com/openai/codex); learn.chatgpt.com/docs/non-interactive-mode; learn.chatgpt.com/docs/developer-commands; learn.chatgpt.com/docs/github-action; github.com/openai/codex-action README; github.com/openai/codex/blob/main/sdk/typescript/README.md.

---

## 2. Claude Code / Claude Agent SDK

### Install
- CLI: native installer (`curl -fsSL https://claude.ai/install.sh | bash`) or `npm install -g @anthropic-ai/claude-code`. Proprietary; must be run "as published by Anthropic" (no forks/modification).
- SDK Python: `pip install claude-agent-sdk` (repo `anthropics/claude-agent-sdk-python`). SDK TypeScript: `npm install @anthropic-ai/claude-agent-sdk` (repo `anthropics/claude-agent-sdk-typescript`). Both SDKs drive the installed CLI (`cli_path` option) as a subprocess and stream typed messages.

### Programmatic surfaces
1. **Headless CLI**: `claude -p "<prompt>"` (print mode; reads stdin too).
2. **Agent SDK**: `query(prompt, options)` — stateless one-shot async iterator of messages; `ClaudeSDKClient` (Py) / interactive `query()` (TS) — stateful multi-turn with `interrupt()`, `set_permission_mode()`, `set_model()`, MCP management. "Runs the agent loop in your own process" — same tools, hooks, context management as Claude Code.
3. Non-Python/TS languages: official guidance is to run the CLI as a subprocess with `-p --output-format json` — exactly the HarnessDriver pattern.

### Headless CLI flags (documented)
- `-p` / `--print`; `--output-format text|json|stream-json`; `--input-format text|stream-json`.
- `--bare` — "recommended mode for scripted and SDK calls, and will become the default for `-p`": skips hooks, skills, subagents, plugins, MCP, auto memory, CLAUDE.md; **requires `ANTHROPIC_API_KEY` (no OAuth/keychain)** — i.e. Anthropic's own recommended CI posture is API-key-only.
- Permissions: `--permission-mode default|acceptEdits|plan|auto|dontAsk|bypassPermissions` (`manual` = alias of `default`); `--dangerously-skip-permissions` (≡ `bypassPermissions`); `--allowedTools "Read,Edit,Bash(git diff *)"`; `--disallowedTools`; `--permission-prompts none` (v2.1.259+, denies anything that would prompt).
- Sessions: `--resume <session_id|path.jsonl>`, `--continue`, `--session-id <uuid>`, `--fork-session`.
- Model: `--model sonnet|opus|haiku|fable|<full-name>`; overrides `ANTHROPIC_MODEL` env and settings. Aliased models resolve to the latest snapshot — pin a full name for reproducibility.
- Limits/structure: `--max-turns N` (errors when reached), `--max-budget-usd <n>` (dollar budget, print mode), `--json-schema <schema>` (validated `structured_output`), `--append-system-prompt`, `--system-prompt`, `--mcp-config`, `--agents`, `--settings`.
- Settings isolation: `--setting-sources <user,project,local>` — comma list; default for interactive CLI is all three; **policy/managed settings and CLI flags always load and cannot be excluded**. SDK default is the opposite: `setting_sources=[]` (no filesystem settings) unless you opt in — this is the SDK's isolation model; `--setting-sources ''` is the CLI equivalent trick. (Known bug class: empty array serializing to `--setting-sources ""`; issue #252 in the TS repo.)
- Exit codes: 0 success; non-zero failure; SIGTERM → **143** (SessionEnd hooks still run; resume continues the unfinished turn). Background bash tasks killed ~5 s after result; `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` caps idle wait (default 10 min).

### stream-json format
NDJSON messages: `system/init` (model, tools, MCP servers, plugins, `capabilities`), `assistant`, `user` (tool_results), `stream_event` (partial deltas, requires `--include-partial-messages` + `--verbose`), `system/api_retry` (attempt, max_retries, retry_delay_ms, error_status, error category), `result` (always last). Subagent messages carry `parent_tool_use_id`. Structured output via `--json-schema` lands in the result's `structured_output` field.

### Result / usage extraction
Terminal `result` message (type `SDKResultMessage`, subtype e.g. `success`) fields: `result` (final text), `is_error`, `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd` (**client-side estimate**; per-model breakdown in recent versions), `usage: { input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens }`, `session_id`, `permission_denials`, `structured_output`, `terminal_reason` (`aborted_streaming`/`aborted_tools` on interruption). Usage is reported **per turn** in `-p`/interactive mode (each turn ends with its own `result`); a multi-turn `ClaudeSDKClient` session therefore yields per-turn receipts, and the aggregate is the sum. `--max-budget-usd` and `total_cost_usd` give the only first-class dollar figures.

### Permission / approval model (SDK)
Evaluation order: hooks → deny rules → ask rules → permission mode → allow rules → `canUseTool` callback. Modes: `default`, `dontAsk` (everything that would prompt is denied; `canUseTool` never fires — best "locked" default for unattended classification runs), `acceptEdits` (auto-approves edits + mkdir/touch/rm/rmdir/mv/cp/sed inside cwd/additionalDirectories), `bypassPermissions` (approves everything except critical-path `rm`/`rmdir`, org-forced ask connectors, user-interaction tools; deny rules and hooks still apply), `plan` (edits never auto-approved), `auto` (model classifier decides, v2.1.218+). `can_use_tool` (Py async callback → `PermissionResultAllow`/`PermissionResultDeny`) is the programmatic approver. Warning: `allowed_tools` does not constrain `bypassPermissions`.

### Sessions / resume / model (SDK)
`ClaudeAgentOptions`: `resume` (session id), `continue_conversation`, `fork_session`, `session_id`, `model`, `fallback_model`, `max_turns`, `max_budget_usd`, `cwd`, `env`, `setting_sources`, `can_use_tool`, `hooks`, `mcp_servers`, `permission_mode`, `extra_args` (raw CLI passthrough). Python helpers: `list_sessions()`, `get_session_messages()`. Mid-run: `set_permission_mode()`, `set_model()`, `interrupt()` (must drain buffered messages afterwards).

### Auth / ToS constraints (critical for automation)
- **Consumer ToS** (Free/Pro/Max, claude.ai OAuth login): individual, first-party use. **Commercial ToS** (Team/Enterprise/API): required for programmatic/product use.
- Anthropic "does not permit third-party developers to offer Claude.ai login into their own applications"; may not "route requests through Free, Pro, or Max plan credentials on behalf of their users"; may not "collect, store, or intermediate Claude.ai credentials or session tokens". OAuth is "intended exclusively for purchasers of Claude Free, Pro, Max, Team, Enterprise subscription plans".
- For SDK/product integrations, use **API key auth** (Console or Bedrock/Vertex). `--bare` enforces this mechanically. Consequence for HarnessDriver: ship an API-key path as the only supported unattended mode for Claude; do not adopt claude.ai login credentials.
- Branding: calling your agent "Claude Code" is not permitted ("Claude Agent" preferred if required at all).

### CI / containers
Official reference devcontainer in the `anthropics/claude-code` repo (`.devcontainer/`) plus a docs page (code.claude.com/docs/en/devcontainer) covering auth persistence across rebuilds and firewalling. No officially published Docker image — **[uncertain]**; the devcontainer reference is the sanctioned pattern (rootless container + allowlisted network egress + `--dangerously-skip-permissions` only inside it, per Anthropic's own security guidance).

Sources: code.claude.com/docs/en/agent-sdk/overview; .../agent-sdk/permissions; .../agent-sdk/python; .../headless; .../cli-reference; .../legal-and-compliance; .../devcontainer; github.com/anthropics/claude-agent-sdk-python; github.com/anthropics/claude-agent-sdk-typescript (incl. issue #252); github.com/anthropics/claude-code/.devcontainer.

---

## 3. Grok Build CLI (xAI)

### Install
- Official installer only: `curl -fsSL https://x.ai/cli/install.sh | bash` (macOS/Linux), `irm https://x.ai/cli/install.ps1 | iex` (Windows). No brew/npm channel documented. Open-source repo: `xai-org/grok-build` (Rust) — **[uncertain]** license identifier (verify LICENSE in repo; not stated on docs pages). Default model: `grok-4.6` (also exposed on the xAI API directly).

### Headless / programmatic invocation
- `grok -p "<prompt>"` (`-p, --single`) triggers headless mode; also `--prompt-json <JSON>` and `--prompt-file <PATH>`. Stdin is **not** read for the prompt (use `--prompt-file` or command substitution).
- Output: `--output-format plain|json|streaming-json|streaming-messages-json`; `--include-partial-messages` adds raw deltas (only affects `streaming-messages-json`).
- Model: `-m, --model <MODEL>` (e.g. `grok-4.6`); custom providers via `~/.grok/config.toml` (`[model.<name>]` with `model`, `base_url`, `env_key`).
- Sessions: `-s, --session-id <UUID>` (create with explicit id; errors if in use — does **not** resume), `-r, --resume <ID|title>` (scripts should use IDs), `-c, --continue` (most recent in cwd), `--fork-session`. Stored under `~/.grok/sessions`.
- Other headless-relevant flags: `--cwd`, `--no-alt-screen`, `--no-auto-update` (or `auto_update = false` in config; env `GROK_DISABLE_AUTOUPDATER`), `--max-turns <N>`, `--tools`, `--disallowed-tools`, `--allow <RULE>`, `--deny <RULE>`, `--sandbox <PROFILE>`, `--effort`, `--system-prompt-override`, `--agent`, `--no-subagents`, `--worktree [NAME]`.
- **`--debug-file` does not exist** in the current docs — logging is via `**[GROUND TRUTH 2026-09-13: `grok --debug-file <FILE>` EXISTS and works in grok 1.0.30 — verified against the installed CLI; the doc omission below is inaccurate for current builds.]** GROK_LOG_FILE` env (path) and `RUST_LOG`; headless logs go to stderr.
- ACP mode: `grok agent stdio` (JSON-RPC over stdio: `initialize`, `authenticate` — methods `xai.api_key`, `cached_token`, `session/new`, `session/prompt` → returns completion metadata, `session/update` → `agent_message_chunk`). ACP assumes grok already authenticated locally or `XAI_API_KEY` set.

### Output format (events)
`streaming-json` = NDJSON, switch on `type`: `text` (chunk in `data`), `thought` (reasoning), `tool_call` (`toolCallId`, `toolName`, `kind`, `status`, `rawInput`, `content`, `locations`), `tool_call_update` (`status`, `rawOutput`), `usage` (per-response boundary: `messageId`, `stopReason`, `usage`, `signature`), `plan`, `available_commands`, `error`, and **`end` — always last**, carrying `stopReason`, `sessionId`, `requestId`, `usage`, `num_turns`, `modelUsage`. `streaming-messages-json` mirrors the Anthropic/Messages-style wire format (`system`/`init`, `assistant`, `user` with tool_result blocks, `result`, plus `message_start`/`content_block_*`/`message_delta`/`message_stop` framing).

### Result extraction
`--output-format json` yields one final object: `text`, `stopReason` (snake_case: `end_turn`, `max_tokens`, …), `sessionId`, `requestId`, `num_turns`, `usage`, `modelUsage`, `total_cost_usd`, `total_cost_usd_ticks` (1 USD = 10^10 ticks). Error path: non-zero exit + `{"type":"error","message":...}`. Extraction pattern: `grok -p ... --output-format json | jq -r '.text'`.

### Usage / token reporting (best-in-class of the four)
- Per-response (per-turn): `usage` events and `usage` fields — snake_case `input_tokens` (uncached), `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`.
- Session/run aggregate: final `end` event / json object adds `modelUsage` (per model: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `modelCalls`, `costUSD`) and `total_cost_usd`. Partial-run caveats: `usage_is_incomplete` / `cost_is_partial` markers; incomplete cost omits dollar floats.
- Both per-turn **and** aggregate are first-class — the driver can pass through `modelUsage`/`total_cost_usd` directly.

### Permission / approval for unattended runs
- `--always-approve` (alias `--yolo`) — identical to `--permission-mode bypassPermissions`; deny rules, hooks, and admin locks still apply. Without it, headless **hangs** waiting for approval — the documented unattended contract is: either pass `--always-approve`, or constrain via `--tools`/`--disallowed-tools`/`--deny` and accept that write actions stall.
- Other modes exist via `--permission-mode` (mirrors the Claude-style mode names — **[uncertain]** full enumeration; docs page confirms `bypassPermissions` only).

### Auth (CI-relevant)
- Default: browser OAuth on first run; credentials in `~/.grok/auth.json` (0600), auto-refreshed; `grok login` (`--oauth` default, `--device-auth`/`--device-code` for headless printers), `grok logout` clears.
- **API-key mode exists**: `export XAI_API_KEY="xai-..."` — but it is a **fallback** only when no session token is active (interactive token wins); in CI use it exclusively (no `auth.json` present) or run `grok logout` first.
- Auth precedence: per-model `api_key`/`env_key` in config.toml > active session token > `XAI_API_KEY`. External auth provider binaries supported (`GROK_AUTH_PROVIDER_COMMAND` et al.), enterprise OIDC (PKCE) supported. Team admins can enable Zero Data Retention.

### Cancellation / exit codes
Exit codes documented: `0` success, `1` auth/network/runtime error, `130` SIGINT, `143` SIGTERM. No in-band cancel for headless (kill the process); ACP mode has `session/cancel`.

### CI / ToS
No official Docker image or GitHub Action documented — **[uncertain]**. CI guidance in docs amounts to: `XAI_API_KEY`, `--no-auto-update`, parse JSON rather than trust exit codes. ToS constraint: **[uncertain]** — no explicit third-party-automation clause found in the reviewed official pages (grok.com terms govern; SuperGrok/plan-based OAuth auth vs `XAI_API_KEY` pay-per-token distinction suggests the API key is the clean automation path, mirroring the Anthropic situation, but this is inference, not documented policy).

Sources: docs.x.ai/build/overview; docs.x.ai/build/cli/headless-scripting; x.ai/build/changelog; github.com/xai-org/grok-build user-guide docs 02-authentication.md and 14-headless-mode.md (repo mirror of official docs).

---

## 4. OpenCode

### Install
- `curl -fsSL https://opencode.ai/install | bash`, `npm i -g opencode-ai`, brew (`brew install sst/tap/opencode`), etc. (per opencode.ai install page). Open source (`sst/opencode`). **[uncertain]** license identifier — historically SML-variant; verify.

### Two programmatic surfaces
1. **CLI run**: `opencode run [message..]` — non-interactive one-shot. Flags: `-m/--model provider/model`, `-s/--session <id>` (continue), `-c/--continue` (last session), `--fork`, `--agent`, `--format default|json` ("raw JSON events"), `--file/-f`, `--title`, `--variant` (reasoning effort), `--auto` (**auto-approve permissions not explicitly denied** — the unattended flag), `--dir`, `--attach http://localhost:4096` (reuse a running server, avoids MCP cold-start), `--port/--username/--password` for attach auth, `--command`, `--share`, `--thinking`; global `--print-logs`, `--log-level`. Exit codes not documented — **[uncertain]**.
2. **Server + SDK** (the robust path): `opencode serve [--port 4096] [--hostname 127.0.0.1] [--cors origin] [--mdns]` runs a headless HTTP server with an **OpenAPI 3.1 spec at `/doc`**. Basic auth via `OPENCODE_SERVER_PASSWORD` (+`OPENCODE_SERVER_USERNAME`, default `opencode`). JS/TS SDK: `@opencode-ai/sdk` — `createOpencode()` (spawns server + client) or `createOpencodeClient({ baseUrl })`. **No Python SDK documented** — the Python driver should speak raw HTTP against the OpenAPI spec (or generate a client from `/doc`).

### Server API (exact endpoints)
- Sessions: `POST /session` (`{parentID?, title?}`), `GET /session`, `GET/PATCH/DELETE /session/:id`, `POST /session/:id/abort` (**first-class cancel**), `/fork`, `/summarize`, `/revert`, `/unrevert`, `/share`, `/init`.
- Messaging: `POST /session/:id/message` (sync prompt — body: `{model: {providerID, modelID}, parts: [{type:"text", text}]}`), `POST /session/:id/prompt_async` (204), `GET /session/:id/message[s]`, `POST /session/:id/command`, `POST /session/:id/shell`.
- Permissions: SDK method `postSessionByIdPermissionsByPermissionId` → **`POST /session/:id/permissions/:permissionID`** to reply to an asked permission (**[uncertain]** exact payload shape — `response: "once"|"always"|"reject"`; verify in `/doc`).
- Events: `GET /event` — SSE; first event `server.connected`, then bus events. `GET /global/event`, `GET /global/health`.
- Config/providers: `GET /config`, `PATCH /config`, `GET /config/providers`, `GET /agent`, OAuth `POST /provider/{id}/oauth/authorize|callback`, files `GET /file/content?path=`, `GET /find?pattern=`.
- Structured output: prompt with `format: {type: "json_schema", schema, retryCount}` → result on `structured_output`; failure surfaces `StructuredOutputError`.
- SSE event types (from generated SDK types — code-derived, **[verify against /doc]**): `session.created|updated|deleted|error`, `message.updated|removed|part.updated|part.delta`, `session.next.text.delta`, `session.next.tool.called|progress|success|failed`, `permission.asked`/`permission.updated` (+v2 `permission.v2.asked`), `question.v2.asked`, `todo.updated`, `file.edited`, `session.diff`, plus pty/tui events. Treat names as version-sensitive.

### Permission config (unattended)
`opencode.json` (JSONC allowed, `"$schema": "https://opencode.ai/config.json"`):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "*": "ask",
    "edit": "allow",
    "webfetch": "allow",
    "bash": { "*": "ask", "git *": "allow", "rm *": "deny" }
  }
}
```
Values: `"allow" | "ask" | "deny"`; last matching pattern wins; `*`/`?` globs. Agent-level overrides under `"agent": {"<name>": {"permission": {...}}}`. Keys: `read, edit, glob, grep, bash, task, skill, lsp, question, webfetch, websearch, external_directory, doom_loop`. Unattended recipe: write a temp config where everything is `allow`/`deny` (never `ask`) **or** run `opencode run --auto` (auto-approves anything not explicitly denied). Headless `ask` without `--auto` prompts on the terminal (once/always/reject) — do not rely on it in automation. Driver can inject config via `OPENCODE_CONFIG` (file path) or `OPENCODE_CONFIG_CONTENT` (inline JSON) env vars — ideal for safe serialization from Python (`json.dumps`), no shell interpolation.
- Config precedence: remote → global `~/.config/opencode/opencode.json` → `OPENCODE_CONFIG` → project `opencode.json` → `.opencode/` dirs → `OPENCODE_CONFIG_CONTENT` → managed/MDM (highest).

### Providers / models config
```json
{
  "provider": { "anthropic": { "options": { "apiKey": "{env:ANTHROPIC_API_KEY}" } } },
  "model": "anthropic/claude-sonnet-4-5",
  "small_model": "anthropic/claude-haiku-4-5"
}
```
`{env:VAR}` / `{file:path}` substitution; `enabled_providers`/`disabled_providers`; per-provider `timeout`/`headerTimeout`/`chunkTimeout`. Per-request model override in the prompt body (`providerID`/`modelID`) or `-m provider/model`.

### Usage / token reporting
Token usage and cost live on **message info** (`message.updated` events and `GET /session/:id/message/:messageID`; SDK `session.message()` returns `{info, parts}`) — `info.tokens` (input/output/reasoning/cache read+write) and `info.cost` — **[uncertain]** exact field names, verify in `/doc` types.gen. There is no single terminal "result" event with a receipt; the driver must capture the final assistant message part (from SSE or a final `session.message` fetch) and read usage from message info. Per-message granularity; session aggregate = sum.

### CI / containers
No official Docker image or GitHub Action documented — **[uncertain]**. Recommended pattern implied by docs: `opencode serve` (headless, basic auth, non-localhost hostname) inside the container; drive over HTTP; `--print-logs --log-level DEBUG` for CI logs.

Sources: opencode.ai/docs/server; opencode.ai/docs/sdk; opencode.ai/docs/cli; opencode.ai/docs/permissions; opencode.ai/docs/config; sst/opencode generated types via DeepWiki (code-derived, secondary).

---

## 5. Cross-cutting comparison & HarnessDriver design

### Comparison table

| Dimension | Codex CLI | Claude Code | Grok Build | OpenCode |
|---|---|---|---|---|
| Invocation | `codex exec [prompt]` | `claude -p` (or Agent SDK in-process) | `grok -p` | `opencode run` or HTTP server |
| Event stream | `--json` JSONL: `thread.started`, `turn.*`, `item.*`, `error` | `--output-format stream-json`: `system/*`, `assistant`, `user`, `stream_event`, `result` | `--output-format streaming-json`: `text`, `thought`, `tool_call[_update]`, `usage`, `plan`, `end`, `error` | SSE over HTTP (`GET /event`) or `--format json` |
| Terminal result | stdout final message / `-o file` / `--output-schema` | last `result` message (`result`, `structured_output`) | `end` event / `json` object (`text`, `stopReason`) | final assistant part + `structured_output` (json_schema format) |
| Usage visibility | per-turn only (`turn.completed.usage`: input/cached_input/output/reasoning tokens) | per-turn `result.usage` + `total_cost_usd` (estimate) | per-response `usage` events **and** aggregate `modelUsage` + `total_cost_usd` | per-message `tokens`/`cost` on message info |
| Resume | `exec resume <id|--last>` | `--resume <id>` / `--continue` / SDK `resume` | `--resume <id>` / `-c` | `--session <id>` / `-c`; server sessions persistent |
| Cancel | kill process | `interrupt()` (SDK); SIGTERM→143 | kill (130/143); ACP `session/cancel` | `POST /session/:id/abort` (first-class) |
| Unattended approval | `--sandbox` + `--ask-for-approval never` / `--yolo` | `--permission-mode dontAsk|bypassPermissions` / `--dangerously-skip-permissions` / `canUseTool` | `--always-approve`/`--yolo` (hangs otherwise) | config `permission` map (allow/deny, no ask) or `--auto` |
| Structured output | `--output-schema <file>` | `--json-schema` / SDK `output_format` | none documented (wrap `text`) | prompt `format: json_schema` |
| Auth model | ChatGPT login or `CODEX_API_KEY`; CI: proxy in codex-action | API key for automation (ToS bans third-party claude.ai login); `--bare` enforces | `XAI_API_KEY` (fallback) or OAuth `auth.json` | provider API keys via config `{env:}` |
| License (code) | Apache-2.0 | proprietary (run as published) | open repo, license **[uncertain]** | open source, license **[uncertain]** |
| Official CI support | `openai/codex-action@v1` + 2-job pattern | official devcontainer reference | none documented | none documented |

### Per-turn vs aggregate usage
- Per-turn receipts: Codex (`turn.completed.usage`), Claude (`result.usage` per turn), Grok (`usage` events). Grok additionally emits a run aggregate (`modelUsage`, `total_cost_usd`); Claude emits `total_cost_usd` per turn with no run-level aggregate in one-shot mode; Codex emits no aggregate and no documented cost.
- Driver rule of thumb: **sum per-turn receipts yourself; treat any harness-provided aggregate/cost as advisory** (Claude's `total_cost_usd` is explicitly a client-side estimate; Grok marks partial runs `cost_is_partial`).

### Proposed `HarnessDriver` contract (Python protocol)

```python
class HarnessEvent:        # normalized
    kind: Literal["session_start", "turn_start", "text_delta", "reasoning_delta",
                  "tool_call", "tool_result", "permission_request",
                  "file_change", "usage", "turn_end", "error", "done"]
    raw: dict               # untouched harness payload (lossless audit trail)

class UsageReceipt:
    input_tokens: int; cached_input_tokens: int; output_tokens: int
    reasoning_tokens: int; cost_usd: float | None; scope: Literal["turn", "run"]; source: str

class HarnessResult:
    status: Literal["ok", "error", "cancelled", "budget_exceeded", "max_turns", "timeout"]
    exit_code: int | None
    final_text: str | None
    structured: dict | None           # from output-schema / json-schema / structured_output
    session_id: str | None            # for resume
    usage: list[UsageReceipt]         # per-turn receipts, summed view derived
    changed_files: list[str] | None   # candidate set, see below

class HarnessDriver(Protocol):
    def launch(self, cwd: Path, brief: str, *, model: str | None = None,
               resume_session: str | None = None,
               max_turns: int | None = None, budget_usd: float | None = None,
               schema: dict | None = None,
               on_event: Callable[[HarnessEvent], None] | None = None) -> HarnessResult: ...
    async def cancel(self) -> None: ...
```

Design guidance derived from the research:
1. **Prefer subprocess+JSONL/NDJSON for all four CLIs** (Codex `--json`, Claude `--output-format stream-json` or SDK, Grok `--output-format streaming-json`); OpenCode is the exception — prefer its HTTP server (long-lived, first-class abort, SSE). A uniform subprocess adapter for OpenCode via `opencode run --format json --auto` is possible but gives weaker cancel/permission control.
2. **Unattended lock-down default**: translate "no human" into harness-native deny-by-default: Codex `--sandbox workspace-write --ask-for-approval never`; Claude `--permission-mode dontAsk` + allowlist (never bare `bypassPermissions`); Grok `--always-approve` is all-or-nothing, so pair it with `--disallowed-tools`/`--deny` and run it in a container; OpenCode permission map with `"*": "deny"` + explicit allows, or `--auto` in a container.
3. **Exit classification**: harnesses are inconsistent (Claude/Grok document SIGTERM=143/SIGINT=130; Codex/OpenCode undocumented). Classify on (a) parsed terminal event (Grok `end.stopReason`, Claude `result.is_error`/`subtype`, Codex `turn.failed`/`error` event), (b) exit code, (c) timeout/budget/turn-limit markers (`--max-turns` reached errors in Claude; Grok `max_turns_reached`; enforce `max_turns`/`budget_usd` in the driver for Codex/OpenCode).
4. **Changed-files candidates**: no harness emits a reliable changed-file list end-to-end (Grok `tool_call` file_change items and OpenCode `file.edited`/`session.diff` events are closest; Codex `item.completed` file-change items; Claude has no file-diff event — use `enable_file_checkpointing`/rewind or a git-diff snapshot). Recommended: driver records cwd → run → `git status --porcelain` / `git diff` before/after; treat harness file events only as hints.
5. **Budget enforcement**: native only in Claude (`--max-budget-usd`) and Grok (`costUSD` fields to check against). Driver-side enforcement: watch `usage` events, cancel (kill / abort endpoint) on breach.
6. **Settings isolation**: Claude `--setting-sources ''` or `--bare`; Codex `--ignore-user-config --ignore-rules --ephemeral`; Grok `--no-auto-update` (+ clean `HOME`/`GROK_HOME`); OpenCode `OPENCODE_CONFIG_CONTENT` inline config with explicit providers. All four support pinning a model string — normalize as `provider/model` where possible (OpenCode native; Grok/OpenAI/Anthropic single-provider).
7. **Auth isolation in the driver**: each driver receives credentials via env only (`CODEX_API_KEY`/`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, provider keys for OpenCode); never reuse interactive login stores (`~/.codex/auth.json`, `~/.grok/auth.json`, claude.ai OAuth) in unattended runs — both because of ToS (Anthropic explicitly; OpenAI warns for public repos) and reproducibility.
8. **CI containers**: no harness except Codex (via `codex-action` safety strategies) ships an official container image; the common pattern is: disposable/rootless container, pinned binary version, API-key-only auth, network egress limited to the model API, `--no-auto-update`-style flags (Grok) / `--ephemeral` (Codex) / clean `HOME` (all). Anthropic's devcontainer (firewalled, rootless) is the most complete official reference.

---

## 6. ACP (Agent Client Protocol)

- **Status**: actively versioned spec at agentclientprotocol.com; both `/protocol/v1/schema` and `/protocol/v2/schema` are published — v1 is the stable baseline, v2 in progress (**[uncertain]** which is marked current; check the site banner). Docs use `/protocol/v1/...` paths for narrative pages.
- **Transport/model**: JSON-RPC 2.0 between a Client (editor/harness) and an Agent run "typically as a subprocess" (stdio in practice); camelCase JSON keys, snake_case discriminators, absolute paths, 1-based lines.
- **What it standardizes**: `initialize` (version + capability negotiation), `authenticate`/`logout` (auth methods, e.g. xAI's `xai.api_key`/`cached_token`); session lifecycle `session/new`, `session/load` (optional capability), `session/prompt`, `session/cancel` (notification), `session/set_mode`; tool calls surfaced as `session/update` notifications (message chunks, tool-call create/update, plans, mode changes); client-side `session/request_permission` (agent asks client for tool approval — the standardized permission loop), `elicitation/create|complete` (structured user input), optional client FS/terminal methods (`fs/read_text_file`, `fs/write_text_file`, `terminal/*`); agent plans via "Agent Plan" page. Extensibility via `_meta` and underscore methods.
- **Support**: broad agent adoption per the official registry page: Gemini CLI, OpenCode, Cursor, Cline, Goose, Qwen Code, OpenHands, Factory Droid, Kimi CLI, Kiro CLI, GitHub Copilot (preview), JetBrains Junie, and adapters for **Codex CLI** ("ACP's adapter") and Claude ("Claude Agent" via Zed's SDK adapter). **Grok Build supports ACP natively** (`grok agent stdio` is an official usage mode). Clients: Zed and others; an ACP Registry exists for discovery. Libraries: official TypeScript, Python, Rust, Java, Kotlin.
- **Viability as a future common transport**: ACP covers exactly the HarnessDriver hard parts that raw CLI modes do poorly — normalized permission requests (`session/request_permission`), first-class `session/cancel`, session load/new, streamed deltas. Grok (native) and OpenCode/Gemini/Codex/Claude (native or adapter) can all sit behind it. Caveats: adapter maturity varies per agent, capability negotiation means feature sets differ (e.g. `session/load` optional), usage/token reporting is **not** a standardized first-class structure (Grok stuffs it into `session/prompt` "completion metadata" — agent-specific), and structured output is absent. Recommendation: build the v1 driver on native CLI/HTTP interfaces (precise flags/usage documented above), keep event normalization ACP-shaped, and treat ACP as the convergence target once per-agent support matures.

Sources: agentclientprotocol.com/protocol/overview; /protocol/v1/schema; /get-started/agents; /get-started/clients; /libraries/python; docs.x.ai/build/cli/headless-scripting (grok agent stdio examples).

---

## Open questions / to verify against binaries
1. Codex: current `--ask-for-approval` value set (`untrusted`/`on-failure` gone?); exit-code table; existence of a patch-file output in `codex-action@v1`; whether `item.updated` fires in `exec --json`.
2. Claude: exact `canUseTool` argument shape (tool_name/input) and TS `permissionPrompts:'none'` semantics; `--setting-sources ''` vs `--bare` interaction; per-model cost breakdown field names in `total_cost_usd`.
3. Grok: repo license; full `--permission-mode` enumeration; `--sandbox <PROFILE>` profile names.
4. OpenCode: exact SSE event-name strings wire-side (camelCase `message.part.updated` etc.) and permission reply payload; message-info usage field names; license.
5. ACP: which schema version is declared current; whether token usage becomes standardized.
