# OpenCode Server API — Research for a Real Adapter

> Researched 2026-09-21 from official docs (opencode.ai), the `sst/opencode` GitHub repo
> (via DeepWiki index of `packages/opencode/src/server/**`, `packages/protocol/src/groups/session.ts`,
> `packages/sdk/js/src/v2/gen/types.gen.ts`), and community sources.
>
> **Bottom line:** OpenCode is a *client-server* system, not just a CLI. `opencode serve` runs a
> headless HTTP server with a documented REST API plus Server-Sent Events (SSE) for streaming.
> An OpenAPI 3.1 spec is served live at `/doc`, and the official JS SDK (`@opencode-ai/sdk`) is
> generated from that spec. **No WebSocket** — events are SSE only.

---

## 1. Architecture

- Running `opencode` (TUI) starts the TUI **plus** a server; the TUI is just another client of the server.
- `opencode serve` runs the server standalone/headless. If a TUI is already running, `opencode serve`
  starts a **separate, independent** server instance.
- The server is written in TypeScript on the Effect framework (`HttpApiBuilder` + Effect Schema);
  every route is typed and reflected into the OpenAPI 3.1 spec.
- The server manages: sessions, messages, agent execution, config, providers/auth, file search,
  LSP, formatters, MCP servers, permissions, and TUI control (for IDE plugins).

## 2. Starting the server

```bash
opencode serve [--port <number>] [--hostname <string>] [--cors <origin>] [--mdns] [--mdns-domain <domain>]
```

| Flag           | Default         | Notes                                        |
|----------------|-----------------|----------------------------------------------|
| `--port`       | `4096`          | HTTP port                                    |
| `--hostname`   | `127.0.0.1`     | Bind address (use `0.0.0.0` to expose)       |
| `--cors`       | `[]`            | Repeatable, e.g. `--cors http://localhost:5173` |
| `--mdns`       | off             | Advertises `opencode.local` when enabled     |

Example:

```bash
opencode serve --port 4096 --hostname 127.0.0.1 --cors http://localhost:5173
```

**OpenAPI spec:** open `http://127.0.0.1:4096/doc` (HTML page with the OpenAPI 3.1 spec).
**Health check:** `GET /global/health` → `{"healthy": true, "version": "<version>"}`.

The TUI (`opencode` with no args) also starts a server, but on a **random** port unless
`--port`/`--hostname` are passed. For an adapter, always use `opencode serve` with explicit flags.

## 3. API surface (HTTP, all JSON)

There is **no WebSocket**. Two transports:

1. Plain HTTP request/response for commands and queries.
2. **SSE** (`text/event-stream`) for events/streaming: `GET /event` (project/instance-scoped bus)
   and `GET /global/event` (global). A newer durable per-session stream exists at
   `GET /api/session/:sessionID/event?after=<seq>` (see §5).

### Endpoint catalogue (from `packages/web/src/content/docs/server.mdx`)

**Global / instance**
- `GET /global/health` → `{ healthy: true, version: string }`
- `GET /global/event` → SSE stream
- `POST /instance/dispose` → `boolean` (shuts down instances; emitted as event)

**Project / path / VCS**
- `GET /project` → `Project[]`; `GET /project/current` → `Project`
- `GET /path` → `Path`; `GET /vcs` → `VcsInfo`

**Config**
- `GET /config` → `Config`; `PATCH /config` (body: `Config`) → `Config`
- `GET /config/providers` → `{ providers: Provider[], default: { [key]: string } }`

**Providers / auth (BYOK)**
- `GET /provider` → `{ all: Provider[], default: {...}, connected: string[] }`
- `GET /provider/auth` → `{ [providerID]: ProviderAuthMethod[] }`
- `POST /provider/{id}/oauth/authorize` → `ProviderAuthAuthorization`
- `POST /provider/{id}/oauth/callback` → `boolean`
- `PUT /auth/{id}` — **set provider credentials**; "body must match provider schema" → `boolean`

