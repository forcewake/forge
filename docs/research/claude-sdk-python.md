# Claude Agent SDK for Python — Adapter Research

> Research date: 2026-09-21. **Every API shape in §§2–5, 10–11 was verified against the actual
> `claude-agent-sdk==0.2.157` wheel** (downloaded and inspected: `types.py`, `client.py`,
> `query.py`, `_errors.py`, `__init__.py`). Doc-site prose came from the official reference
> (code.claude.com/docs/en/agent-sdk/python, mirrored at docs.claude.com / docs.anthropic.com /
> platform.claude.com), PyPI, GitHub (anthropics/claude-agent-sdk-python), and production
> third-party write-ups (claude-mem, dd-agents). Where doc summaries and the package disagreed,
> **the package won** and the discrepancy is noted.
>
> Audience: the coding agent that will write the real adapter.

---

## 1. Package identity

| Item | Value (verified) |
|---|---|
| PyPI name | **`claude-agent-sdk`** (current), formerly `claude-code-sdk` (renamed Sept 2025 at v0.1.0) |
| Import path | **`claude_agent_sdk`** |
| Version used for verification | **0.2.157** (latest on PyPI 2026-09-21) |
| Bundled Claude Code CLI | **2.1.277** (`claude_agent_sdk/_cli_version.py`) — platform wheels bundle the CLI; no Node/Claude install needed. Override with `cli_path`. |
| Python | 3.10+ |
| Runtime deps | anyio, mcp, jsonschema, typing-extensions |
| Async runtime | anyio-based (asyncio or trio). Async-first API. |
| License | Code is MIT; usage governed by Anthropic Commercial Terms of Service |

**Do not use the legacy `claude-code-sdk` package** (import `claude_code_sdk`): its
`ClaudeCodeOptions` was renamed to `ClaudeAgentOptions` in v0.1.0 with breaking field changes.
Target `claude-agent-sdk>=0.2.118` minimum (adds `ResultMessage.terminal_reason`, needed for
reliable interrupt detection — §7); 0.2.157 is current.

### Architecture (mental model)

The SDK is **not** an HTTP client. It spawns the Claude Code CLI as a **subprocess** and speaks
newline-delimited JSON over stdin/stdout (`--input-format stream-json --output-format stream-json`),
plus a **control protocol** (`control_request`/`control_response` frames on the same stream) used
for interrupt, set_model, permission callbacks, rewind_files, MCP status, etc. Default transport:
`SubprocessCLITransport`; `WebSocketTransport` and `HTTPTransport` also exist
(`claude_agent_sdk/transport/{subprocess_cli,websocket,http}.py`). A custom transport can be
injected via the `transport` parameter of both `query()` and `ClaudeSDKClient` — this is the SDK's
official extension seam for testing and for remote CLI hosts.

Key top-level exports (verified in `__init__.py`; `__all__` has ~130 names):

- `query`, `ClaudeSDKClient`, `ClaudeAgentOptions`, `Transport`
- Messages: `AssistantMessage`, `UserMessage`, `SystemMessage`, `ResultMessage`, `StreamEvent`,
  `RateLimitEvent`, `ConversationResetMessage`, `Message` (the union)
- Content blocks: `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`,
  `ServerToolUseBlock`, `ContentBlock` (union)
- Errors: `ClaudeSDKError`, `CLINotFoundError`, `CLIConnectionError`, `ProcessError`,
  `ResultError`, `CLIJSONDecodeError`, `MessageParseError`
- MCP tooling: `tool` (decorator), `create_sdk_mcp_server`
- Session utilities: `list_sessions`, `get_session_messages`, `get_session_info`,
  `rename_session`, `tag_session`, `fork_session`, `delete_session`, `list_subagents`,
  `get_subagent_messages`, `InMemorySessionStore`, `SessionStore`, `SessionKey`
- Hooks/permissions: `HookMatcher`, `CanUseTool`, `PermissionResultAllow`, `PermissionResultDeny`,
  `ToolPermissionContext`, `PermissionMode`

---

## 2. The two entry points

| Feature | `query()` | `ClaudeSDKClient` |
|---|---|---|
| Session | New session per call (unless `resume`/`continue_conversation`) | Reuses the same session across `.query()` calls |
| Connection | Managed automatically | Manual `connect()`/`disconnect()` or async context manager |
| Interrupts | **Not supported** | **Supported** (`interrupt()`) |
| Hooks / custom tools | Supported | Supported |
| Mid-session setters | N/A | `set_model()`, `set_permission_mode()` |
| Use case | One-off tasks | Chat/interactive; anything needing interrupt or steer |

### 2.1 `query()` — one-shot (verified signature, note keyword-only)

```python
async def query(
    *,
    prompt: str | AsyncIterable[dict[str, Any]],
    options: ClaudeAgentOptions | None = None,
    transport: Transport | None = None,
) -> AsyncIterator[Message]
```

- `prompt`: plain string, or an async iterable of SDK message dicts for **streaming-input mode**
  (push follow-up turns into the same session while it runs).
