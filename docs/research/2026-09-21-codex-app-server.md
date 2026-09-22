# OpenAI Codex App Server API Research (verified 2026-09-21)

Implementation reference for a real Codex adapter (not a Protocol stub). Researched against
the official docs at <https://developers.openai.com/codex/app-server> (that URL currently
serves/redirects to the `learn.chatgpt.com/docs/app-server` mirror; content is identical),
the upstream source README at `openai/codex` → `codex-rs/app-server/README.md`, and
third-party practitioners (Zenn technical survey, Promptfoo provider, GitHub issues).

Convention: **[documented]** = stated in official docs/source README; **[observed]** = verified
in third-party real-world implementations; **[inference]** = derived design conclusion.

---

## 0. The single most important fact

**There is no cloud HTTP/WebSocket endpoint.** The "Codex App Server" is a *local process*
you run: the `codex app-server` subcommand of the Codex CLI. It is a long-lived process that
hosts Codex threads and speaks **JSON-RPC 2.0** over **stdio (default)**, with optional
**WebSocket** and **Unix-socket** listeners. It is the same interface the official VS Code
extension and Codex Desktop/TUI use. Think "MCP server", not "REST API". [documented]

Consequences for the adapter design:

- You either **spawn the process** (`codex app-server`, newline-delimited JSON on
  stdin/stdout) — the supported production path — or connect to a WebSocket it exposes
  (experimental, explicitly "not supported for production workloads").
- The App Server binary is **open source**: `codex-rs/app-server` in
  <https://github.com/openai/codex>. You can read the Rust source for ground truth.
- For CI/automation OpenAI points people at the Codex SDK (`@openai/codex-sdk`,
  `@openai/codex-sdk` Python), which is itself a wrapper that spawns `codex app-server`
  (or the older `codex exec` JSONL mode) — i.e. building directly on the App Server
  protocol is the lower-level, more controllable variant of what the SDK does.
  [observed]

---

## 1. Transport and framing

### 1.1 Options via `--listen` — [documented]

| Transport | Start command | Framing |
|---|---|---|
| **stdio (default)** | `codex app-server` | Newline-delimited JSON (JSONL): exactly one JSON-RPC message per line on stdout; write one message per line to stdin. |
| **WebSocket (experimental)** | `codex app-server --listen ws://127.0.0.1:4500` | Exactly one JSON-RPC message per WebSocket **text frame**. |
| **Unix domain socket** | `codex app-server --listen unix:///path/to/sock` | WebSocket framing over HTTP Upgrade on the socket. |
| off | `codex app-server --listen off` | — |

- The WebSocket listener additionally serves plain HTTP `GET /readyz` and `GET /healthz`
  for health checks. Any request carrying an `Origin` header is rejected with **403**
  (CSRF protection — do not send `Origin` from a non-browser client; browsers cannot
  connect). [documented]
- **No SSE. No request/response HTTP API.** Everything is bidirectional JSON-RPC on one
  connection: you receive *notifications* (server → client events) interleaved with
  responses to your requests, and the server can also send *server-initiated requests*
  (e.g. approval prompts) that you must answer. [documented]

### 1.2 Wire format — [documented]

- JSON-RPC 2.0, **but the `"jsonrpc":"2.0"` header field is omitted on the wire.**
  Requests: `{"method": "...", "id": ..., "params": {...}}`; responses:
  `{"id": ..., "result": ...}` or `{"id": ..., "error": {...}}`; notifications have no
  `id`. Match responses to requests by `id` (numeric ids in the docs; JSON-RPC permits
  strings too — stick to monotonically increasing integers).
- One `initialize` request per connection, then the `initialized` notification (see §3).
  Requests before `initialize` fail with "Not initialized"; a second `initialize` fails
  with "Already initialized".
- Schema tooling (strongly recommended for adapter codegen, output is
  **version-specific** — pin the Codex version):
  ```
  codex app-server generate-ts --out ./schemas
  codex app-server generate-json-schema --out ./schemas
  ```

### 1.3 Remote connections — [documented]

```
codex app-server --listen ws://127.0.0.1:4500     # on the remote host
codex --remote ws://127.0.0.1:4500                # connect the TUI
```
`--remote` accepts `ws://`, `wss://`, and `unix://PATH`. For authenticated remote use
(token passed via env var name, never inline):

