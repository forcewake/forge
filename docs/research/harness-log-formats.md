# Harness CI Job-Log Formats — Research (September 2026)

Purpose: make the forge CI job logs readable while an agent works live. Today the
GitHub-Actions dogfood lane floods the log with thousands of raw `stream-json`
lines; the GitLab lanes filter, but the filters have gaps (no tool outcomes,
unbounded thinking noise, one dead code path). This doc enumerates each driver's
machine-readable event surface, defines ONE line grammar shared by all filters,
and ends with a per-driver implementation checklist in Node `readline` terms.

Tags: **[documented]** = confirmed against official docs/repo sources listed at
the end of the section. **[inference]** = derived from observed output or
community sources; verify against the installed binary before freezing code.

---

## 0. Where the flood comes from (current forge state)

| Lane | Invocation | Filter | Result in the job log |
|---|---|---|---|
| GitLab claude | `ci/templates/claude-code.gitlab-ci.yml` — `claude -p … --output-format stream-json --verbose 2>&1 \| tee /tmp/claude-events.jsonl \| node /tmp/filter.mjs` | `ci/templates/claude-events-filter.mjs` | Compacted lines, but no tool outcomes, thinking unthrottled, `api_retry` path never fires (see §1.4) |
| GitLab grok | `ci/templates/grok.gitlab-ci.yml` — `grok … --output-format streaming-json … \| tee … \| node /tmp/filter.mjs` | `ci/templates/grok-events-filter.mjs` | Compacted lines; no tool outcome status, no turn separators |
| GitLab opencode | `ci/templates/opencode.gitlab-ci.yml` — `opencode run --auto` | none | Plain prose only; no tool trace, usage unknown |
| GitLab copilot | `ci/templates/copilot.gitlab-ci.yml` — `copilot -p …` | none | Response + "stats and decoration" **[documented]**, no tool trace |
| GitHub Actions (all drivers) | `src/forge/harness_entry.py:render_driver_script()` — `… \| tee -a .forge/events.jsonl` | **none** | Raw NDJSON floods the log — this is the dogfood regression |

The Actions lane is the root cause: `claude -p … --output-format stream-json
--verbose 2>&1 | tee -a .forge/events.jsonl` sends every NDJSON line straight
into the Actions log. With GLM behind the gateway, claude-code 2.1.x additionally
emits a `system/thinking_tokens` progress event repeatedly per turn
**[documented as observed; not in official docs]**, and full `tool_result`
payloads ride in `user` messages — hence thousands of raw lines per run.
`parse_usage()` in the same file reads the tee'd file afterwards, so any fix
must keep the tee in place (filter sits AFTER the tee, never before).

---

## 1. claude-code `stream-json` schema + suppression rules

### 1.1 Event enumeration

Invocation: `claude -p … --output-format stream-json --verbose` (NDJSON on
stdout; `--verbose` is required with `stream-json` **[documented]**).
`stream_event` partial deltas only appear with the extra
`--include-partial-messages` flag — forge does NOT set it **[documented]**.