- **All parameters are keyword-only.**
- **Single-shot error semantics**: after yielding a result with `is_error=True` (e.g.
  `error_max_turns`), the iterator raises (`ResultError`). Wrap the loop in try/except — the
  result message was already yielded, so `session_id` is still capturable.

### 2.2 `ClaudeSDKClient` — interactive (wrap THIS in the adapter)

```python
class ClaudeSDKClient:
    def __init__(
        self,
        options: ClaudeAgentOptions | None = None,   # defaults to ClaudeAgentOptions()
        transport: Transport | None = None,          # defaults to SubprocessCLITransport
    ): ...
```

- `async with ClaudeSDKClient(options) as client:` — `__aenter__` calls `connect()` with no
  prompt; `__aexit__` calls `disconnect()` (returns `False`, does not suppress exceptions).
- `connect()` may raise `CLINotFoundError`, `CLIConnectionError`, `ProcessError`.
- Holds a persistent anyio task group for reading messages from `connect()` to `disconnect()`.
- If `resume` + `session_store` are set, `connect()` materializes the stored transcript into a
  temp `CLAUDE_CONFIG_DIR` for the subprocess (skipped when a custom transport is supplied).

---

## 3. `ClaudeSDKClient` — full method surface (verified against client.py)

### Session lifecycle

```python
async def connect(self, prompt: str | AsyncIterable[dict[str, Any]] | None = None) -> None
async def disconnect(self) -> None
```
`prompt=None` connects without sending anything (interactive use). Disconnect tears down the
subprocess and read task; call it on every exit path (context manager does this for you).

### Sending / steering

```python
async def query(self, prompt: str | AsyncIterable[dict[str, Any]], session_id: str = "default") -> None
```
Sends a user turn **in streaming mode**. String prompts are wrapped into
`{"type":"user","message":{"role":"user","content":prompt},"parent_tool_use_id":None,
"session_id":session_id}` and written to stdin; an async iterable is streamed item by item.
The `session_id` param here is an **internal stream label** (default `"default"`), NOT the Claude
Code session UUID and NOT `ClaudeAgentOptions.resume` — don't confuse them. The CLI session is
continued automatically across `query()` calls on the same client. Raises `CLIConnectionError`
("Not connected. Call connect() first.") if used before `connect()`.

Steering mid-turn: feed `query()` an async iterable that yields a follow-up user message while the
turn runs, or simpler: `interrupt()` then `query()` with the new instruction (drain first — §7.3).

### Receiving events

```python
async def receive_messages(self) -> AsyncIterator[Message]   # yields indefinitely until disconnect
async def receive_response(self) -> AsyncIterator[Message]   # yields until and INCLUDING the next ResultMessage
```
- `receive_response()` terminates right after yielding the turn's `ResultMessage`.
- Single consumer at a time; the background read task fills the internal queue.
- Task* system messages for background tasks started in earlier turns are suppressed here
  (turn-tracking hygiene inside the client).

### Interrupt

```python
async def interrupt(self) -> None
```
Sends the `interrupt` control request; aborts the current turn (in-flight tool calls and/or
streaming). Awaits the CLI's control ack. The aborted turn still produces a `ResultMessage`
(§7). Only works in streaming mode (which `ClaudeSDKClient` always uses).

### Mid-session setters