**Sessions**
- `GET /session` → `Session[]`
- `POST /session` — body `{ parentID?, title? }` → `Session`  ← **create**
- `GET /session/status` → `{ [sessionID]: SessionStatus }` (busy/idle per session)
- `GET|PATCH|DELETE /session/:id` — PATCH `{ title? }` → `Session`; DELETE → `boolean`
- `GET /session/:id/children` → `Session[]`; `GET /session/:id/todo` → `Todo[]`
- `POST /session/:id/init` — body `{ messageID, providerID, modelID }` → `boolean` (writes AGENTS.md etc.)
- `POST /session/:id/fork` — body `{ messageID? }` → `Session`
- `POST /session/:id/abort` → `boolean`  ← **abort**
- `POST /session/:id/share` → `boolean`; `DELETE /session/:id/share` → `Session`
- `GET /session/:id/diff?messageID=` → `FileDiff[]`
- `POST /session/:id/summarize` — body `{ providerID, modelID }` → `boolean`
- `POST /session/:id/revert` — body `{ messageID, partID? }` → `boolean`; `POST /session/:id/unrevert` → `boolean`
- `POST /session/:id/permissions/:permissionID` — body `{ response, remember? }` → `boolean`

**Messages / prompts**
- `GET /session/:id/message?limit=` → `{ info: Message, parts: Part[] }[]`  ← pollable history
- `POST /session/:id/message` — body (below) → `{ info: Message, parts: Part[] }`  ← **send prompt (blocking until the assistant finishes)**
- `GET /session/:id/message/:messageID` → `{ info: Message, parts: Part[] }`
- `POST /session/:id/prompt_async` — same body → **204 No Content** (results arrive via SSE)
- `POST /session/:id/command` — body `{ messageID?, agent?, model?, command, arguments }`
- `POST /session/:id/shell` — body `{ agent, model?, command }`

**Files / search**
- `GET /find?pattern=` (ripgrep-style text search), `GET /find/file?query=`, `GET /find/symbol?query=`
- `GET /file?path=` → `FileNode[]`; `GET /file/content?path=`; `GET /file/status` → `File[]`

**Other**
- `GET /agent` → `Agent[]` (build/plan/general agents, custom agents)
- `GET /command` → `Command[]`
- `GET /lsp`, `GET /formatter`, `GET /mcp`; `POST /mcp` — body `{ name, config }` (add MCP server at runtime)
- `GET /experimental/tool/ids`, `GET /experimental/tool?provider=&model=` (experimental)
- `POST /log` — body `{ service, level, message, extra? }` → `boolean`
- TUI control (only meaningful when attached to a TUI): `POST /tui/append-prompt`, `/tui/submit-prompt`,
  `/tui/clear-prompt`, `/tui/execute-command`, `/tui/show-toast`, `/tui/open-*`, `GET /tui/control/next`,
  `POST /tui/control/response`

## 4. Session lifecycle (create → prompt → events → abort)

### 4.1 Create

`POST /session` with optional `{ "title": "..." }` → `Session`. Minimal: empty body `{}` works.

Key `Session` fields (from SDK gen types):

```ts
type Session = {
  id: string            // e.g. "ses_123..."
  slug: string
  projectID: string
  workspaceID?: string
  directory: string     // working directory the session operates on
  parentID?: string
  title: string
  agent?: string
  model?: { id: string; providerID: string; variant?: string }
  share?: { url: string }
  cost?: number
  tokens?: { input: number; output: number; reasoning: number; cache: { read: number; write: number } }
  version: string
  time: { created: number; updated: number; compacting?: number; archived?: number }
  // ... revert, permission ruleset, summary metadata
}
```

### 4.2 Send a prompt

`POST /session/:id/message`:

```json
{
  "model":   { "providerID": "anthropic", "modelID": "claude-sonnet-4-5" },
  "agent":   "build",
  "messageID": "optional-client-chosen-id",
  "noReply": false,
  "system":  false,
  "tools":   ["optional tool allowlist"],
  "parts":  [ { "type": "text", "text": "Fix the failing test in src/api.ts" } ]
}
```

- `parts` is an array of message parts; the essential one is `TextPart`:
  `{"type":"text","text":"..."}` (files can be attached via other part types — check `/doc` for the
  full `MessagePart` union on your version).
- `model` is `{providerID, modelID}` (in the JS SDK this is `body.model` as an object).
- `agent`: `"build"` (default, can edit files), `"plan"` (read-only), or a custom agent from `GET /agent`.
- `noReply: true` injects context as a user message **without** triggering an assistant reply.
- Response: `{ "info": Message, "parts": Part[] }` — the **assistant** message after it completes.
  The call **blocks for the whole agent run** (possibly minutes) — set HTTP client timeouts accordingly.
- Async alternative: `POST /session/:id/prompt_async` with the same body → `204` immediately;
  consume results via SSE (`/event`) or polling (`GET /session/:id/message`).