```
export CODEX_REMOTE_TOKEN="$(cat "$HOME/.codex/app-server-token")"
codex --remote wss://remote-host:4500 --remote-auth-token-env CODEX_REMOTE_TOKEN
```

Plain `ws://` is only appropriate for localhost or SSH-forwarded connections.

---

## 2. Authentication

There are **two separate auth layers** — do not conflate them.

### 2.1 Transport auth (WebSocket listener only) — [documented]

Flags on `codex app-server`:

| Mode | Flags | Client behavior |
|---|---|---|
| Static capability token | `--ws-auth capability-token --ws-token-file /path` (or `--ws-token-sha256 HEX`) | Send `Authorization: Bearer <token>` during the WebSocket handshake. |
| Signed bearer token | `--ws-auth signed-bearer-token --ws-shared-secret-file /path` (+ optional `--ws-issuer`, `--ws-audience`, `--ws-max-clock-skew-seconds`) | Send `Authorization: Bearer <signed-jwt>` during handshake. |

- Auth is enforced **before** the JSON-RPC `initialize` is accepted.
- Prefer `--ws-token-file` over raw tokens on the command line (process listings leak
  argv).
- **Warning:** non-loopback listeners currently allow *unauthenticated* connections by
  default during rollout. If you expose the WebSocket off-host without `--ws-auth`,
  anyone who can reach the port gets full agent access. For the adapter, prefer stdio
  (no transport auth needed) unless remote control is a hard requirement.

### 2.2 Model/account auth (how the server reaches OpenAI) — [documented]

The App Server **has no auth protocol of its own for the model backend** — it inherits the
shared Codex CLI auth state:

1. **ChatGPT sign-in (OAuth / device flow):** run `codex login` once; tokens are cached in
   `~/.codex/auth.json` and reused by every `codex app-server` session. Required for
   ChatGPT-workspace/cloud features (some features are limited or unavailable with API-key
   auth).
2. **API key:** set `OPENAI_API_KEY` (or `CODEX_API_KEY`) in the process environment.
   API-key and explicitly-external auth providers **bypass discovery**; custom
   ChatGPT-auth destinations require discovery before being treated as independent.

Runtime introspection methods: `account/read` (returns account state, experimental
`workspaceRouting` with `chatgptAccountId`, `backendOrigin`, `accountRoutingOverride` of
`us` / `us_cr` / `NO_CONSTRAINT`; null when API-only or signed out), `accounts/check`, plus
the `account/updated` notification and `account/login/completed` event — clients are
expected to re-read `configRequirements/read` and `account/read` on `account/updated`.

[observed, practitioners] A stale `auth.json` token surfaces as 401
("Incorrect API Key Provided") on the first turn — the adapter should surface
`Unauthorized` (see §8 `codexErrorInfo`) as a re-login condition, not a transient retry.

### 2.3 `clientInfo` compliance note — [documented]

`clientInfo.name` in `initialize` identifies your client in OpenAI's Compliance Logs
Platform. Enterprise integrations should contact OpenAI for the known-clients list. Use a
stable product name (e.g. `"forge"`), not a per-install UUID.

---

## 3. Connection handshake — [documented]

```json
{"method": "initialize", "id": 0, "params": {
  "clientInfo": {"name": "forge", "title": "Forge Adapter", "version": "0.1.0"},
  "capabilities": {
    "experimentalApi": true,
    "optOutNotificationMethods": [],
    "requestAttestation": true,
    "mcpServerOpenaiFormElicitation": true
  }
}}
```
then the notification:
```json
{"method": "initialized", "params": {}}
```

Capabilities:

- `experimentalApi: bool` — opt-in gate; experimental methods invoked without it are
  rejected with "`<descriptor> requires experimentalApi capability`".
- `optOutNotificationMethods: string[]` — exact-match method-name denylist for noisy
  notifications (e.g. `["item/agentMessage/delta"]`); unknown names ignored;
  notifications only (you cannot opt out of requests/responses).
- `requestAttestation`, `mcpServerOpenaiFormElicitation` — niche; safe to omit.

The `initialize` **result** returns a user-agent string plus `platformFamily` /
`platformOs`.

---

## 4. Core model: Thread → Turn → Item — [documented]

- **Thread** — a persisted conversation (event history is durable; clients can
  reconnect/resume). Created by `thread/start`; can be resumed, forked, archived, deleted.