| Event (`type` / `subtype`) | Useful fields | Line emitted | Notes |
|---|---|---|---|
| `system` / `init` | `model`, `session_id`, `tools`, `mcp_servers`, `permissionMode`, `claude_code_version` | header: `── claude-code · model <m> · session <id8> ──` | **[documented]** |
| `system` / `api_retry` | `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, `error` (category) | `⏳ API retry 2/5 · HTTP 429 · wait 2.0s` | **[documented]** — see §1.4 bug |
| `system` / `thinking_tokens` | `estimated_tokens` | SUPPRESSED; accumulate (§1.3) | **[documented-as-observed]** — appears in Aug 2026 stream-json runs; absent from official docs |
| `system` / other (`hook_*`, `plugin_install`, `permission_denied`, `compact_boundary`, rate-limit notices, future subtypes) | varies | SUPPRESSED (bump a `suppressed` counter) | allowlist posture: unknown subtypes must never print raw **[inference]** |
| `assistant` block `text` | `b.text` | `💬 <first 140 chars, newlines→spaces>` | **[documented]** |
| `assistant` block `thinking` | `b.thinking` | `💭 …` — one line per turn max (§1.3) | **[documented]** |
| `assistant` block `redacted_thinking` | — | suppressed (counter) | **[inference]** |
| `assistant` block `tool_use` | `b.id`, `b.name`, `b.input` | `🔧 <Tool> <hint ≤80>` (§1.2); record id→name for the result line | **[documented]** |
| `assistant` subagent messages | `parent_tool_use_id != null` | prefix `⟲ ` to the line | **[documented]** — subagent traffic is distinguishable; keep it visible but marked |
| `user` block `tool_result` | `tool_use_id`, `content` (string or block array), `is_error` | `✅ <Tool> · <size>` or `❌ <Tool> · <120-char first-line preview>` | **[documented]** — currently dropped entirely by the filter |
| `result` | `subtype`, `is_error`, `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd`, `usage{input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens}`, `result`, `model_usage`, `api_error_status` (v2.1.110+), `terminal_reason` (`completed`/`max_turns`/`aborted_streaming`/`aborted_tools`) | turn separator + feeds the footer (§5); per-turn `usage` summed into the FORGE_USAGE receipt | **[documented]** (fields per Agent SDK types; exact `subtype` error values like `error_max_turns`/`error_max_budget_usd` are **[inference]** — only `success` is stable) |
| non-JSON line | — | SUPPRESSED (counter, one report line per 100) | `2>&1` merges CLI warnings/stderr prose into the pipe **[inference]** |

### 1.2 Tool-input summarizer (≤80 chars)

`b.input` keys per well-known tool **[documented tool schemas]**, with fallback
`JSON.stringify(input).slice(0,80)` **[inference]**:

| Tool | Hint source | Example |
|---|---|---|
| `Edit` / `Write` / `Read` | `input.file_path` (basename unless ambiguous; add `+n −n` only if a diff is cheaply derivable — otherwise omit) | `🔧 Edit src/x.py` |
| `Bash` | `input.command` (first line) | `🔧 Bash git status` |
| `Grep` | `input.pattern` + `input.path` | `🔧 Grep "TODO" src/` |
| `Glob` | `input.pattern` | `🔧 Glob **/*.py` |
| `WebFetch` / `WebSearch` | `input.url` / `input.query` | `🔧 WebFetch docs.x.ai/…` |
| `Task` (subagent) | `input.description` | `⟲ 🔧 Task run tests` |
| `mcp__<server>__<tool>` | first scalar value in `input` | `🔧 mcp__linear__create_issue FORGE-12` |

### 1.3 Thinking + `thinking_tokens` suppression (the spam killer)

- `system/thinking_tokens` (`estimated_tokens`): never print per event. Sum per
  turn into `thinkTokens`; print totals only, attached to the NEXT turn
  separator (`💭 3 phases · ~2.1k tok`) — zero lines during, one number after.
  **[documented-as-observed field; aggregation rule is forge policy]**
- `assistant/thinking` blocks: print only the FIRST one per `result`-to-`result`
  window, capped at 110 chars; further blocks in the same window collapse into
  `💭 +2 more`. **[inference: GLM gateway emits several thinking blocks/turn]**

### 1.4 Bugs in the current `claude-events-filter.mjs` (fix in the rewrite)

1. **`api_error` branch is dead code.** The filter matches
   `e.subtype === "api_error"` and reads `e.retryAttempt`/`e.maxRetries`; the
   documented event is subtype **`api_retry`** with fields
   **`attempt`/`max_retries`/`retry_delay_ms`/`error_status`** — so today every
   retry is silently swallowed. **[documented]** vs filter code (line 71).
2. **No `user` handling** → no ✅/❌ tool outcomes at all.
3. **`thinking` unthrottled** → one line per thinking block (spam under GLM).
4. **Line caps differ from §5** (`tool` hint capped at 100, not 80).

### 1.5 Flag changes the lane needs

None. Keep `--output-format stream-json --verbose`; do NOT add
`--include-partial-messages` (delta spam, no filter value). Route stdout through
`tee <events> | node filter.mjs` in the Actions renderer too.

Sources: code.claude.com/docs/en/headless (system subtypes init/api_retry/
plugin_install/hook_*, stream_event gating, permission_denied, result metadata);
github.com/anthropics/claude-agent-sdk-python `types.py` (ResultMessage,
StreamEvent, terminal_reason, api_error_status, model_usage);
mer.vin/2026/08 "Your JSON Schema Doesn't Stop Claude…" (observed
`{"type":"system","subtype":"thinking_tokens","estimated_tokens":N}` lines);
takopi.dev stream-json cheatsheet; anthropics/claude-code CHANGELOG.md;
forge `ci/templates/claude-events-filter.mjs`.

---

## 2. Grok `streaming-json` schema + mapping

`grok -p … --output-format streaming-json` emits NDJSON, one `type`-tagged
object per line, derived from ACP session updates. **[documented]** — official
example payloads:

```json
{"type":"thought","data":"Analyzing the directory structure..."}
{"type":"tool_call","toolCallId":"call_1","title":"Read","kind":"read","status":"in_progress","toolName":"read_file","rawInput":{"path":"src/main.rs"},"content":[],"locations":[]}
{"type":"tool_call_update","toolCallId":"call_1","status":"completed","content":[],"rawOutput":{"lines":42},"locations":[]}
{"type":"text","data":"Here's a summary"}
{"type":"usage","messageId":"resp_1","stopReason":"end_turn","usage":{"input_tokens":812,"output_tokens":45,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"reasoning_tokens":0},"signature":"..."}
{"type":"end","stopReason":"end_turn","sessionId":"abc123","requestId":"xyz789","usage":{...},"num_turns":7,"modelUsage":{...}}
```

| Event | Fields | Line emitted |
|---|---|---|
| `text` (`data`) | chunk | buffer → flush on non-text event or close → `💬 …` (§5 caps) |
| `thought` (`data`) | chunk | buffer → throttled `💭 …` (same rule as §1.3) |
| `tool_call` | `toolCallId`, `title` (display), `kind` (`read`/`edit`/`execute`/…), `toolName`, `rawInput`, `status` | `🔧 <title> <hint from rawInput.path/command/pattern ≤80>`; record callId→title |
| `tool_call_update` | `status` (`completed`/`failed` — **[inference]** exact failure value; treat non-`completed`+non-`in_progress` as failed), `rawOutput`, `content` | `✅ <title> · <output size>` / `❌ <title> · <120-char preview>` |
| `usage` | snake_case tokens (uncached `input_tokens`) | turn separator: `── turn N · ↑… ↓… tok ──` |
| `plan` | `entries` | suppressed by default (`🗺 n-step plan` one-liner if FORGE_LOG_PLAN=1) |
| `available_commands` | `tools`, `commands` | suppressed (startup noise) |
| `error` | `message` (+ spend fields) | `❌ <message ≤160>` |
| `max_turns_reached`, `auto_compact_*` | — | `⏹ max turns reached` / `🗜 auto-compact` — list is non-exhaustive, so default-branch on unknown `type` = suppress+counter **[documented]** |
| `end` (always last) | `stopReason` (`end_turn`/`max_tokens`/`max_turn_requests`/`refusal`/`cancelled`), `num_turns`, `usage`, `modelUsage` | summary footer; `end.usage` wins over summed `usage` events (no double count) — same policy `harness_entry.parse_usage` already applies |

Lane change: none (flags already correct). Filter rewrite only.

Sources: xai-org/grok-build `crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md`
(Output Formats section: streaming-json event table + example lines, `end` is
always last, token-field policy); docs.x.ai/build/cli/headless-scripting
(format list); forge `ci/templates/grok-events-filter.mjs`.

---

## 3. OpenCode headless output + mapping

`opencode run` supports `--format` with values `default` (formatted prose) or
`json` ("raw JSON events") **[documented]**. The forge lane currently omits
`--format json` — that is THE flag change needed. Global `--print-logs`
(logs to stderr) and `--log-level` exist but are diagnostics, not a trace
**[documented]**.

`opencode run --auto --format json` emits JSONL where the top-level `type` IS
the part type (NOT a `message.part.updated` envelope) **[documented via source
+ multiple independent consumers; verify against installed version]**:

| Event | Fields | Line emitted |
|---|---|---|
| `step_start` | `part.id`, `part.messageID`, `part.snapshot` | suppressed (increment turn counter) |
| `text` | `part.text`, `part.time{start,end}` | buffer → `💬 …` |
| `reasoning` | `part.text` | throttled `💭 …` |
| `tool_use` | `part.tool` (`bash`/`read`/`write`/`edit`/`grep`/…), `part.callID`, `part.state{status, input, output, title}` — CLI emits only on completion (`status:"completed"`), no pending/running states | single combined line: `🔧/✅ <tool> <state.title or hint> · <output size>`; `❌` if state reports error **[inference: error surface inside state]** |
| `step_finish` | `part.reason` (`stop` final / `tool-calls` continuing), `part.cost` (USD), `part.tokens{input, output, reasoning, cache{read, write}}` | turn separator `── turn N · ↑… ↓… tok ──`; sum tokens+cost into the receipt — **this fixes opencode's `usage: null`** in candidate.meta.json |
| `error` | `error.name`, `error.data.message`, `statusCode`, `isRetryable` | `❌ APIError 429 Rate limit exceeded` |

The receipt shape: `input = tokens.input + tokens.cache.read + tokens.cache.write`
(matches how eval harnesses bank opencode input) **[inference]**; write it as
`source: "opencode:step_finish"`, `completeness: "aggregate"`.

Sources: opencode.ai/docs/cli (`--format` "default (formatted) or json (raw
JSON events)", global `--print-logs`/`--log-level`); observed event shapes via
multiple independent consumers of `packages/opencode/src/cli/cmd/run.ts`
(littlebearapps/untether cheatsheet, pkg.go.dev ailang executor,
pymc-labs/decision-lab log-processing notes).

---

## 4. GitHub Copilot CLI output + mapping

`copilot -p` runs one prompt and exits. Documented behavior: without `-s` it
prints "stats and decoration" plus the response; `-s` suppresses stats and
decoration "outputting only the agent's response". There is NO
`--output-format json` / stream flag on the CLI **[documented]**.

Machine events DO exist, just not on `-p` stdout **[documented]**:

1. **Session event log**: `~/.copilot/session-state/<sessionId>/events.jsonl`
   (documented turn-counting example). Persisted event names include
   `assistant.turn_start`/`turn_end` (turnId), `assistant.message` (content,
   outputTokens), `assistant.usage` (inputTokens, outputTokens, reasoningTokens,
   cacheRead/Write, cost, duration, finishReason), `tool.execution_start`
   (toolCallId, toolName, arguments), `tool.execution_complete` (toolCallId,
   success, result.content), `session.error`, `session.task_complete`,
   `session.shutdown` (totalApiDurationMs, codeChanges, modelMetrics). Envelope:
   `{id, timestamp, parentId, agentId, ephemeral, type, data}`.
2. **`--share=PATH`** — exports the session transcript to markdown after
   non-interactive completion (default `./copilot-session-<ID>.md`).
3. **`copilot --server --stdio`** (SDK JSON-RPC transport) — the full event
   stream, but that is a driver rewrite, not a filter.

Recommended mapping (fallback accepted, staged):

- **Now**: keep `copilot -p` WITHOUT `-s` (the decoration includes tool/step
  chatter and final stats — already more readable than raw JSON), wrap with the
  standard header/footer, accept `usage: null` (F22: unknown ≠ zero).
  Add `--no-ask-user` (headless cannot answer) **[documented]**.
- **Next** (cheap trace): before launch, note `$(ls -t
  ~/.copilot/session-state | head -1)`; after exit, tail the NEW session's
  `events.jsonl`, filter out `"ephemeral":true`, and re-render
  `tool.execution_*`/`assistant.usage`/`session.*` through the §5 grammar as a
  compact "copilot trace" block. **[inference: sessionId discovery is not
  documented for `-p`; verify directory layout on the installed version]**
- **Later**: SDK JSON-RPC driver for live events.

Sources: docs.github.com Copilot CLI programmatic reference (`-p`, `-s`,
`--no-ask-user`, `--share=PATH`, `--secret-env-vars`); docs.github.com
"Streaming session events" (event table, envelope) and "The agent loop"
(`~/.copilot/session-state/<sessionId>/events.jsonl`, `session.idle`);
github/copilot-sdk debugging guide (`--log-dir`, log levels, `--server --stdio`).

---

## 5. The unified line grammar

One grammar for all four filters. Timestamp `HH:MM:SS` local (ISO slice), one
space, glyph, space, payload. Nothing else ever goes to stdout from a filter.

```
── claude-code · model glm-5.3-flash[1m] · session 3f9a1c22 ──   # run header (init)
HH:MM:SS 🔧 Edit src/app.py                                      # tool start
HH:MM:SS ✅ Edit src/app.py · 412B                               # tool ok (+ result size)
HH:MM:SS ❌ Bash npm test · exit 1 · TypeError: cannot read …    # tool error (+120-char preview)
HH:MM:SS 💭 …splitting the refactor into three passes…           # thinking (throttled)
HH:MM:SS 💬 Done — all three modules now share the helper.       # assistant prose (capped)
HH:MM:SS ⏳ API retry 2/5 · HTTP 429 · wait 2.0s                 # retry / transient
HH:MM:SS ⟲ 🔧 Task run lint over changed files                   # subagent tool call
── turn 7 · ↑1.2k ↓340 tok ──                                    # turn separator (usage)
```

Rules:

- **Prefixes**: `🔧` tool start, `✅` tool ok, `❌` tool error, `💭` thinking,
  `💬` assistant prose, `⏳` retry/transient, `⟲` subagent scope, `⏹` limit hit
  (max turns), `🗜` compaction. Turn separators and the header/footer use `── … ──`.
- **Caps**: payload ≤ **160 chars** total line budget (after the timestamp);
  tool hint ≤ **80**; tool_result/`rawOutput`/`state.output` preview = first
  line only, ≤ **120** chars, shown ONLY on error; `💬` ≤ 140; `💭` ≤ 110.
- **Sanitize**: strip ANSI escapes (`\x1b\[[0-9;]*[A-Za-z]`), collapse all
  whitespace runs to single spaces, drop control chars, then truncate with `…`.
- **Token math**: `↑` input (uncached), `↓` output; when cache reads exist show
  `↑1.2k (c 165k)`; costs as `$0.0141` when the driver provides one.
- **Abbreviate**: ≥1000 → `12.5k`, ≥1e6 → `1.2M`.
- **Suppression is counted, not silent**: unknown/uninteresting events bump a
  counter; emit `… 87 routine events hidden` at most once per 100.
- **Footer** (on the terminal event: claude `result`, grok `end`, opencode last
  `step_finish` reason `stop` / process exit, copilot process exit):

```
── forge run summary ─────────────────────────────────────────
driver claude-code · model glm-5.3-flash[1m] · exit completed (0)
turns 42 · ↑184.2k (c 165.0k) ↓12.5k tok · $0.61 · 14m32s
tools 87 (✅ 85 · ❌ 2) · 💭 ~9.8k tok · hidden 1,204
result: bumped the retry budget and fixed the flaky import order test…
FORGE_USAGE:{"input_tokens":19200,"cached_input_tokens":165000,"output_tokens":12500,"completeness":"aggregate","source":"claude:result"}
```

The `FORGE_USAGE:` marker line stays byte-compatible with the current contract
(ADR-0016 §4 / F22 lite) — same shape, written on `close()`, still skipped when
zero receipts were seen.

---

## 6. Per-driver filter checklist (Node `readline` terms)

Shared skeleton (one module, driver passed via `argv[2]` or `FORGE_LOG_DRIVER`):

```js
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const cap = (s, n) => String(s ?? "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, "")
  .replace(/\s+/g, " ").trim().slice(0, n) + (String(s ?? "").length > n ? "…" : "");
const ts = () => new Date().toISOString().slice(11, 19);
const k = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
for await (const line of rl) { let e; try { e = JSON.parse(line); } catch { noise++; continue; } /* dispatch */ }
// on close: flush buffers, write summary + FORGE_USAGE receipt (skip if turns === 0)
```

**claude-code** (`ci/templates/claude-events-filter.mjs` rewrite):
1. `type === "system"` → switch on `subtype`: `init` header; `api_retry` →
   `⏳` line using `attempt`/`max_retries`/`retry_delay_ms`/`error_status`
   (fixes the dead `api_error` branch); `thinking_tokens` → `thinkTokens +=
   e.estimated_tokens ?? 0`; default → counter.
2. `type === "assistant"` → per content block: `tool_use` → hint table (§1.2),
   store `pending.set(b.id, b.name + " " + hint)`; `text` → `💬`; `thinking` →
   first-per-turn rule; all gated on `e.parent_tool_use_id ? "⟲ " : ""`.
3. `type === "user"` → blocks with `type === "tool_result"`: look up
   `pending.get(b.tool_use_id)`, emit `✅`/`❌` (`b.is_error`), size =
   `JSON.stringify(b.content).length`, error preview = first text block capped
   120; `pending.delete(...)`.
4. `type === "result"` → addUsage(`e.usage`) (sum, keep cache reads separate),
   emit turn separator + stash footer fields (`num_turns`, `duration_ms`,
   `total_cost_usd`, `terminal_reason`, `api_error_status`).
5. `close()` → footer + receipt (existing `writeUsageReceipt` logic verbatim).

**grok** (`ci/templates/grok-events-filter.mjs` rewrite): keep the
`update ?? msg.params?.update ?? msg` unwrap and text/thought buffering; add
(1) `tool_call` hint from `rawInput` (path/command/pattern), callId→title map;
(2) `tool_call_update` → `✅/❌` with `rawOutput` size + map lookup; (3) `usage`
→ turn separator instead of a raw `[grok:usage]` line; (4) `end` → footer where
`end.usage`/`num_turns`/`modelUsage` override the accumulated sums (no double
count); (5) default branch suppresses `available_commands`/`plan` with counter.

**opencode** (NEW `ci/templates/opencode-events-filter.mjs`): lane adds
`--format json`; filter dispatches on top-level `type`: `text`/`reasoning`
buffer → flush; `tool_use` → single `🔧/✅/❌` line from
`part.tool` + `part.state.title/input/output`; `step_finish` → turn separator
from `part.tokens` (+`part.cost`), accumulate receipt
(`input + cache.read + cache.write` as input, `reasoning` noted in footer);
`error` → `❌`; `step_start` suppressed. `close()` → footer + `FORGE_USAGE`
with `source: "opencode:step_finish"`.

**copilot**: no stdout filter is possible **[documented]**. Lane keeps `copilot
-p` (no `-s`) piping through a THIN pass-through filter that only prints the
header, passes lines through unchanged (caps already enforced by the CLI), and
prints the §5 footer with process exit + elapsed; usage stays `null`. Optional
post-run block: `ls -t ~/.copilot/session-state | head -1` → compact the new
`events.jsonl` (`jq 'select(.ephemeral != true)'`, then map
`tool.execution_start`→`🔧`, `tool.execution_complete`+`success:false`→`❌`,
`assistant.usage`→tokens, `session.task_complete`→result line) **[inference]**.

**Actions lane** (`src/forge/harness_entry.py`): render the identical
`… | tee -a <events> | node /tmp/filter.mjs` pipeline (download the filter from
the same pinned ref the GitLab templates use) so `parse_usage()` keeps reading
the RAW tee'd file while the job log gets only the grammar lines.

---

## Open items to verify against installed binaries

1. claude-code: exact set of `result.subtype` error values; whether
   `thinking_tokens` carries more fields than `estimated_tokens`.
2. grok: the failure `status` value of `tool_call_update` (`failed` vs
   `error`/`aborted`).
3. opencode: confirm `--format json` event shapes on the version pinned by the
   lane (field names are version-sensitive); whether failed tools emit at all
   in CLI JSON output.
4. copilot: `-p`-mode sessionId discovery under `~/.copilot/session-state/` and
   whether `events.jsonl` appears before process exit (tail-ability).