`Message` (the `info` field) shape (from `packages/opencode/src/session/message.ts`):

```ts
{
  id: string
  role: "user" | "assistant"
  parts: MessagePart[]          // part IDs live here
  metadata: {
    time: { created: number; completed?: number }
    error?: { name: string; data?: unknown }   // e.g. AbortError, StructuredOutputError
    sessionID: string
    assistant?: {                // present on assistant messages
      system: string[]
      modelID: string
      providerID: string
      path: { cwd: string; root: string }
      cost: number
      tokens: { input: number; output: number; reasoning: number; cache: { read: number; write: number } }
    }
    tool?: { [toolCallID]: { title: string; snapshot?: string; time: { start: number; end: number } } }
  }
}
```

Text is assembled from the assistant message `parts` by concatenating parts where `type === "text"`
(each has `{ id, type: "text", text }`). Tool invocations appear as tool parts with `state`
(pending/running/completed/error) — see `/doc` for the exact union on your build.

### 4.3 Receive events / streaming

Subscribe to `GET /event` (SSE) **before** sending `prompt_async` so no events are missed.
Useful event types observed in the repo/tests:

- `server.connected` — first frame on connect
- `server.heartbeat` — every 10 s (from the handler source)
- `session.created`, `session.disposed`, `session.idle` (assistant turn finished), `session.error`
- `message.updated` (message info changed — cost/tokens/time as the run progresses)
- `session.next.text.started` / `session.next.text.delta` / `session.next.text.ended` —
  streaming text deltas for the assistant reply (`{ sessionID, assistantMessageID, textID, delta|text }`)
- `permission.asked` / permission requested + responded
- `installation.updated` (after self-upgrade)

Practical loop for an adapter using async mode:
1. Open SSE `/event`, ignore until `server.connected`.
2. `POST /session` → keep `session.id`.
3. `POST /session/:id/prompt_async` → 204.
4. Buffer `session.next.text.delta` events filtered by `sessionID` → stream to caller.
5. Finish on `session.idle` for that `sessionID` (or `session.error` / message `metadata.error`).
6. Optionally `GET /session/:id/message` for the authoritative final transcript.

If you use the **blocking** `POST /session/:id/message` instead, you get the final assistant
message directly in the response — but no incremental streaming, and the HTTP call is long-lived.

### 4.4 Abort

`POST /session/:id/abort` → `boolean`. Aborts the in-flight agent run for that session. The
in-flight assistant message gets `metadata.error` (e.g. abort error), and a `session.idle` event
follows. `GET /session/status` tells you which sessions are currently busy.

### 4.5 Delete / cleanup

`DELETE /session/:id` → `boolean`.

## 5. SSE wire format (exactly)

From `packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts`:

```ts
function eventData(data) {
  return { _tag: "Event", event: "message", id: undefined, data: JSON.stringify(data) }
}
```

So every frame is:

```
event: message
data: {"type":"message.updated","properties":{ ... },"id":"evt_..."}
```

- The SSE `event:` line is **always `message`** — do not switch on it; switch on the JSON `type`.
- `data:` holds one JSON object; the canonical client-side decode shape is
  `{ id?: string; type: string; properties: Record<string, any> }`.
- Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`,
  `X-Accel-Buffering: no`.
- First frame: `{"type":"server.connected","properties":{}}`. Then bus events; heartbeats every 10 s.

**Event-system caveat (important for an adapter):** the event layer was rewritten ("EventV2").
On current builds the internal bus also emits a durable wrapper of the form
`{ type: "sync", syncEvent: { id, type, seq, aggregateID, data } }`, and a durable per-session
endpoint `GET /api/session/:sessionID/event?after=<lastSeq>` replays events after a sequence
number (`seq`) for reconnect-safe consumption. The **global `/event stream has no replay
guarantee** — if you drop the connection you may miss events; reconcile with
`GET /session/:id/message`. Some frames may nest the payload under `payload` (e.g.
`{ payload: { id, type, properties }, directory, project, workspace }`) depending on version —
**parse defensively**: `const ev = json.payload ?? json`, then read `ev.type` / `ev.properties`.

## 6. Authentication (two separate layers)

### 6.1 Server access auth (protecting the HTTP API)

HTTP **Basic auth**, enabled only if `OPENCODE_SERVER_PASSWORD` is set:

```bash
OPENCODE_SERVER_PASSWORD=s3cret opencode serve
# optional custom username (default "opencode"):
OPENCODE_SERVER_USERNAME=myuser OPENCODE_SERVER_PASSWORD=s3cret opencode serve
```