- **Turn** — one user request plus all agent work answering it. Has `id`, `status`
  (`inProgress` → `completed` | `interrupted` | `failed`), and an ordered `items` list.
- **Item** — an input/output unit: `userMessage`, `agentMessage`, `commandExecution`,
  `fileChange`, `mcpToolCall`, `dynamicToolCall`, `collabToolCall`, `webSearch`, `plan`,
  `reasoning`, `imageView`, `functionCallOutput`, `enteredReviewMode`/`exitedReviewMode`,
  `contextCompaction`, `skill`.

### 4.1 `thread/start` — [documented]

```json
{"method": "thread/start", "id": 10, "params": {
  "model": "gpt-5.6-terra",
  "cwd": "/Users/me/project",
  "approvalPolicy": "never",
  "sandbox": "workspaceWrite",
  "personality": "friendly",
  "serviceName": "forge_adapter"
}}
```

Result:

```json
{"id": 10, "result": {
  "thread": {
    "id": "thr_123",
    "sessionId": "thr_123",
    "preview": "",
    "ephemeral": false,
    "modelProvider": "openai",
    "createdAt": 1730910000
  },
  "instructionSources": ["...AGENTS.md paths..."]
}}
```

- Emits a `thread/started` notification and **automatically subscribes the connection to
  turn/item events for that thread** — there is no separate `thread/subscribe` call for
  start/resume/fork.
- Per-thread settings given here become thread defaults; `turn/start` may override per
  turn (see §5). `serviceName` is optional client identification.
- Related: `thread/resume` (`threadId` + the same optional overrides; reconnects to
  persisted history — dynamic tools are restored from the rollout if not resupplied),
  `thread/fork` (`threadId` + `lastTurnId` or `ephemeral: true`; result adds
  `forkedFromId`; non-ephemeral forks copy source thread attachments), `thread/read`,
  `thread/list`, `thread/archive` / `unarchive` / `delete`, `thread/compact/start`,
  `thread/inject_items`, `thread/settings/update`, `thread/metadata/update`,
  `thread/unsubscribe`.
- A required MCP server failing to start makes `thread/start` / `thread/resume` fail.
- `thread/archive` / `thread/delete` reject removing a live internal worker with `-32600`.

### 4.2 Allowed config values — [documented]

- `approvalPolicy`: `"never"`, `"onRequest"`, `"unlessTrusted"` (servers may advertise
  `allowedApprovalPolicies`).
- `sandbox` / `sandboxPolicy.type`: `"dangerFullAccess"`, `"readOnly"`, `"workspaceWrite"`,
  `"externalSandbox"`.
  - `readOnly`: optional `access: {"type": "fullAccess"}` or
    `{"type": "restricted", "includePlatformDefaults": bool, "readableRoots": [...]}`.
  - `workspaceWrite`: `writableRoots: [...]`, `networkAccess: bool` (**boolean** here),
    optional `readOnlyAccess` (same restricted shape).
  - `externalSandbox`: `networkAccess` is the **string** `"restricted"` (default) or
    `"enabled"` — note the asymmetry vs `workspaceWrite`.

For a headless adapter, `approvalPolicy: "never"` + `workspaceWrite` sandbox with explicit
`writableRoots`/`networkAccess` is the deterministic choice; with `never`, commands that
would need approval are simply denied by policy instead of blocking on a server-initiated
approval request.

### 4.3 Reading history — [documented]

- `thread/read`: `{"threadId": "thr_123", "includeTurns": true}` — result thread includes
  `status: {"type": "notLoaded" | "idle" | "systemError" | "active", "activeFlags"?}` and
  `turns` when requested.
- `thread/list`: params `cursor`, `limit`, `sortKey` (`created_at` | `updated_at` |
  `recency_at`), `sortDirection` (`desc` default | `asc`), `modelProviders`, `sourceKinds`
  (default `["cli","vscode"]` when empty), `archived`, `isPinned`, `cwd` (string or
  array), `useStateDbOnly`, `searchTerm`, experimental `parentThreadId` /
  `ancestorThreadId`. Rows carry `id`, `preview`, `ephemeral`, `isPinned`,
  `modelProvider`, `createdAt`, `updatedAt`, `name`, `status`, `nextCursor`.
- **Treat `item/completed` payloads as the authoritative state** (see §7), not
  `thread/read` snapshots taken mid-turn.

---

## 5. Turns: start, steer, interrupt

### 5.1 `turn/start` — send a new turn — [documented]