```python
async def set_permission_mode(self, mode: PermissionMode) -> None
async def set_model(self, model: str | None = None) -> None    # None -> CLI default model
```
`PermissionMode = Literal["default","acceptEdits","plan","bypassPermissions","dontAsk","auto"]`
— note `"dontAsk"` (deny anything not pre-approved) and `"auto"` (model classifier approves/denies)
exist beyond the four the older docs mention. Model switch invalidates prompt cache for the next
request (recomputed at the new model's rates).

### Checkpoint / restore

```python
async def rewind_files(self, user_message_id: str) -> None
```
Control request `{"subtype":"rewind_files","user_message_id":...}`. Restores tracked files to
their state at that user message; **conversation is NOT rewound**. `user_message_id` = the `uuid`
of a `UserMessage` seen in the stream. Requires `enable_file_checkpointing=True` AND
`extra_args={"replay-user-messages": None}` (§8).

### Introspection / tasks / MCP

```python
async def get_mcp_status(self) -> McpStatusResponse          # {"mcpServers":[{"name","status","tools",...}]}
async def reconnect_mcp_server(self, server_name: str) -> None
async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None
async def stop_task(self, task_id: str) -> None              # -> task_notification status "stopped"
async def get_context_usage(self) -> ContextUsageResponse    # context window utilization (newer SDKs)
async def get_server_info(self) -> dict[str, Any] | None     # commands/output styles; None if not streaming
```

---

## 4. `ClaudeAgentOptions` — verified field list (v0.2.157)

Single dataclass for both entry points; all fields optional. Defaults verified by importing the
package. (Doc-site summaries list a few fields that do NOT exist in 0.2.157 — flagged below.)

| Field | Type / default | Notes |
|---|---|---|
| `tools` | `list[str] \| ToolsPreset \| None = None` | Which built-in tools exist. `[]` disables all; `{"type":"preset","preset":"claude_code"}` = full set. |
| `allowed_tools` | `list[str] = []` | Auto-approve (permission allowlist). Does NOT remove tools from the toolset. `"Skill"` here is deprecated → use `skills`. |
| `disallowed_tools` | `list[str] = []` | Block tools. |
| `system_prompt` | `str \| SystemPromptPreset \| SystemPromptCustom \| SystemPromptFile \| None` | Plain str = full replacement; `{"type":"preset","preset":"claude_code","append":"..."}` layers onto the Claude Code prompt; `{"type":"custom","prompt":...}` can set a snapshot. |
| `mcp_servers` | `dict[str, McpServerConfig] \| str \| Path = {}` | Mixed: in-process SDK servers, stdio configs, HTTP/SSE configs, or a path to a config file. |
| `strict_mcp_config` | `bool = False` | Ignore filesystem MCP config. |
| `permission_mode` | `PermissionMode \| None` | See the 6 modes in §3. |
| `continue_conversation` | `bool = False` | Resume most recent session in `cwd`. Mutually exclusive with `resume`. |
| `resume` | `str \| None` | Session ID to resume. |
| `session_id` | `str \| None` | **Use a specific UUID for a NEW conversation** (not resume). Must be a valid UUID; cannot combine with `continue_conversation`/`resume` unless `fork_session` is set. Useful for adapters that mint their own session ids. |
| `max_turns` | `int \| None` | `0` behaves as unset. |
| `max_budget_usd` | `float \| None` | `0` rejected by CLI at startup. |
| `model` / `fallback_model` | `str \| None` | Alias or full name (`"sonnet"`, `"claude-sonnet-4-5"`...). Fallback retried-primary each user turn. |
| `betas` | `list[SdkBeta] = []` | Only `"context-1m-2025-08-07"` currently. |
| `permission_prompt_tool_name` | `str \| None` | Auto-set to `"stdio"` when `can_use_tool` is provided. |
| `cwd` | `str \| Path \| None` | Working dir; no mid-session setter. |
| `cli_path` | `str \| Path \| None` | Custom CLI executable (overrides bundled). |
| `settings` | `str \| None` | Settings file path or inline JSON string; overrides filesystem settings. |
| `add_dirs` | `list[str \| Path] = []` | Extra file-access scopes. |
| `env` | `dict[str, str] = {}` | **Merged OVER inherited env** (Python semantics; TS replaces). |
| `extra_args` | `dict[str, str \| None] = {}` | Arbitrary CLI flags, e.g. `{"replay-user-messages": None}`. |
| `max_buffer_size` | `int \| None` | Cap on CLI stdout buffering. |
| `debug_stderr` | `Any = sys.stderr` | Raw stderr sink (defaults to passing through to process stderr). |
| `stderr` | `Callable[[str], None] \| None` | Per-line stderr callback — wire to adapter logs. |
| `can_use_tool` | `CanUseTool \| None` | Async approval callback `(tool_name, input, context) -> PermissionResult`. |
| `hooks` | `dict[HookEvent, list[HookMatcher]] \| None` | Python-function hooks (PreToolUse, Stop, ...). |
| `user` | `str \| None` | User identifier. |
| `include_partial_messages` | `bool = False` | **Must be True for `StreamEvent` deltas.** |
| `include_hook_events` | `bool = False` | Emit HookEventMessages. |
| `forward_subagent_text` | `bool = False` | Surface subagent text. |
| `fork_session` | `bool = False` | With `resume`: branch into a new session id; original untouched. |
| `resume_session_at` | `str \| None` | Truncate resumed history at this message UUID. |
| `resume_drops_turn` | `str \| None` | Drop a partial turn when resuming a mid-turn session. |
| `agents` | `dict[str, AgentDefinition] \| None` | Programmatic subagents (`description`, `prompt`, `tools`, `model`). |
| `setting_sources` | `list[SettingSource] \| None` | `None` = isolated (SDK default loads NOTHING from disk); `["user","project","local"]` = CLI-like. |
| `skills` | `list[str] \| Literal["all"] \| None` | Skill loading. |
| `sandbox` | `SandboxSettings \| None` | Bash sandboxing (macOS/Linux). |
| `plugins` | `list[SdkPluginConfig] = []` | Plugins. |
| `max_thinking_tokens` | `int \| None` | Deprecated → `thinking`. |
| `thinking` | `ThinkingConfig \| None` | `{"type":"adaptive"}` / `{"type":"enabled","budget_tokens":N}` / `{"type":"disabled"}`. |
| `effort` | `EffortLevel \| None` | `'low'|'medium'|'high'|'max'`. |
| `output_format` | `dict[str, Any] \| None` | Structured outputs (`{"type":"json_schema","schema":{...}}`) → `ResultMessage.structured_output`. |
| `enable_file_checkpointing` | `bool = False` | §8. |
| `session_store` | `SessionStore \| None` | External persistence (§6.3). |
| `session_store_flush` | `SessionStoreFlushMode = "batched"` | Store flush mode. |
| `load_timeout_ms` | `int = 60_000` | Timeout loading a session from store at connect. |
| `task_budget` | `TaskBudget \| None` | Budget for background tasks. |

**Fields that doc-site summaries mention but that DO NOT exist in 0.2.157** (do not code against
them): `permission_rules`, `disallowed_permission_rules`, `additional_directories` (use
`add_dirs`), `env_default`, `exact_path`, `id`, `program`, `agent_definition`,
`initial_message_count`, `persist_session`, `include_plan`, `max_thinks`, `append_system_prompt`.
If you need one of these, re-check the installed version's `dataclasses.fields(ClaudeAgentOptions)`.

There are **no** `temperature`/`top_p`/`max_tokens` options — clamp output via the
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` env var (§9).

---

## 5. Streaming event shapes (verified dataclasses)

```python
Message = (UserMessage | AssistantMessage | SystemMessage | ResultMessage
           | StreamEvent | RateLimitEvent | ConversationResetMessage)
```
Task messages (`TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage`,
`TaskUpdatedMessage`) are **dataclass SUBCLASSES of `SystemMessage`** — an `isinstance(msg,
SystemMessage)` check still matches them; match the subclasses first if you branch on type.

### `AssistantMessage` — one per assistant API response
```python
@dataclass
class AssistantMessage:
    content: list[ContentBlock]                 # DIRECT: iterate msg.content
    model: str                                  # e.g. "claude-sonnet-4-5-20250929"
    parent_tool_use_id: str | None = None       # set when emitted by a subagent/tool
    error: AssistantMessageError | None = None
    usage: dict[str, Any] | None = None         # per-API-call usage
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None
```
Content blocks (isinstance-check, then attribute access):
```python
@dataclass class TextBlock:         text: str
@dataclass class ThinkingBlock:     thinking: str; signature: str
@dataclass class ToolUseBlock:      id: str; name: str; input: dict[str, Any]
@dataclass class ToolResultBlock:   tool_use_id: str; content: str | list[dict] | None; is_error: bool | None
@dataclass class ServerToolUseBlock: id; name (web_search, web_fetch, advisor, code_execution...); input
```
Tool results arrive inside **`UserMessage.content`** as `ToolResultBlock` items (synthetic user
turns) — also `UserMessage.tool_use_result`. Note `ToolResultBlock.tool_use_id` (not `id`).

### `UserMessage`
```python
@dataclass
class UserMessage:
    content: str | list[ContentBlock]        # DIRECT: msg.content
    uuid: str | None = None                  # ONLY with extra_args={"replay-user-messages": None}; = checkpoint id
    parent_tool_use_id: str | None = None
    tool_use_result: dict[str, Any] | None = None
    origin: MessageOrigin | None = None      # provenance (human vs injected turn)
```

### `SystemMessage`
```python
@dataclass
class SystemMessage:
    subtype: str          # "status" | "stream" | "control_response" | "result" | task subtypes
    data: dict[str, Any]  # raw payload; init session id is nested here in Python
```

### `ResultMessage` — end of turn; the adapter's most important frame
```python
@dataclass
class ResultMessage:
    subtype: str                     # "success" | "error_max_turns" | "error_max_budget_usd" | "error_during_execution"
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str                  # the Claude Code session UUID — persist every turn
    stop_reason: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None     # input_tokens, output_tokens, cache_* tokens...
    result: str | None               # final text
    structured_output: Any = None    # when output_format was set
    model_usage: dict[str, ModelUsage] | None   # per-model cost/token/turn breakdown incl. provider
    permission_denials: list[Any] | None = None
    deferred_tool_use: DeferredToolUse | None = None
    errors: list[str] | None = None
    api_error_status: int | None = None   # HTTP 429/500/529 of failing call (CLI >= 2.1.110); log-safe
    uuid: str | None = None
    terminal_reason: str | None = None    # v0.2.118+; see §7
    origin: MessageOrigin | None = None
```
(There is no `cost_usd` turn field — use `total_cost_usd`. Older doc text mentioning
`modelUsage`/`usage_delta` camelCase fields is TypeScript or outdated; Python is `model_usage` etc.)

### `StreamEvent` — raw Anthropic stream deltas (needs `include_partial_messages=True`)
```python
@dataclass
class StreamEvent:
    uuid: str
    session_id: str
    event: dict[str, Any]            # raw Messages-API frame: message_start/message_delta/message_stop/
                                     # content_block_start/content_block_delta/content_block_stop
    parent_tool_use_id: str | None = None
```
`event["type"]=="content_block_delta"` → `event["delta"]` is `{"type":"text_delta","text":...}` or
`{"type":"input_json_delta","partial_json":...}` (incremental tool-call JSON the adapter must
assemble if it consumes it).

### Rate-limit / reset
```python
@dataclass class RateLimitEvent:    rate_limit_info: RateLimitInfo; uuid: str; session_id: str
# RateLimitInfo: status ("allowed"|"allowed_warning"|"rejected"), resets_at, rate_limit_type,
#                utilization (0..1), overage_* fields, raw dict
@dataclass class ConversationResetMessage: ...   # conversation replaced without disconnect (/clear);
                                                 # subsequent ResultMessage totals are re-zeroed
```

### Background-task messages (SystemMessage subclasses; CLI >= 2.1.79)
```python
TaskStartedMessage(task_id, description, uuid, session_id, tool_use_id?, task_type?)
TaskProgressMessage(task_id, description, usage: TaskUsage, uuid, session_id, tool_use_id?, last_tool_name?)
TaskNotificationMessage(task_id, status: "completed"|"failed"|"stopped", output_file, summary, uuid, session_id, tool_use_id?, usage?)
TaskUpdatedMessage(task_id, patch: dict, ...)   # patch["status"] terminal per TERMINAL_TASK_STATUSES
```
Clear active task ids on a terminal status from EITHER TaskNotificationMessage or TaskUpdatedMessage.

### Error taxonomy (verified `_errors.py`)
```python
ClaudeSDKError                      # base
├── CLIConnectionError
│   └── CLINotFoundError            # subclass of CLIConnectionError!
├── ProcessError                    # .exit_code, message includes stderr
│   └── ResultError                 # CLI error result surfaced as exception
├── CLIJSONDecodeError
└── MessageParseError
```
(No separate `MaxTurnsError`/`UnpromptedErrorMessageError` classes in 0.2.157 — max-turns
surfaces as `ResultError`/error-result in single-shot mode.) Control requests time out after 60s
(`load_timeout_ms`-style `anyio.fail_after`) raising a plain `Exception("Control request timeout:
<subtype>")`.

---

## 6. Session persistence & resumption

### 6.1 Where sessions live
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (cwd with non-alphanumerics → `-`;
`CLAUDE_CONFIG_DIR` relocates the root; names >200 chars truncated + hashed). Sessions are
machine-local; cross-host resume needs a store or file transfer.

### 6.2 Options (on `ClaudeAgentOptions`)
- Capture the id: `ResultMessage.session_id` on every turn (also in the init `SystemMessage.data`
  in Python). `ClaudeSDKClient` auto-continues its session across `.query()` calls in-process.
- `resume="<session-id>"` — resume a specific session (works from any cwd on CLI >= 2.1.223;
  bundled 2.1.277 qualifies).
- `continue_conversation=True` — most recent session in `cwd` (no id needed).
- `fork_session=True` + `resume` — branch into a NEW session id; original untouched.
- `session_id="<uuid>"` — pre-assign the id of a new conversation (adapter-friendly).
- Resume-after-limit: a run that ended `error_max_turns` / `error_max_budget_usd` can be resumed
  with a higher cap (catch the raised `ResultError` first in single-shot mode).
- Module-level synchronous utilities: `list_sessions()`, `get_session_messages()`,
  `get_session_info()`, `rename_session()`, `tag_session()`, `fork_session()`,
  `delete_session()`, `list_subagents()`, `get_subagent_messages()` — each also has a
  `..._via_store()` variant that takes a `SessionStore`.

### 6.3 `session_store` — external persistence (cross-host)
Pass an object implementing the `SessionStore` protocol (TypedDict-based, async methods; entries
are JSON-safe dicts, one per JSONL line; `SessionKey = {"project_key","session_id","subpath"?}`,
project_key derives from cwd):

```python
class SessionStore:
    async def append(self, key, entries): ...            # required — after each local write batch
    async def load(self, key) -> list[SessionStoreEntry] | None: ...   # required — before spawn when resume set
    # optional: list_sessions, list_session_summaries, delete, list_subkeys
```
On `connect()`, the SDK loads the transcript and **materializes it into a temp
`CLAUDE_CONFIG_DIR`** for the subprocess (skipped when a custom transport is supplied — relevant
if the adapter injects one). `InMemorySessionStore` ships in the package for dev/test.
`session_store_flush="batched"` (default) controls flush cadence; `load_timeout_ms=60_000`
bounds the load.

For ephemeral sessions in Python, set `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` in `env` (there is no
`persist_session` field in the Python 0.2.157 package).

---

## 7. Interruption — semantics and gotchas (critical for the adapter)

1. **Only `ClaudeSDKClient.interrupt()` exists.** `query()` cannot interrupt.
2. After `interrupt()` the turn **still emits a `ResultMessage`** — typically `subtype="success"`
   with `terminal_reason` of `"aborted_streaming"` or `"aborted_tools"` (SDK >= 0.2.118 /
   CLI >= 2.1.37; bundled 2.1.277 is fine). Map those two values to "cancelled"; `None` on older
   stacks or results that bypassed the query loop (e.g. local slash command).
3. **Drain before re-querying (documented buffering gotcha):** `interrupt()` followed immediately
   by `query()` can queue the new prompt without executing it. Correct pattern: after
   `interrupt()`, consume `receive_messages()`/`receive_response()` until the `ResultMessage`
   arrives, THEN send the next `query()`.
4. `interrupt()` awaits the CLI's control ack (60s internal timeout). Known issue
   ([#1094](https://github.com/anthropics/claude-agent-sdk-python/issues/1094), fixed via PR
   #1095 mid-2026): `disconnect()` while an interrupt is pending used to leave the await hanging
   for the full 60s; fixed in recent 0.2.x — another reason to pin >= 0.2.118.
5. `interrupt()` aborts the turn, not the session — client stays connected, history intact.
6. Related: `stop_task(task_id)` for background tasks; `disconnect()` kills the subprocess
   outright (hard stop).

---

## 8. Checkpointing / restore

- Enable: `ClaudeAgentOptions(enable_file_checkpointing=True,
  extra_args={"replay-user-messages": None})` — the extra arg makes `UserMessage.uuid` appear in
  the stream; that uuid IS the checkpoint id.
- Restore: `await client.rewind_files(user_message_id)`. Files created during the session are
  deleted; modified files restored to checkpoint-time content. **Conversation is not rewound.**
  Symlinks/hard links/moved parents are skipped.
- Only `Write`, `Edit`, `NotebookEdit` tool changes are tracked — NOT Bash-written files.
- Cross-process restore (documented pattern): resume the session, open a turn with an empty
  prompt, rewind on first message:
  ```python
  async with ClaudeSDKClient(ClaudeAgentOptions(enable_file_checkpointing=True, resume=session_id)) as client:
      await client.query("")
      async for message in client.receive_response():
          await client.rewind_files(checkpoint_id)
          break
  ```
- CLI equivalent for ops tooling: `claude --resume <session-id> --rewind-files <checkpoint-uuid>`.
- Env `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING=1` disables globally; retention ~30 days.
- **"Checkpoint export" (shipping a snapshot) is NOT an SDK API** — the SDK only rewinds. If the
  adapter needs exportable snapshots, persist transcripts via `session_store` and treat
  checkpoint uuids as restore points.

---

## 9. Model / provider / credentials configuration

### 9.1 Options-level
`model`, `fallback_model`, `set_model()` mid-session, `effort`, `thinking`. No sampling params.

### 9.2 Environment (via `ClaudeAgentOptions.env` — merged over inherited env in Python)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic key; sent as `x-api-key`. |
| `ANTHROPIC_AUTH_TOKEN` | Bearer token — **use for gateways** (e.g. LiteLLM virtual key). |
| `ANTHROPIC_BASE_URL` | Any Anthropic-Messages-protocol endpoint (LiteLLM, corporate proxy...). |
| `ANTHROPIC_CUSTOM_HEADERS` | Extra inference headers. |
| `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL` | Model-name overrides (gateway knows different names). |
| `CLAUDE_CODE_USE_BEDROCK=1` / `CLAUDE_CODE_USE_VERTEX=1` | Route to Bedrock / Vertex (+ region/cred vars). |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Output clamp — required when the backing model caps lower than Claude. |
| `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` | Suppress transcript writes (ephemeral sessions, Python). |
| `CLAUDE_CONFIG_DIR` | Relocate `~/.claude`. |
| Watchdogs | `API_TIMEOUT_MS`, `CLAUDE_CODE_MAX_RETRIES`, `CLAUDE_ENABLE_STREAM_WATCHDOG` +
  `CLAUDE_STREAM_IDLE_TIMEOUT_MS` (default 300000, on by default), `CLAUDE_CODE_RETRY_WATCHDOG=1`
  for indefinite capacity retries. |

Official gateway pattern:
```python
options = ClaudeAgentOptions(env={"ANTHROPIC_BASE_URL": "https://my-gateway.example.com"})
# Python MERGES over inherited env — ambient credentials survive; explicit keys here win on collision.
```

### 9.3 Gateway specifics (official + production sources)
- **Protocol**: the gateway must expose the Anthropic Messages wire format (`/v1/messages`, SSE
  streaming), not OpenAI format. LiteLLM's unified endpoint is the common choice.
- **Model discovery**: with a non-Anthropic `ANTHROPIC_BASE_URL`, Claude Code calls `GET
  /v1/models` and adds ids starting with `claude`/`anthropic` to the picker (cached at
  `~/.claude/cache/gateway-models.json`); auth reuses `ANTHROPIC_AUTH_TOKEN` (bearer) or
  `ANTHROPIC_API_KEY` (x-api-key) + `ANTHROPIC_CUSTOM_HEADERS`. Bedrock/Vertex endpoints do not
  trigger discovery.
- **Placeholder API key**: several production integrations (claude-mem, NanoClaw — third-party
  reported) set BOTH `ANTHROPIC_AUTH_TOKEN` (real gateway token) and a placeholder
  `ANTHROPIC_API_KEY`, because some CLI builds refuse to spawn without the latter. If a gateway
  setup 401s or fails to start, try adding the placeholder.
- **No model-name translation**: the model string must be one the gateway recognizes (map via
  `ANTHROPIC_MODEL` or `model=`).
- Token-limit 400s behind a gateway: clamp `CLAUDE_CODE_MAX_OUTPUT_TOKENS`.

---

## 10. Known limitations & gotchas (adapter checklist)

1. **Async-context stickiness** (SDK caveat since v0.0.20): a `ClaudeSDKClient` cannot cross async
   runtime contexts (different anyio task groups/nurseries). Connect, use, disconnect within one
   async context; own the client inside one task and expose queues/events at the adapter boundary.
2. **Never abandon the iterator**: breaking out of `async for` without `disconnect()` leaks the
   subprocess. Always use `async with`.
3. **`receive_response()` never ends if no `ResultMessage` arrives** — race it with a timeout
   task; on expiry call `interrupt()` then `disconnect()`.
4. **Single-shot `query()` raises after an error result** — catch; `session_id` was already yielded.
5. **Interrupt→query buffering** — drain to `ResultMessage` before the next prompt (§7.3).
6. **`query()` is keyword-only** (`query(prompt=..., options=...)`).
7. **`client.query(session_id=...)` is an internal stream label**, not resume.
8. **Doc drift**: several doc-page summaries list options/fields absent from the package
   (see the blacklist in §4) and omit newer ones (`strict_mcp_config`, `session_id`, `task_budget`,
   `get_context_usage`). Code against the installed package; assert critical fields at import time
   if defensive.
9. **Version floors**: `terminal_reason` >= 0.2.118; `env_default`-era fields appear only in
   doc previews. Bundled CLI 2.1.277 covers background-task messages (>= 2.1.79),
   `api_error_status` (>= 2.1.110), cross-directory resume (>= 2.1.223).
10. **Python `env` merges** — ambient `ANTHROPIC_API_KEY` survives unless explicitly overridden;
    decide your precedence explicitly in the adapter.
11. **`system_prompt` size**: prompts pass through subprocess argv; ~128KB argv limits on Linux
    apply to very large prompts.
12. **Settings isolation**: SDK loads NO filesystem settings by default (unlike the CLI). To load
    `~/.claude/settings.json` / project CLAUDE.md, pass `setting_sources=["user","project","local"]`.
13. **Message classes are dataclasses; config TypedDicts are dicts** — mixed access styles.
14. **Model switching invalidates prompt cache** — next request recomputes at the new model's rates.
15. **`max_budget_usd=0` crashes at startup**; `max_turns=0` means "no limit".
16. **Windows**: subprocess transport works; sandbox is macOS/Linux only.
17. **Crash recovery**: a mid-stream CLI crash raises `ProcessError`/`CLIConnectionError` and kills
    the turn; the client does not auto-restart. Recovery = new client with
    `resume=<last ResultMessage.session_id>`.

---

## 11. Minimal working examples (field access verified against 0.2.157)

### 11.1 One-shot query

```python
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock

async def main():
    options = ClaudeAgentOptions(
        cwd="/path/to/project",
        allowed_tools=["Read", "Glob", "Grep"],
        max_turns=10,
    )
    session_id = None
    try:
        async for message in query(prompt="Analyze the auth module", options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:            # DIRECT .content
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                print(f"\n[done: {message.subtype}, cost: ${message.total_cost_usd:.4f}]")
    except Exception as e:
        print(f"query raised after error result: {e}")
    print(f"session_id={session_id}")

asyncio.run(main())
```

### 11.2 Interactive client with interrupt (the adapter core pattern)

```python
import asyncio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, StreamEvent, TextBlock, UserMessage,
)

async def main():
    options = ClaudeAgentOptions(
        cwd="/path/to/project",
        permission_mode="acceptEdits",
        include_partial_messages=True,               # StreamEvent deltas
        enable_file_checkpointing=True,              # checkpoint support
        extra_args={"replay-user-messages": None},   # UserMessage.uuid = checkpoint ids
    )

    async with ClaudeSDKClient(options=options) as client:
        checkpoints: list[str] = []

        async def consume_turn() -> ResultMessage:
            result = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(block.text, end="", flush=True)
                elif isinstance(msg, StreamEvent):
                    pass  # raw deltas: msg.event["type"] == "content_block_delta" etc.
                elif isinstance(msg, UserMessage):
                    if msg.uuid:
                        checkpoints.append(msg.uuid)
                elif isinstance(msg, ResultMessage):
                    result = msg
            return result

        # Turn 1
        await client.query("Refactor the authentication module")
        result = await consume_turn()
        print(f"\nturn 1: {result.subtype} terminal={result.terminal_reason} session={result.session_id}")

        # Interrupt a long-running turn, then steer
        await client.query("Count from 1 to 100 slowly, running bash sleep 1 between each")
        await asyncio.sleep(2)
        await client.interrupt()
        aborted = await consume_turn()               # DRAIN to ResultMessage before re-querying
        assert aborted.terminal_reason in ("aborted_streaming", "aborted_tools")

        # Steer in the same session
        await client.query("Actually, just list the first 10 numbers.")
        await consume_turn()

        # Mid-session model switch
        await client.set_model("sonnet")

        # Rewind files to the first checkpoint (conversation unchanged)
        if checkpoints:
            await client.rewind_files(checkpoints[0])

asyncio.run(main())
```

### 11.3 Custom model gateway (BYOK)

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

options = ClaudeAgentOptions(
    model="claude-sonnet-4-5",                      # a name YOUR gateway recognizes
    cwd="/path/to/project",
    env={
        "ANTHROPIC_BASE_URL": "https://gateway.example.com",   # Anthropic-protocol endpoint
        "ANTHROPIC_AUTH_TOKEN": "sk-gateway-token",            # sent as Bearer
        "ANTHROPIC_API_KEY": "placeholder-if-cli-requires",    # some CLI builds demand presence
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192",               # clamp to backing model's cap
    },
)
# Python merges env over the inherited environment.
```

### 11.4 Resume / fork

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

async def run(prompt: str, **opts):
    sid = None
    async for m in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(m, ResultMessage):
            sid = m.session_id
    return sid

session_id = await run("Analyze the code")                        # capture
await run("Now implement it", resume=session_id)                  # resume
await run("Try the OAuth2 angle instead",
          resume=session_id, fork_session=True, max_turns=5)      # fork: original untouched
```

### 11.5 Custom transport seam (for tests / remote CLI)

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

transport = SubprocessCLITransport(prompt="hello", options=ClaudeAgentOptions(cwd="/tmp"))
client = ClaudeSDKClient(options=ClaudeAgentOptions(), transport=transport)
```
Implement the `Transport` protocol (`connect`, read_messages, write, close, ...) to fake the CLI
in adapter tests (this is what the SDK's own test suite does). Note: session-store materialization
is skipped when a custom transport is supplied.

---

## 12. Adapter design notes (synthesis)

- **Wrap `ClaudeSDKClient`, not `query()`** — interrupts, steering, `set_model`,
  `rewind_files`, and in-process session continuity only exist there.
- Lifecycle mapping: `connect()` (spawn) → `query(prompt)` (turn start) → `receive_response()`
  stream (AssistantMessage/TextBlock → token events; StreamEvent → deltas; ToolUseBlock → tool
  events; ResultMessage → turn-complete with usage/cost/session_id/terminal_reason) →
  `interrupt()` (cancel; confirm via `terminal_reason`) → `disconnect()`.
- Run the receive loop **inside one asyncio task that owns the client**; expose queues/async
  events to the rest of the adapter (async-context stickiness).
- Persist `ResultMessage.session_id` every turn; support `resume`/`fork_session` (and optionally a
  pre-assigned `session_id` UUID) on (re)connect to survive crashes (`ProcessError` recovery =
  new client with `resume=<last session_id>`).
- Gateways: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` via `options.env` (merged), model name
  pinned to what the gateway serves, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` clamped.
- Pin `claude-agent-sdk>=0.2.150` (floor with all fields in §4; ≥0.2.118 strictly needed for
  `terminal_reason`); tested against 0.2.157 / bundled CLI 2.1.277.
- Wire the `stderr=` callback into adapter logs; without it subprocess errors are invisible
  (raw fallback: `debug_stderr`).
- If you depend on options fields beyond this doc, assert them at import:
  `assert {f.name for f in dataclasses.fields(ClaudeAgentOptions)} >= {...}`.

## Source index

- Official Python reference: https://code.claude.com/docs/en/agent-sdk/python (mirrors:
  docs.claude.com / docs.anthropic.com / platform.claude.com `.../agent-sdk/python`)
- Sessions: https://code.claude.com/docs/en/agent-sdk/sessions — Checkpointing:
  https://code.claude.com/docs/en/agent-sdk/file-checkpointing — Config/model:
  https://code.claude.com/docs/en/agent-sdk/model-config — Session storage:
  https://code.claude.com/docs/en/agent-sdk/session-storage — Migration/rename:
  https://platform.claude.com/docs/en/agent-sdk/migration-guide
- Repo: https://github.com/anthropics/claude-agent-sdk-python (`src/claude_agent_sdk/client.py`,
  `types.py`, `_errors.py`; examples `streaming_mode.py`, `streaming_mode_ipython.py`)
- PyPI: https://pypi.org/project/claude-agent-sdk/ (0.2.157 verified 2026-09-21)
- Interrupt-hang issue: anthropics/claude-agent-sdk-python #1094 / PR #1095; terminal_reason
  commit 07b46c6
- Gateway production write-ups: claude-mem docs (LiteLLM / ANTHROPIC_BASE_URL / placeholder-key),
  dd-agents model-providers guide
- **Primary source for this document**: the `claude_agent_sdk-0.2.157-py3-none-macosx_11_0_arm64.whl`
  wheel, downloaded from PyPI and inspected directly (types.py / client.py / query.py /
  _errors.py / __init__.py / _cli_version.py)


---

## LIVE CONFIRMATION — 2026-09-21 (forge live smoke + e2e)

The wheel-verified shapes held against a real gateway end-to-end
(z.ai Anthropic-compatible route, claude-agent-sdk 0.2.157, claude CLI
2.1.273): interactive `ClaudeSDKClient`, session ids on the init
SystemMessage and every ResultMessage, `terminal_reason` on
completion, bounded interrupt, and drain-to-ResultMessage across
repeated `query()` calls. Two of OUR smoke's initial assumptions were
wrong, not the doc's: `asdict` content blocks carry NO `type` field (a
text block is a dict WITH a `text` key; thinking has `thinking`), and
stream/system messages serialize as `{"data", "subtype"}`. A real
implement-a-failing-test task completed green through the driver
(evidence: `docs/evaluation/2026-09-21-drivers/claude-e2e.json`).