- Applies to `opencode serve` and `opencode web`.
- No API-key/token auth for the server itself — Basic or nothing. Default bind is `127.0.0.1`,
  which keeps it private on the host.
- `opencode run` / `opencode attach` accept `--username` / `--password` to reach a protected server.

### 6.2 Provider auth — BYOK (model API keys)

Three ways, all compatible with headless operation:

**a) `auth.json` file** (what the TUI `/connect` command writes) at
`~/.local/share/opencode/auth.json`:

```json
{
  "anthropic": { "type": "api", "key": "sk-ant-..." },
  "openai":    { "type": "api", "key": "sk-..." }
}
```

Verify with `opencode auth list`. `opencode auth login --provider anthropic --method api` is the
interactive equivalent (prompts for the key).

**b) Over HTTP at runtime:** `PUT /auth/:id` with the same body shape → `boolean`:

```
PUT /auth/anthropic
{ "type": "api", "key": "sk-ant-..." }
```

(SDK: `client.auth.set({ path: { id: "anthropic" }, body: { type: "api", key: "..." } })`.)
OAuth flows exist via `POST /provider/{id}/oauth/authorize` + `/callback` for providers that
support them.

**c) Config file (`opencode.json`) custom provider** — fully BYOK against any
OpenAI-compatible endpoint:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "my-local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "My Local LLM",
      "options": {
        "baseURL": "http://127.0.0.1:1234/v1",
        "apiKey": "{env:MY_API_KEY}"
      },
      "models": {
        "some-model": { "name": "Some Model", "limit": { "context": 128000, "output": 4096 } }
      }
    }
  }
}
```

- `npm` selects the AI SDK backend (`@ai-sdk/openai-compatible` for `/v1/chat/completions`,
  `@ai-sdk/openai` for `/v1/responses`).
- `options.apiKey` supports substitution: `"{env:ANTHROPIC_API_KEY}"`, `"{file:~/.secrets/key}"`.
- `options.headers` for custom headers (e.g. Helicone/gateway routing).
- Many first-party providers also just read standard env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `NVIDIA_API_KEY`, AWS/Vertex/GitLab env chains, etc.) or a `.env` in the project.
- Config can also be read/written over HTTP: `GET /config`, `PATCH /config`; runtime model/provider
  discovery: `GET /config/providers`, `GET /provider`.

## 7. Permission flow (tools that need approval)

1. During a run the agent may request permission (e.g. bash command, file write outside ruleset).
2. SSE event (shape from repo tests):

```json
{
  "id": "evt_...",
  "type": "permission.asked",
  "properties": {
    "id": "perm_123",
    "sessionID": "ses_...",
    "permission": "bash",
    "patterns": ["rm -rf ..."],
    "metadata": {},
    "always": ["..."],
    "tool": { "messageID": "msg_...", "callID": "call_..." }
  }
}
```

3. Respond: `POST /session/:id/permissions/<that id>` with body
   `{ "response": "once" | "always" | "reject", "remember": true? }` → `boolean`.
4. A `permission responded` event follows and the run continues.

An adapter that wants fully unattended operation should either pre-configure permissive
permission rules in the session/config or auto-respond to these events.

## 8. Known limitations / gotchas

- **No WebSocket.** Events are SSE-only; commands are HTTP-only. (Verify on your build via `/doc`.)
- **Event schema churn.** The event system was migrated to "EventV2" with new type names
  (`message.part.delta`, `session.next.text.*`, durable `sync` events with `seq`). Names/shapes
  differ between versions. **Always fetch `/doc` from the running server and pin your adapter to
  tested versions**; the official SDK is generated per-release and should be version-matched to
  the server.
- **Global `/event` has no replay.** Missed frames on reconnect are gone; reconcile via
  `GET /session/:id/message`. The durable session event endpoint (`?after=<seq>`) fixes this but
  is the newer API surface.
- **Blocking prompt calls are long.** `POST /session/:id/message` holds the connection for the
  entire agent run. A reported GitHub issue (Mar 2026) shows `HeadersTimeoutError` after ~5 min
  in SDK usage — configure generous client timeouts, or prefer `prompt_async` + SSE.
- **Empty responses with custom OpenAI-compatible providers** were reported (GitHub issue,
  Mar 2026) — validate model IDs/baseURL carefully when wiring BYOK custom providers.
- **Server auth is Basic or nothing** — no bearer tokens; combine with the 127.0.0.1 default bind
  and explicit `--cors` (browsers need CORS entries) for exposure control.
- **One directory per server context.** Sessions are scoped to the project/working directory the
  server was started in (`Session.directory`, `GET /project/current`); the multi-project story is
  via the project endpoints (`GET /project`, instance disposal) rather than a path parameter on prompts.
- TUI-oriented endpoints (`/tui/*`) are no-ops against a headless `opencode serve`.
- `opencode serve` while a TUI is running creates a second, separate server — don't assume a
  shared port 4096 unless you started it yourself.

## 9. Minimal working examples

### 9.1 Raw curl (no SDK) — full lifecycle

```bash
# 0) start server
opencode serve --port 4096 --hostname 127.0.0.1

# 1) health (also shows version)
curl -s http://127.0.0.1:4096/global/health
# {"healthy":true,"version":"..."}

# 2) create a session
curl -s -X POST http://127.0.0.1:4096/session \
  -H 'content-type: application/json' -d '{"title":"adapter test"}'
# -> { "id": "ses_...", ... }   save $SID

# 3) blocking prompt (waits until the assistant finishes — can take minutes)
curl -s -X POST http://127.0.0.1:4096/session/$SID/message \
  -H 'content-type: application/json' \
  -d '{
        "model": { "providerID": "anthropic", "modelID": "claude-sonnet-4-5" },
        "parts": [ { "type": "text", "text": "Say hello and list the files in this directory." } ]
      }'
# -> { "info": { "id": "msg_...", "role": "assistant", ... }, "parts": [ { "type": "text", "text": "..." }, ... ] }

# 4) read transcript anytime
curl -s "http://127.0.0.1:4096/session/$SID/message?limit=100"

# 5) abort a run in flight (from another terminal while 3) is executing)
curl -s -X POST http://127.0.0.1:4096/session/$SID/abort
# -> true

# 6) BYOK: set a provider key at runtime
curl -s -X PUT http://127.0.0.1:4096/auth/anthropic \
  -H 'content-type: application/json' \
  -d '{ "type": "api", "key": "sk-ant-..." }'

# 7) with server password enabled:
curl -s -u opencode:s3cret http://127.0.0.1:4096/global/health
```

### 9.2 Official SDK (`npm i @opencode-ai/sdk`)

```ts
import { createOpencodeClient } from "@opencode-ai/sdk"

const client = createOpencodeClient({ baseUrl: "http://127.0.0.1:4096" })

// events (SSE under the hood) — subscribe BEFORE prompting
const events = await client.event.subscribe()
;(async () => {
  for await (const ev of events.stream) {
    if (ev.properties?.sessionID === session.id) console.log(ev.type, ev.properties)
  }
})()

const session = await client.session.create({ body: { title: "adapter test" } })

// async fire-and-forget; consume via the event stream above
await client.session.prompt({
  path: { id: session.id },
  body: {
    model: { providerID: "anthropic", modelID: "claude-sonnet-4-5" },
    parts: [{ type: "text", text: "Hello!" }],
  },
})

// OR blocking call that resolves with the finished assistant message:
const result = await client.session.prompt({
  path: { id: session.id },
  body: { parts: [{ type: "text", text: "List files." }] },
})
const text = result.data.parts.filter(p => p.type === "text").map(p => p.text).join("")

// abort
await client.session.abort({ path: { id: session.id } })

// BYOK at runtime
await client.auth.set({ path: { id: "anthropic" }, body: { type: "api", key: "sk-ant-..." } })
```

`createOpencode()` (same package) alternatively spawns its own server and hands you a client —
useful in tests but less appropriate for a long-lived adapter.

### 9.3 Raw fetch + SSE in Node (zero dependencies — adapter skeleton)

```js
const BASE = "http://127.0.0.1:4096"
const AUTH = { Authorization: "Basic " + Buffer.from("opencode:s3cret").toString("base64") } // only if OPENCODE_SERVER_PASSWORD set
const H = { "content-type": "application/json", ...AUTH }

// 1. subscribe to SSE first
const res = await fetch(`${BASE}/event`, { headers: AUTH })
const reader = res.body.getReader()
const dec = new TextDecoder()
let buf = ""
;(async () => {
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, i); buf = buf.slice(i + 2)
      const data = frame.split("\n").find(l => l.startsWith("data: "))
      if (!data) continue
      const json = JSON.parse(data.slice(6))
      const ev = json.payload ?? json          // defensive: some versions nest under .payload
      handleEvent(ev)                           // switch on ev.type, read ev.properties
    }
  }
})()

function handleEvent(ev) {
  switch (ev.type) {
    case "server.connected": return console.log("connected")
    case "session.next.text.delta": return process.stdout.write(ev.properties.delta ?? "")
    case "session.idle":        return finish()
    case "session.error":       return fail(ev.properties)
    case "permission.asked":    return respondPermission(ev.properties)
  }
}

// 2. create session
const s = await fetch(`${BASE}/session`, { method: "POST", headers: H, body: JSON.stringify({ title: "adapter" }) }).then(r => r.json())

// 3. async prompt (returns 204 immediately; stream arrives over SSE above)
await fetch(`${BASE}/session/${s.id}/prompt_async`, {
  method: "POST", headers: H,
  body: JSON.stringify({
    model: { providerID: "anthropic", modelID: "claude-sonnet-4-5" },
    parts: [{ type: "text", text: "Say hi" }],
  }),
})

// 4. abort
// await fetch(`${BASE}/session/${s.id}/abort`, { method: "POST", headers: H })

async function respondPermission(p) {
  await fetch(`${BASE}/session/${p.sessionID}/permissions/${p.id}`, {
    method: "POST", headers: H, body: JSON.stringify({ response: "once" }),
  })
}
```

### 9.4 One-shot CLI alternative (no server management)

```bash
opencode run "Explain this repo" --model anthropic/claude-sonnet-4-5 --format json
opencode run --continue            # continue last session
opencode run --attach              # attach to an already-running server
```

Useful for smoke tests; a real adapter should own the server lifecycle via `opencode serve`.

## 10. Adapter recommendations (summary for the implementer)

1. **Lifecycle:** spawn `opencode serve --port <p> --hostname 127.0.0.1`, poll `GET /global/health`
   until healthy; treat `version` in the response as a capability token. Optionally set
   `OPENCODE_SERVER_PASSWORD` and use Basic auth on every request (including the SSE fetch).
2. **Streaming:** always open the SSE `/event` connection *before* `POST .../prompt_async`.
   Parse frames on the JSON `type` (never the SSE `event:` line, it is always `message`);
   defensively unwrap `json.payload ?? json`. Treat `session.idle` (per sessionID) as end-of-turn,
   `session.error` / `metadata.error` as failure, `permission.asked` as a decision point.
3. **Transcript authority:** after `session.idle`, call `GET /session/:id/message` for the final
   messages rather than trusting only the delta stream.
4. **Abort:** `POST /session/:id/abort`; then still wait for `session.idle` before reusing the session.
5. **BYOK:** prefer `PUT /auth/:id` (`{type:"api", key}`) for per-request keys, `auth.json` /
   `opencode auth login` for host-level setup, and `opencode.json` custom-provider blocks for
   OpenAI-compatible gateways. Discover usable models via `GET /config/providers`.
6. **Version drift:** fetch `/doc` (OpenAPI 3.1) at startup and fail fast on unknown critical
   routes; pin and test against known OpenCode versions. Event names have changed before.

## 11. Sources

- Official server docs: https://opencode.ai/docs/server/ (and repo source
  `packages/web/src/content/docs/server.mdx` on the `dev` branch)
- Official SDK docs: https://opencode.ai/docs/sdk/
- Providers / BYOK docs: https://opencode.ai/docs/providers/
- CLI reference (`serve`, `run`, `auth`): https://opencode.ai/docs/cli/
- GitHub repo: https://github.com/sst/opencode — key files:
  `packages/opencode/src/server/routes/instance/httpapi/handlers/global.ts` (SSE handler, verbatim above),
  `packages/opencode/src/event-v2-bridge.ts` (event bridge),
  `packages/opencode/src/session/message.ts` (Message schema),
  `packages/protocol/src/groups/session.ts` (session endpoint group),
  `packages/sdk/js/src/v2/gen/types.gen.ts` (generated types)
- DeepWiki index of sst/opencode (route/type/event extraction)
- Community: cefboud.com "How Coding Agents Actually Work: Inside OpenCode" (Sep 2025,
  client-server architecture), daytona.io SDK walkthrough
- Reported issues (Mar 2026): SDK `HeadersTimeoutError` after ~5 min on long `session.prompt`
  calls; empty `session.prompt()` responses with custom OpenAI-compatible providers