```json
{"method": "turn/start", "id": 30, "params": {
  "threadId": "thr_123",
  "input": [
    {"type": "text", "text": "Run tests"}
  ],
  "cwd": "/Users/me/project",
  "approvalPolicy": "unlessTrusted",
  "sandboxPolicy": {
    "type": "workspaceWrite",
    "writableRoots": ["/Users/me/project"],
    "networkAccess": true
  },
  "model": "gpt-5.6-terra",
  "effort": "medium",
  "summary": "concise",
  "personality": "friendly",
  "outputSchema": {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"]
  }
}}
```

Result:

```json
{"id": 30, "result": {"turn": {"id": "turn_456", "status": "inProgress", "items": [], "error": null}}}
```

Input item types:

| Type | Shape |
|---|---|
| `text` | `{"type": "text", "text": "..."}` |
| `image` | `{"type": "image", "url": "https://.../design.png"}` |
| `localImage` | `{"type": "localImage", "path": "/tmp/screenshot.png"}` |
| `skill` | `{"type": "skill", "name": "skill-creator", "path": "/Users/me/.codex/skills/.../SKILL.md"}` |

`toolOutput` (for dynamic-tool round-trips) is a **sibling of `input`**, with `input: []` —
not combinable with user input:

```json
{"threadId": "thr_123", "input": [],
 "toolOutput": {"name": "run_tests", "namespace": null, "output": "All 42 tests passed."}}
```
(`output` may be a string or an array of content items; persists as a `functionCallOutput`
item.)

Per-turn overrides (model, effort, summary, approvalPolicy, sandboxPolicy, cwd, …)
**become the thread defaults** once the turn starts; `outputSchema` is turn-only.
`turn/start` is rejected when managed `model_provider` requirements fail.

### 5.2 `turn/steer` — steer the ACTIVE turn — [documented]

Steering appends user input to the **in-flight** turn; it does **not** create a new turn
and does **not** emit a new `turn/started`. The active turn's agent sees the injected
message as a mid-course correction and continues the *same* turn.

```json
{"method": "turn/steer", "id": 32, "params": {
  "threadId": "thr_123",
  "input": [
    {"type": "text", "text": "Actually focus on failing tests first."}
  ],
  "expectedTurnId": "turn_456"
}}
```

Rules — [documented]:

- `expectedTurnId` **must match the currently active turn's id** — an optimistic
  concurrency guard so you never steer the wrong (e.g. already-finished) turn. Stale or
  mismatched id returns the normal error; steering a turn that already completed is an
  error, not a queue-for-next-turn.
- **No per-turn overrides allowed** on steer (no model/sandbox/etc.) — it is pure input
  injection.
- Success appends the steering message into the active turn (it appears as items/events on
  the same `turnId`) and the turn continues to a single `turn/completed`.
- Internally the steering input is delivered to the model on its next opportunity
  mid-turn; some implementations surface a `turn.steered`-style event, but on the wire you
  primarily observe the new `userMessage`/item events under the same `turnId`.
  [observed, third-party SDK docs]

**Steering vs. new turn — decision table** [inference from documented behavior]:

| Situation | Call | Effect |
|---|---|---|
| Thread idle (`thread/status/changed` → `idle`, last turn `completed`) | `turn/start` | New `turn/started`, new `turnId` |
| Turn active (`status: "inProgress"`) and you want to redirect it | `turn/steer` with `expectedTurnId` = active turn id | Same turn continues with injected input |
| Turn active and your message is unrelated new work | `turn/interrupt`, then `turn/start` | Old turn ends `interrupted`, new turn begins |

Note: the `@openai/codex-sdk` TypeScript SDK did not expose steer for a long time
(GitHub issue #12329, protocol support since v0.99.0) — another reason to talk to the App
Server protocol directly from the adapter.

### 5.3 `turn/interrupt` — cancel the active turn — [documented]

```json
{"method": "turn/interrupt", "id": 33, "params": {
  "threadId": "thr_123",
  "turnId": "turn_456"
}}
```
Response is `{}`. The turn then ends via `turn/completed` with
`turn.status: "interrupted"`. Work already applied (files written, commands run) stays
applied — interrupt cancels future work, it does not roll back.

[observed, GitHub issue Aug 2026] A repeated `turn/interrupt` can stay pending
indefinitely after the turn was already interrupted (aborted turns linger "active" in
app-server thread state). Adapter guidance: only interrupt when you have an active
`turnId` from `turn/started`, and treat the `turn/completed(interrupted)` notification as
the source of truth rather than the interrupt response.

To fully detach from a thread without deleting it: `thread/unsubscribe` — result status is
`unsubscribed` | `notSubscribed` | `notLoaded`. After the **last** subscriber leaves, the
thread stays loaded for a **30-minute inactivity grace period**, then unloads
(`thread/status/changed` → `notLoaded`, and `thread/closed` is emitted).

---

## 6. Server-initiated requests (approvals) — must-handle — [documented]

Unless `approvalPolicy: "never"` guarantees no prompts, the server sends **requests**
(with `id`) that the client must answer by responding to that id. The pattern for all of
them: server request arrives → your client sends `{"id": <requestId>, "result":
<decision>}` → server confirms via `serverRequest/resolved`
(`{threadId, requestId}`).

- `item/commandExecution/requestApproval` — params include `itemId`, `threadId`,
  `turnId`, `reason?`, `command?`, `cwd?`, `commandActions?`,
  `proposedExecpolicyAmendment?`, `networkApprovalContext?`, experimental
  `availableDecisions` / `additionalPermissions`. Decisions: `"accept"`,
  `"acceptForSession"`, `"decline"`, `"cancel"`, or
  `{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": [...]}}`.
- `item/fileChange/requestApproval` — params include `itemId`, `threadId`, `turnId`,
  `reason?`, `grantRoot?`. Decisions: `accept` / `acceptForSession` / `decline` /
  `cancel`.
- `item/permissions/requestApproval` — respond with the granted subset; decisions carry
  `scope: "session"` or `"turn"`.
- Dynamic tools: `item/tool/call`; user input: `item/tool/requestUserInput`.
- MCP elicitation (form or url): respond `action: "accept"` + content, or
  `decline`/`cancel` with null content. The OpenAI-form variant requires the
  `mcpServerOpenaiFormElicitation` capability.

**An unanswered approval request blocks the turn indefinitely.** For an unattended
adapter, either use `approvalPolicy: "never"` or implement a decline-with-reason
responder.

---

## 7. Events and results — how you get output

All events arrive as **JSON-RPC notifications** on the same connection (auto-subscribed
per thread at start/resume/fork). Core sequence for one turn:

```
turn/started → (item/started → item/* deltas → item/completed)* → turn/completed
```

### 7.1 Turn lifecycle — [documented]

- `turn/started` — `{turn: {id, items: [], status: "inProgress", ...}}` (also carries
  `threadId` context).
- `turn/completed` — `{turn}` with `status: "completed" | "interrupted" | "failed"`;
  failures embed `error: {message, codexErrorInfo?, additionalDetails?}`.

### 7.2 Item events — [documented]

- `item/started` — full item as work begins; `item.id` matches later delta `itemId`.
- `item/completed` — final item. **Authoritative state — build your result model from
  these, treat deltas as progressive UI.**
- Deltas (append-only, in order): `item/agentMessage/delta`, `item/plan/delta`,
  `item/reasoning/summaryTextDelta` (with `summaryIndex`),
  `item/reasoning/summaryPartAdded`, `item/reasoning/textDelta`,
  `item/commandExecution/outputDelta` (stdout/stderr).
  `item/fileChange/outputDelta` is deprecated and no longer emitted.
- `turn/diff/updated` — `{threadId, turnId, diff}` aggregated unified diff for the turn.
- `turn/plan/updated` — `{turnId, explanation?, plan}`; entries `{step,
  status: pending|inProgress|completed}`.

### 7.3 Thread events — [documented]

`thread/started`, `thread/status/changed` (`{threadId, status}` — the idle/active signal
for steering decisions), `thread/tokenUsage/updated`, `thread/closed`,
`thread/archived`/`unarchived`/`deleted` (`{threadId}` each), `thread/name/updated`,
`thread/goal/updated` / `thread/goal/cleared`, `thread/settings/updated`.

### 7.4 Other notable notifications — [documented]

`hook/started` / `hook/completed`, `model/safetyBuffering/updated`, `model/rerouted`
(`{fromModel, toModel, reason}`), `model/verification`, `serverRequest/resolved`,
`mcpServer/startupStatus/updated`, `mcpServer/oauthLogin/completed`, `fs/changed`
(`{watchId, changedPaths}`), `skills/changed`, `configWarning`, `warning`, plus
process/terminal plumbing (`command/exec/outputDelta`, `process/outputDelta`,
`process/exited`) gated behind `experimentalApi`.

### 7.5 Receiving the final answer — [inference, from item semantics]

The turn's textual answer is the `item/completed` for the final `agentMessage` item
(`text` field; note optional `phase: "commentary" | "final_answer"` — prefer
`final_answer`/last agentMessage). File modifications come from `fileChange` items and
`turn/diff/updated`; command results from `commandExecution` items
(`aggregatedOutput`, `exitCode`, `durationMs`). If you passed `outputSchema`, the
structured answer arrives in the completed agent message per schema.

---

## 8. Errors, retries, and edge behavior — [documented unless noted]

- Failed turn: an error event carries `{error: {message, codexErrorInfo?,
  additionalDetails?}}` followed by `turn/completed` with `status: "failed"`.
  `httpStatusCode` is forwarded inside `codexErrorInfo` when available.
- `codexErrorInfo` values: `ContextWindowExceeded`, `UsageLimitExceeded`,
  `HttpConnectionFailed`, `ResponseStreamConnectionFailed`,
  `ResponseStreamDisconnected`, `ResponseTooManyFailedAttempts`, `BadRequest`,
  `Unauthorized`, `SandboxError`, `InternalServerError`, `Other`.
  (`ContextWindowExceeded` → consider `thread/compact/start`; `UsageLimitExceeded` and
  `Unauthorized` are not retryable.)
- Overloaded server rejects requests with JSON-RPC code **`-32001`**,
  "Server overloaded; retry later." — retry with **exponential backoff + jitter**.
- Standard JSON-RPC error codes apply elsewhere (`-32600` invalid request — also used to
  reject archiving live internal workers and managed-provider non-compliance; unknown
  methods hit the generic unknown-method rejection).
- Handshake errors: "Not initialized" (request before `initialize`), "Already
  initialized" (duplicate `initialize`).
- Experimental methods without the capability: "`<descriptor> requires experimentalApi
  capability`".
- `model/list` validates the startup provider against managed provider requirements; on
  non-compliance returns `-32600` asking the client to restart Codex.

---

## 9. Known limitations and caveats

1. **WebSocket transport is experimental** — official docs: "aren't supported for
   production workloads". Production adapters should spawn stdio. [documented]
2. **Unauthenticated non-loopback WS by default** during rollout — security footgun; use
   `--ws-auth` or stay on stdio. [documented]
3. **Schemas are version-specific**: regenerate `generate-ts` / `generate-json-schema`
   artifacts per pinned Codex version; method surface moves fast (e.g. `turn/steer`
   added in protocol v0.99.0; `thread/rollback` already deprecated in favor of
   `thread/revert`). [documented]
4. **Repeated `turn/interrupt` may hang** after the turn already ended — key off
   `turn/completed(interrupted)` instead. [observed, GitHub issue]
5. `thread/started`-style auto-subscribe covers start/resume/fork; there is **no
   documented attach-to-arbitrary-active-thread method** for external clients (open
   GitHub feature request, June 2026 — discovering/attaching to a Desktop UI thread).
   The adapter owns the threads it creates. [documented]
6. `thread/start`/`thread/resume` **fail if a required MCP server fails to start**;
   `turn/start` (and steer, review, compaction) are rejected under managed-provider
   non-compliance, though interrupt and goal pause/clear still work. [documented]
7. Last-subscriber threads linger **30 minutes** before unload (`thread/closed`) —
   relevant for process lifecycle/CPU if you run many threads. [documented]
8. `plugin/list|read|install|uninstall` are "under development" — do not call from
   production clients. Paginated-`historyMode` thread creation returns `-32601`; full
   history reads/resume fail closed until paginated history is supported.
   [documented]
9. `thread/shellCommand` runs **outside the sandbox** with full access — avoid exposing
   it through the adapter. [documented]
10. API-key auth limits some ChatGPT-workspace/cloud features (plugins curated set,
    workspace routing). [documented]

---

## 10. Minimal working example

### 10.1 Node.js over stdio (official docs example, lightly adapted) — [documented]

```ts
import { spawn } from "node:child_process";
import readline from "node:readline";

const proc = spawn("codex", ["app-server"], { stdio: ["pipe", "pipe", "inherit"] });
const rl = readline.createInterface({ input: proc.stdout });

// One JSON-RPC message per line; "jsonrpc":"2.0" header omitted on the wire.
const send = (m: unknown) => proc.stdin.write(`${JSON.stringify(m)}\n`);

let threadId: string | null = null;
let turnId: string | null = null;

rl.on("line", (line) => {
  const msg = JSON.parse(line);

  // Responses (have id + result/error)
  if (msg.id === 1 && msg.result?.thread?.id && !threadId) {
    threadId = msg.result.thread.id;
    send({ method: "turn/start", id: 2, params: {
      threadId,
      input: [{ type: "text", text: "Summarize this repo." }],
    }});
    return;
  }
  if (msg.id === 2 && msg.result?.turn?.id) {
    turnId = msg.result.turn.id;
    return;
  }

  // Notifications (no id)
  switch (msg.method) {
    case "turn/started":            turnId = msg.params.turn.id; break;
    case "item/agentMessage/delta": process.stdout.write(msg.params.delta); break;
    case "item/completed":          /* authoritative items */ break;
    case "turn/completed":
      console.log("\n[turn ended]", msg.params.turn.status, msg.params.turn.error ?? "");
      // Steer-then-done example: to have steered mid-turn we would earlier have sent
      // {method:"turn/steer", id:99, params:{threadId, expectedTurnId: turnId,
      //   input:[{type:"text",text:"Focus on failing tests first."}]}}
      // To cancel: {method:"turn/interrupt", id:98, params:{threadId, turnId}}
      break;
  }
});

// Handshake
send({ method: "initialize", id: 0, params: {
  clientInfo: { name: "forge", title: "Forge Adapter", version: "0.1.0" },
  capabilities: { experimentalApi: false },
}});
send({ method: "initialized", params: {} });
send({ method: "thread/start", id: 1, params: {
  model: "gpt-5.6-terra",
  cwd: process.cwd(),
  approvalPolicy: "never",
  sandbox: "workspaceWrite",
}});
```

### 10.2 WebSocket client sketch — [inference from documented framing]

```js
// Server side:  codex app-server --listen ws://127.0.0.1:4500 \
//                 --ws-auth capability-token --ws-token-file /path/token
const ws = new WebSocket("ws://127.0.0.1:4500", {
  headers: { Authorization: `Bearer ${require("fs").readFileSync("/path/token","utf8").trim()}` },
});                                    // node 'ws' pkg; browsers get 403 (Origin rule)
let nextId = 0;
const pending = new Map();
function rpc(method, params) {
  const id = nextId++;
  ws.send(JSON.stringify({ method, id, params }));   // one JSON-RPC message per TEXT FRAME
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}
ws.on("message", (data) => {
  const m = JSON.parse(data.toString());
  if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
    const p = pending.get(m.id); m.error ? p.rej(m.error) : p.res(m.result); pending.delete(m.id);
  } else if (m.method) {
    handleNotification(m);          // turn/*, item/*, thread/* …
  } else {
    handleServerRequest(m);         // approvals: reply ws.send(JSON.stringify({id: m.id, result: "accept"}))
  }
});
// Health checks: GET http://127.0.0.1:4500/readyz and /healthz (no Origin header!)
// Then: await rpc("initialize", {...}); ws.send(JSON.stringify({method:"initialized",params:{}}));
```

### 10.3 Python sketch (stdio) — [inference from documented framing]

```python
import json, subprocess, threading, queue

proc = subprocess.Popen(
    ["codex", "app-server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True)
out = queue.Queue()

def reader():
    for line in proc.stdout:               # one JSON-RPC message per line
        out.put(json.loads(line))
threading.Thread(target=reader, daemon=True).start()

def send(msg): proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
def rpc(method, params, id_, timeout=600):
    send({"method": method, "id": id_, "params": params})
    while True:
        m = out.get(timeout=timeout)
        if m.get("id") == id_:
            if "error" in m: raise RuntimeError(m["error"])
            return m["result"]
        handle_notification(m)             # turn/*, item/*, thread/*

send({"method": "initialize", "id": 0, "params": {
    "clientInfo": {"name": "forge", "title": "Forge Adapter", "version": "0.1.0"},
    "capabilities": {}}})
send({"method": "initialized", "params": {}})
thread = rpc("thread/start", {"model": "gpt-5.6-terra", "cwd": "/repo",
                              "approvalPolicy": "never", "sandbox": "workspaceWrite"}, 1)
rpc("turn/start", {"threadId": thread["thread"]["id"],
                   "input": [{"type": "text", "text": "Run the tests."}]}, 2)
# Consume `out` until turn/completed for that thread.
```

---

## 11. Adapter checklist — [inference]

1. Spawn `codex app-server` over stdio (production-safe); keep WebSocket optional.
2. Handshake: `initialize` (stable `clientInfo.name`, `experimentalApi` only if needed) →
   `initialized`.
3. Track request ids; route incoming frames into three buckets: responses (match `id`),
   notifications (`method`, no `id`), server-initiated requests (`method` + `id` → must
   answer).
4. `thread/start` with `approvalPolicy: "never"` and an explicit `workspaceWrite`
   `sandboxPolicy` for determinism; store `threadId`.
5. `turn/start` per request; capture `turnId` from the *response* (or `turn/started`).
6. Steer with `expectedTurnId` = active turn id when redirecting; `turn/interrupt` +
   fresh `turn/start` when replacing; never `turn/start` while a turn is inProgress
   expecting queueing.
7. Build results from `item/completed` (+ `turn/completed` status/error); treat deltas as
   optional streaming.
8. Handle `-32001` overload with backoff+jitter; map `codexErrorInfo`
   (`UsageLimitExceeded`, `Unauthorized`, …) to adapter error classes; surface
   `ContextWindowExceeded` → compaction path.
9. Pin the Codex CLI version and commit generated `generate-json-schema` artifacts next to
   the adapter; regenerate on upgrade.
10. Auth: `codex login` state in `~/.codex/auth.json` or `OPENAI_API_KEY` in the spawn
    environment; monitor `account/updated` and re-read `account/read`.

---

## Sources

- Official docs: [Codex App Server — developers.openai.com/codex/app-server](https://developers.openai.com/codex/app-server) (content served via the [learn.chatgpt.com/docs/app-server](https://learn.chatgpt.com/docs/app-server) mirror)
- Source README: [openai/codex → codex-rs/app-server/README.md](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- OpenAI blog: [Unlocking the Codex harness: how we built the App Server](https://openai.com/index/codex-app-server/) (Feb 2026)
- [Technical Survey – Codex App Server (Zenn, May 2026)](https://zenn.dev/articles/codex-app-server-survey)
- [A developer's guide to OpenAI Codex's JSON-RPC interface (gist)](https://gist.github.com/)
- [Promptfoo OpenAI Codex App Server provider](https://www.promptfoo.dev/docs/providers/openai/)
- GitHub issues: [#12329 — Expose turn/steer in the TypeScript SDK](https://github.com/openai/codex/issues/12329); repeated `turn/interrupt` pending bug (Aug 2026); app-server client attach-to-active-thread feature request (June 2026)
- [Codex App Server Python SDK guide (jaesolshin.com, May 2026)](https://jaesolshin.com/)


---

## LIVE CORRECTION — codex-cli 0.153.4 (verified 2026-09-21, forge live smoke)

The sandbox spellings this doc carried are wrong for 0.153.4, and the
doc left turn/start response timing ambiguous. Verified against a real
`codex app-server` (evidence: `docs/evaluation/2026-09-21-drivers/codex-live.json`):

1. **The sandbox variant enums are ASYMMETRIC per surface.**
   `thread/start`'s `sandbox` string parameter wants kebab-case —
   `read-only`, `workspace-write`, `danger-full-access` (camelCase is
   rejected: "unknown variant `workspaceWrite`"). `turn/start`'s
   `sandboxPolicy.type` wants camelCase — `readOnly`, `workspaceWrite`,
   `dangerFullAccess`, plus an `externalSandbox` variant this doc did
   not know (kebab rejected there). The forge driver normalizes input
   of either spelling and emits per-surface.
2. **`turn/start` responds at turn ACCEPTANCE (~0.4 s), not at turn
   completion.** Completion is the `turn/completed` notification
   (status `completed|interrupted|failed`). A client that treats the
   response as the turn result will see turns "finish" instantly.
3. The handshake (`initialize` → response → `initialized`
   notification), `thread/start`, `turn/steer` with `expectedTurnId`
   (stale id errors, never queues), `turn/interrupt` keyed off
   `turn/completed(interrupted)`, and ChatGPT-login auth via inherited
   environment all behaved exactly as this doc describes.
