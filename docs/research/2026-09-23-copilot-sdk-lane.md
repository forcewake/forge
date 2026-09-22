# GitHub Copilot CLI as an interactive driver — copilot-sdk-lane feasibility (verified 2026-09-23)

Feasibility research for a `copilot-sdk-lane`: can the Copilot CLI be driven
INTERACTIVELY (real client, session model, interrupt, steering) the way the
claude-sdk-lane drives `claude-agent-sdk`, the codex-sdk-lane drives
`codex app-server`, and the opencode-sdk-lane drives `opencode serve` — instead
of the scripted batch driver forge ships today (`copilot -p` one-shot,
`ci/templates/copilot.gitlab-ci.yml`)?

Researched against official GitHub docs (Copilot CLI reference, ACP server
reference, programmatic/Actions how-tos, billing docs), the GitHub changelog,
the ACP v1 specification at agentclientprotocol.com, the source-available
`github/copilot-cli` repo, and third-party practitioners who drive the real
binary (Sortie's copilot-cli adapter + working notes, the hollis-labs
`copilotacp` Go adapter with live verification notes, Band, gunbark/Open Agent
View, Hermes, GitHub issues #3942/#4561 with reproductions).

Convention: **[documented]** = stated in official docs/spec/changelog;
**[observed]** = verified in third-party real-world implementations or
reproductions against the real binary; **[inference]** = derived design
conclusion for forge.

---

## 0. Verdict — yes, with a strictly partial capability surface

**Yes, the lane is buildable: `copilot --acp` is a real, documented,
JSON-RPC 2.0 protocol server** (Agent Client Protocol) that the CLI has
exposed since a 2026-01-28 changelog ("public preview") — initialize →
`session/new` → `session/prompt` (full agent turn with streaming
`session/update` notifications) → `session/cancel` (a genuine mid-turn
interrupt), plus `session/load`/`session/resume` over a durable local session
store. [documented] It has been driven end-to-end by third parties against
the real binary. [observed]

The honest capability profile, in forge's closed 7-behavior vocabulary:

- **turn** — yes; a prompt turn reaches a terminal `stopReason`. [observed]
- **native_interrupt** — yes; `session/cancel` stops an in-flight turn within
  milliseconds. [observed]
- **interrupt_outcome_observed** — **degraded today**: the canceled turn's
  `session/prompt` response returns `stopReason: "end_turn"` (indistinguishable
  from completion) instead of the spec-required `"cancelled"` — GitHub issue
  #4561, reproduced on 1.0.80 with a control run. [observed] The outcome is
  observable only via side channels (response arriving promptly after cancel,
  an agent-emitted "Operation cancelled by user" message chunk, no further
  updates). [observed]
- **mid_turn_steer** — **no**: ACP v1 has no steer method and models one
  in-flight prompt per session. [documented] Same posture as the claude and
  opencode lanes, weaker than codex.
- **next_turn_input** — yes by construction: serial `session/prompt` calls
  continue the same session with full context (this is exactly how every ACP
  IDE integration works). [documented] Needs the forge smoke to be claimed.
- **wip_export** — no vendor surface; forge's cooperative working-tree
  checkpoint is vendor-neutral and applies unchanged. [inference]
- **cross_runner_restore** — no: the vendor session store is machine-local
  under `~/.copilot/` (`COPILOT_HOME`); nothing documented exports/restores a
  session on another host. [documented absence]

Net: a copilot-sdk-lane would land **between opencode-sdk-lane and
codex-sdk-lane** — a real session model with native interrupt (better than
nothing, and better-specified than opencode's), but no steering and a currently
untruthful cancel verdict. There is **no vendor SDK** (no equivalent of
`claude-agent-sdk` or a Codex-style app-server README) — the lane talks ACP
directly, which is simple: NDJSON JSON-RPC over the child's stdio.

---

## 1. What Copilot CLI is in late 2026 (topic 1)

- The standalone `copilot` binary (`npm i -g @github/copilot`, Homebrew
  `copilot-cli`, WinGet, `copilot-install`, direct release binaries); Node 22+
  for the npm route. GA **2026-02-25** for all paid Copilot plans (Free tier
  can install it too). The old `gh copilot` suggest/explain extension is
  deprecated; `gh copilot` now installs/forwards to the new CLI. [documented]
- It is a terminal-native coding agent: reads the repo, edits files, runs
  shell, calls GitHub (built-in GitHub MCP server), custom MCP
  (`~/.copilot/mcp-config.json` — what the batch lane already provisions),
  skills, hooks, custom agents (`.agent.md`), LSP. [documented]
- Interactive modes: **standard / plan / autopilot** (Shift+Tab or
  `--autopilot`), `/fleet` parallel subagents, `&`-prefix/`/delegate` handoff
  to the cloud agent. [documented]
- Sessions are durable and resumable: `copilot --resume [id|name|prefix]`,
  `--continue` (context-aware: prefers branch/repo/cwd relevance), `--name`,
  `/session`. [documented] The store is **global, not path-keyed** — a session
  id resumes from any folder; state lives under `~/.copilot/session-state/`
  with a cross-session `session-store.db` index and per-session `events.jsonl`
  replay log, plus workspace sidecars (`workspace.yaml`, `plan.md`,
  `checkpoints/`, `session.db`). [observed: Daintree, gunbark, bundle-internals
  analysis; sidecar layout is internals, not a stable contract]
- Version ground truth (checked 2026-09-23): npm `latest` =
  **1.0.88** (prerelease 1.0.89-0). forge's `DEFAULT_DRIVER_VERSIONS` pins
  `copilot: 1.0.86` (`src/forge/harnesses/script_render.py`), which was npm
  latest at the 2026-09-17 slice. The repo `github/copilot-cli` is public and
  source-available (11k+ stars; license NOASSERTION — read it for ground
  truth, don't assume OSS rights). [observed]

Not relevant to the lane: the TUI itself (alt-screen, mouse, themes). The lane
never renders it — ACP mode runs the same session machinery headless.
[observed]

---

## 2. The two programmatic surfaces (topics 2 + 3)

| Surface | Start | Shape | Status |
|---|---|---|---|
| **ACP server** | `copilot --acp [--stdio \| --port N]` | JSON-RPC 2.0, NDJSON, bidirectional; sessions, streaming updates, permission requests, cancel | documented (public preview); the lane's target |
| **Programmatic `-p`** | `copilot -p "…" [-s] --output-format json` | one-shot subprocess; NDJSON event stream on stdout; terminal `result` event | documented flags, observed event vocabulary (Sortie) |
| Hidden JSON-RPC server | `--server` / `--headless` / `--ui-server` | `session.create/resume/send/getMessages/list/delete`, `session.event`, `shell.output`, external-tool/elicitation/sampling bridges | [observed, minified-bundle analysis] — undocumented, unstable; do not build on it |

### 2.1 The `-p --output-format json` event stream — [observed, Sortie]

One JSON object per line on stdout: `assistant.message_delta`,
`assistant.message` (`outputTokens`, `content`, `toolRequests`),
`assistant.turn_start/end`, `tool.execution_start` / `tool.execution_complete`
(`toolCallId`, boolean `success`), `session.warning/info/task_complete`,
MCP status events, and the terminal `result` event carrying `sessionId`,
`exitCode`, and `usage` (`premiumRequests`, `totalApiDurationMs`,
`sessionDurationMs`, `codeChanges.linesAdded/linesRemoved/filesModified`).
Notable honesty gaps: **no input token counts anywhere on the stream, no model
name in payloads**; per-session token truth lives in the on-disk session
journal (session-cumulative, `modelMetrics` in the shutdown record), and
per-request input/output tokens exist only in the CLI's **OpenTelemetry
export**. Headless multi-step work needs the autonomous-continuation posture
(`--autopilot` + a step cap `--max-autopilot-continues`, plus `--no-ask-user`
to close the ask-the-human path). Sortie's adapter is a working
fork-per-turn design on exactly this surface (see §10) — but it is a batch
shape: one process per turn, no live interrupt, session id learned only from
the terminal `result` event. **ACP supersedes it for an interactive lane.**

### 2.2 Why ACP and not the JSONL mode — [inference]

ACP gives the lane everything the JSONL mode cannot: an immediate
`sessionId` at `session/new` (not at turn end), a first-class cancel, live
permission requests it can answer, slash-command invocation (including
`/usage`), and a spec to develop against instead of an undocumented event
vocabulary. The JSONL mode remains the right shape for the existing scripted
batch lane; nothing there needs to change.

---

## 3. The ACP wire (topic 2, primary)

### 3.1 Transport and handshake — [documented]

```
copilot --acp            # stdio (default; recommended for a subprocess lane)
copilot --acp --port N   # TCP; docs say loopback 127.0.0.1 by default
```

- NDJSON JSON-RPC 2.0 on the pipe/socket; stdio server dies with its parent's
  closed stdin (natural lane teardown). TCP mode exists for
  longer-lived/remote hosts; note a discrepancy — the docs say loopback bind,
  the hollis-labs Go adapter observed a wildcard bind with no `--host` flag,
  and Band front-ends stdio with `socat` because they read it as loopback-only.
  Treat bind scope as unverified; a forge lane uses stdio and the question
  dissolves. [observed conflict]
- Handshake: `initialize` (`protocolVersion: 1`, `clientCapabilities`,
  `clientInfo`) → response with `agentCapabilities` (third parties observed
  `loadSession: true` and `sessionCapabilities.close`), `agentInfo` (e.g.
  "Copilot 1.0.80 (protocol v1)" — record it for provenance), `authMethods`.
  [observed]
- **Server-start options apply to EVERY session** the server later creates —
  `session/new` only carries `cwd` + `mcpServers`: `--available-tools=…`,
  `--excluded-tools=…`, `--effort=low|medium|high|xhigh|max`. Whoever launches
  the server owns the posture — exactly the lane's model. [documented]
- BYOK providers (`COPILOT_PROVIDER_*` env / `COPILOT_PROVIDERS_CONFIG`) run
  ACP sessions without GitHub login. [documented]

### 3.2 Session model — [documented spec + observed]

- `session/new {cwd, mcpServers}` → `{sessionId}` — the id exists BEFORE any
  turn (better than `-p` mode and than the opencode lane's
  id-at-completion). The session's effective filesystem root set is `cwd`
  (plus `additionalDirectories` where advertised). [documented]
- `session/load {sessionId, cwd, mcpServers}` (requires `loadSession`) —
  replays the conversation as `session/update` notifications, then responds;
  `session/resume` (requires `sessionCapabilities.resume`) reconnects without
  replay; `session/close` cancels ongoing work and frees resources without
  deleting persisted history. [documented spec] The bundle additionally
  implements `session/list` and `session_fork` on the ACP adapter.
  [observed, internals]
- Persistence is the CLI's local store (§1): `session/load` by id works only
  where the store lives — same host, or wherever `COPILOT_HOME` was shipped.
  **No documented portable session export.** [documented absence]

### 3.3 Client-side duties — [documented]

The client must implement `session/request_permission` (respond by selecting
one of the offered `optionId`s — allow once / allow always / reject once /
reject always — or `cancelled`) and `session/update` handling; optional
`fs.readTextFile`/`fs.writeTextFile`/`terminal/*`/elicitation capabilities can
be declared false. The reference TS client is `@agentclientprotocol/sdk`; the
wire is plain enough for a dependency-free Python client (JSON lines +
request-id correlation), matching how `codex_app.py` speaks JSON-RPC itself.
[inference]

### 3.4 Slash commands over ACP — [documented]

Send `"/usage"` / `"/context"` / `"/model"` etc. as an ordinary single-block
text prompt; informational commands answer without invoking the model, action
commands (`/plan`, `/review`) start the agent task. The authoritative set
arrives via the `available_commands_update` notification. Interactive-only
commands (`/resume`, `/login`, `/diff`, `/tasks`, …) do not run over ACP.

---

## 4. Turns: prompt, stopReason, cancel — and the cancel bug

- `session/prompt {sessionId, prompt:[{type:"text",text}]}` blocks until the
  turn resolves; `session/update` notifications stream in between
  (`agent_message_chunk`, `agent_thought_chunk`, `tool_call` /
  `tool_call_update` with pending→in_progress→completed statuses, `plan`,
  `user_message_chunk`; the spec also defines an optional `usage_update`
  `{used, size, cost?}` — **no third party reports Copilot emitting it**,
  treat per-turn token/cost over ACP as absent until a smoke proves
  otherwise). The response carries `stopReason`:
  `end_turn | cancelled | max_tokens | max_turns | refusal`.
  [documented spec, observed lifecycle]
- `session/cancel` is a notification and **genuinely interrupts**: the
  hollis-labs live run cut a long generation ~3 s after the notification with
  an agent-emitted "Info: Operation cancelled by user" chunk; the #4561 repro
  measured a stop **26 ms** after the notification (control run: 15.5 s, 31
  updates; cancelled run: 5.2 s, 8 updates). [observed]
- **The bug (#4561, still open as of the research slice):** the interrupted
  turn answers `stopReason: "end_turn"`, not `"cancelled"` — on 1.0.80; the
  sibling harnesses (opencode 1.18.18, `@agentclientprotocol/claude-agent-acp`
  0.49.0) answer `cancelled` correctly. Consequences named by the reporter are
  precisely forge's problems: a TTL watchdog cannot mark timeout from the
  protocol response, and a truncated turn reports the same status as a clean
  run. [observed]
- Known-bug catalog relevant to the lane [observed, GitHub issues]:
  - **#3942** — `--agent <name>` is IGNORED in `--acp` mode (the flag is
    dropped in `startServerMode`; no `session/set_config_option` exists), so
    custom agents can only be reached as subagents via prompt text.
  - **Hermes #17284** — DENYING a `session/request_permission` mid-turn ends
    the turn (`end_turn`) with **zero agent message chunks** (thoughts only).
    The lane's permission responder must never deny a tool the task needs;
    mechanical denials (commit/push) belong in server-start flags instead.
  - ACP itself is "public preview and subject to change" — pin the binary and
    re-run the smoke on upgrade (the same vendor-drift doctrine as
    `live_registrations.py`). [documented]

---

## 5. Steering: absent mid-turn, native next-turn

ACP v1 defines no steer/inject method; the prompt turn is a single request
whose response ends the turn, and reference clients refuse a second prompt
while one is in flight (`ErrTurnInFlight` in the Go adapter). [documented
spec shape, observed] Whether Copilot's server would queue a concurrently
sent `session/prompt` is undefined and untested — the lane must not rely on
it. [inference]

Therefore the steering bridge's posture on this lane mirrors the opencode
lane: mid-turn commands are journaled as refusals, and input lands as
**next_turn_input** — the operator's text becomes the next `session/prompt`
after the current turn's response. That is a real capability (the session
keeps context; no `--resume` gymnastics), just not steering. [inference]

---

## 6. Permissions and tool governance in ACP mode

- Server-start scoping: `--available-tools` / `--excluded-tools` are
  documented ACP-mode options; `--allow-all-tools` is used with `--acp` in
  the wild (Band's socat front runs `copilot --acp --allow-all-tools`).
  [documented + observed] The CLI's general rule — **deny beats allow beats
  allow-all** — is documented for the binary; whether `--deny-tool` rides
  into ACP-mode server starts is NOT explicitly documented → live-check
  before relying on it; fallback is `--available-tools` allowlisting.
  [documented + inference]
- Live-observed permission shape: a non-mutating `pwd` shell call produced
  exactly one `session/request_permission` with tool kind `execute` and
  allow-once/allow-always/reject-once options. [observed, hollis-labs]
- Lane posture (mirrors the batch lane): start
  `copilot --acp --allow-all-tools --deny-tool 'shell(git commit)' --deny-tool 'shell(git push)'`
  with an automated allow-once responder for anything that still asks, and
  rely on the proposal-only lane's real boundary (no push credential, push
  FORBIDDEN at the remote, artifact-validated output) — the same doctrine the
  claude-code batch lane adopted when it moved to `bypassPermissions`.
  [inference]

---

## 7. Credential model for CI (topic 4)

- Token precedence: `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` >
  stored login (system keychain, else plaintext under `~/.copilot/` or
  `COPILOT_HOME`) > `gh auth`. [documented]
- Supported token types: **fine-grained PATs (v2, `github_pat_` prefix) with
  the "Copilot Requests" permission**, Copilot-CLI-app OAuth tokens (`gho_`),
  gh-app OAuth tokens, and GitHub App user-to-server tokens (`ghu_`).
  **Classic PATs (`ghp_`) are NOT supported and fail silently** — the CLI
  falls through all token variables and reports no valid credential. This is
  exactly what forge's batch template already documents
  (`COPILOT_GITHUB_TOKEN` masked/protected CI variable). [documented +
  observed, Sortie]
- Login flows for setup: web / device-code / `copilot login --with-token`;
  `--host` / `GH_HOST` for GHES; in Codespaces/CI the device flow is the
  default. [documented]
- Subscription: a Copilot seat (or Free) covers model spend — no per-provider
  API key. BYOK (`COPILOT_PROVIDER_*`) opts those surfaces out of GitHub
  billing onto the provider's meter, and works in ACP mode without GitHub
  login. [documented]
- The lane needs nothing beyond the batch lane's `COPILOT_GITHUB_TOKEN`
  fine-grained PAT; ACP mode reads the same environment. [inference]

---

## 8. Usage / cost observability (topic 4)

What a lane receipt can honestly carry TODAY:

- **Per-turn over ACP: nothing verified.** No `usage_update` emission
  reported by any practitioner; the `-p` JSONL stream carries output tokens
  only (no input, no model); the on-disk session journal is session-cumulative
  with `modelMetrics`; OTel export carries per-request input/output tokens
  for operators who wire the env. Receipt doctrine: `completeness:
  "unknown"`, tokens only if a verified source appears — the same stance as
  the opencode lane. [observed + inference]
- **Documented session-level surface:** the `/usage` slash command runs over
  ACP as an informational prompt (no model invocation). Output is a text
  graph — parse-worthy only opportunistically, but it is a real, documented
  per-session usage read the other lanes lack an equivalent of.
  [documented]
- **Billing model (context):** since 2026-06-01 Copilot meters in **AI
  Credits** (1 credit = $0.01) at per-model token rates (legacy annual plans
  keep premium-request multipliers; CLI = 1 request per prompt × multiplier —
  autonomous tool calls do not count). [documented]
- **Programmatic org-level accounting (post-hoc, not lane receipts):** Copilot
  usage metrics API `…/copilot/metrics/reports/organization-1-day` exposes
  per-user `ai_credits_used` (daily aggregate, ~2-day lag, no per-surface
  breakdown); the billing usage REST API
  (`/enterprises/{e}/settings/billing/ai_credit/usage` and user variants)
  returns per-model `usageItems` (sku, credits, net amount) for the past 24
  months — org-admin permissions required. Useful for reconciliation, not for
  per-run meta. [documented]

---

## 9. The 7-behavior comparison (forge vocabulary)

Rows for the three live lanes are `LIVE_OBSERVED_CAPABILITIES`
(`src/forge/adaptive/drivers/live_registrations.py`, smoke 2026-09-21); the
copilot column is this research's projection — **nothing is claimable until
the forge smoke records it against the pinned binary**.

| behavior | claude-sdk (2.1.273) | codex-app (0.153.4) | opencode-server (2.0.10) | copilot ACP (projected) |
|---|---|---|---|---|
| `turn` | observed | observed | observed | **expected** — turn → `stopReason` (third-party-verified wire) |
| `native_interrupt` | observed | observed | observed | **expected** — `session/cancel` cuts the turn in ms (third-party-verified) |
| `interrupt_outcome_observed` | observed (ResultMessage seen; turn RAN ON) | observed (`turn/completed(interrupted)`) | not observed | **blocked today** — `stopReason` lies (#4561); side-channel only |
| `mid_turn_steer` | not observed (next-turn buffering) | observed (`turn/steer`) | not observed | **absent** — no protocol method |
| `next_turn_input` | observed | (steers instead) | not observed | **expected** — serial prompts continue the session |
| `wip_export` | not observed | not observed | not observed | not observed — forge-side tree checkpoint only (vendor-neutral) |
| `cross_runner_restore` | not observed | not observed | not observed | not observed — vendor store is machine-local (`COPILOT_HOME`) |

Ecosystem note (for the harness-comparison question): among the four
harnesses, **copilot is the only one with a NATIVE standard protocol server**
(`--acp`); Claude Code needs the `@agentclientprotocol/claude-agent-acp`
adapter, Codex needs `@zed-industries/codex-acp`, opencode ships `opencode
acp`, Gemini `--experimental-acp`. [observed] Forge's own lanes instead use
each vendor's first-party surface (SDK / app-server / HTTP), which remains the
right call where it exists — for Copilot, ACP IS the first-party surface.
[inference]

---

## 10. Precedent (topic 5)

- **Sortie** (`docs.sortie-ai.com/reference/adapter-copilot` +
  `sortie-ai/sortie/docs/copilot-adapter-notes.md`, updated 2026-09-11) — the
  strongest CI-agent precedent: a production Go orchestrator driving the real
  binary per turn over `-p --output-format json`. Design lessons that transfer
  verbatim: version canary + credential preflight before any turn; the
  stand-in-binary/fake-binary trap in tests; graceful kill (SIGTERM → 5 s →
  SIGKILL to the process GROUP — MCP children outlive the direct child);
  session-id discovery discipline (their JSONL mode learns it only from the
  terminal event; `--continue` fallback is unsafe with concurrent agents in
  one workspace — ACP's `session/new`-immediate id removes the problem);
  journal-based usage with a baseline-snapshot rule and a "measured vs
  unknown" flag; "exit zero having done nothing" is a real failure mode
  (unparseable config, silent auth failure — diagnose via stderr). [observed]
- **Copilot coding agent ("cloud agent") is a DIFFERENT surface** — GitHub-
  hosted, Actions-powered ephemeral environment, started by assigning an issue
  (UI/REST/GraphQL `Agent Assignment`: `target_repo`, `base_branch`,
  `custom_instructions`, `custom_agent`, `model`) or `@copilot` on a PR; it
  opens a draft PR itself. It is NOT the CLI's protocol and gives forge no
  mid-turn control; forge's model (proposal-only lane, trusted publisher)
  already covers that flow better on its own lanes. CLI↔cloud handoff exists
  (`&` prefix delegates; "Continue in Copilot CLI" pulls a cloud session
  local) but is a UI/workflow feature, not a driver API. [documented]
- **Copilot "agent mode"** is the IDE (VS Code/JetBrains) interactive agent —
  same harness family, no separate public API; the API-shaped surfaces are
  the ACP server (this doc) and the cloud agent (above). [documented]
- **ACP ecosystem driving copilot today**: Xcode 27, JetBrains (via the ACP
  extension), Pulsar, Band (Python adapter spawning `copilot --acp` stdio +
  loopback MCP tool injection), gunbark's Open Agent View (session/list,
  `session/close` semantics, `~/.copilot/session-state/` layout), Hermes
  (acp:// delegate registry), protoAgent. A forge lane is a peer of these,
  with stricter honesty requirements. [observed]

---

## 11. Ranked recommendations

1. **Build the lane on `copilot --acp --stdio`** (not `-p` JSONL, not the
   hidden `--server`). Driver client shape mirrors `claude_sdk.py`:
   `start_session` = spawn + `initialize` + `session/new` (record
   `agentInfo` for provenance) → `session/prompt` → drain `session/update`
   (non-consuming buffer for the steering bridge, like the codex lane) →
   terminal `stopReason` → `session/cancel` on budget expiry → close stdin
   (the stdio server dies with it). Register as `copilot-sdk-lane`;
   `DRIVER_SDK_OF["copilot-sdk-lane"] = "copilot-acp"` once a smoke exists.
   [inference]
2. **Interrupt verdict must be side-channeled until #4561 ships**: the lane
   keeps its own cancel ledger (sent-cancel timestamp) and classifies a
   prompt response that arrives after a sent cancel as `interrupted_by_lane`
   (or the smoke's observed spelling), never as `completed` — the exact
   watchdog guidance the issue gives. Do NOT claim
   `interrupt_outcome_observed` in `LIVE_OBSERVED_CAPABILITIES` on a version
   whose wire says `end_turn`; re-test the pinned version each bump (the
   claude lane already models "interrupt accepted, turn ran on" honestly —
   this is the inverse: turn stopped, wire says completed). [inference]
3. **Pin + smoke before wiring**: align `DEFAULT_DRIVER_VERSIONS["copilot"]`
   (1.0.86 today; registry latest 1.0.88) with the smoke's binary, and record
   `turn`, `native_interrupt`, `next_turn_input` as observed only on that
   exact version string (NXT-27 doctrine). Extend
   `scripts/driver_live_smoke.py` with a copilot-acp case: tiny turn, a
   cancel-with-control (assert prompt-response latency ≪ control turn
   duration + the "cancelled by user" chunk), a serial second prompt.
   [inference]
4. **Permissions**: server-start posture
   (`--allow-all-tools` + mechanical denies, live-checking that `--deny-tool`
   binds in ACP mode; fallback `--available-tools`), plus an allow-once
   `request_permission` responder that NEVER denies mid-turn (Hermes #17284:
   a denial ends the turn with empty output). Keep the batch lane's
   credential posture (`COPILOT_GITHUB_TOKEN` fine-grained PAT,
   "Copilot Requests"; classic `ghp_` fails silently). [inference]
5. **Usage receipt: absent, honestly.** No tokens on the ACP wire (verify
   `usage_update` absence in the smoke; if it appears, wire it with
   `completeness` reflecting it); optionally parse `/usage` post-turn as
   best-effort telemetry; billing REST/metrics APIs are reconciliation-only
   (org-scope, days lag). Same doctrine as the opencode lane's
   `session.usage.updated`-absent case. [inference]
6. **Do not chase** `session/load` cross-runner restore (vendor store is
  machine-local; shipping `COPILOT_HOME` is a research project with no
  documented contract) or the hidden `--server` JSON-RPC surface (unstable,
  undocumented). The forge WIP checkpoint already provides tree-level
  continuity, as it does for every lane. [inference]

---

## 12. Known limitations (the honest list)

1. ACP is **public preview** — "subject to change" (GitHub's own note); the
   pinned-version smoke is not optional. [documented]
2. `stopReason: "end_turn"` after cancel (#4561, 1.0.80) — the lane's
   interrupt outcome is side-channel until fixed. [observed]
3. `--agent` ignored under `--acp` (#3942) — custom agents unreachable as
   the primary agent in a lane. [observed]
4. No mid-turn steer — protocol-level absence, not a bug. [documented]
5. No verified per-turn token/cost emission over ACP; `-p` stream lacks
   input tokens and model names; journal/OTel are workarounds with their own
   caveats (cumulative, SSH-skipped, opt-in env). [observed]
6. TCP bind scope disputed (docs loopback vs observed wildcard); stdio
   avoids it. [observed conflict]
7. `session/new` cannot set tool filters or effort — posture is
   server-launch-time only (a lane re-spawn is the only per-run posture
   change, which is how the lanes already work). [documented]
8. Everything in §1's persistence layout (sidecars, `session-store.db`) is
   internals — read for diagnosis, never depended on. [observed]

---

## 13. Minimal driver-client sketch (Python over stdio) — [inference]

```python
import json, subprocess, threading, queue

proc = subprocess.Popen(
    ["copilot", "--acp", "--stdio",
     "--allow-all-tools",
     "--deny-tool", "shell(git commit)",
     "--deny-tool", "shell(git push)"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
events: queue.Queue[dict] = queue.Queue()

def reader():
    for line in proc.stdout:                 # one JSON-RPC message per line
        events.put(json.loads(line))
threading.Thread(target=reader, daemon=True).start()

def send(msg): proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
def rpc(method, params, id_):
    send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
    # caller drains `events`; responses match on id, notifications on method

rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                   "clientInfo": {"name": "forge", "version": "0.1.0"}}, 0)
session = rpc("session/new", {"cwd": repo_dir, "mcpServers": []}, 1)
session_id = session["sessionId"]             # exists BEFORE any turn
rpc("session/prompt",
    {"sessionId": session_id,
     "prompt": [{"type": "text", "text": task}]}, 2)
# drain session/update (agent_message_chunk / tool_call / tool_call_update)
# until the id=2 response: stopReason end_turn -> completed;
#   refusal/max_tokens/max_turns -> failed(reason)
# budget expiry: send({"jsonrpc":"2.0","method":"session/cancel",
#                      "params":{"sessionId": session_id}})
#   then classify from the response + the lane's own cancel ledger (#4561)
# client-side requests (id + method session/request_permission):
#   answer {"id": <id>, "result": {"outcome": {"outcome": "allow_once"}}}
#   — never deny mid-turn (ends the turn with empty output)
# teardown: proc.stdin.close() -> stdio server exits with the pipe
```

---

## Sources

- Official: [Copilot CLI ACP server reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) · [CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference) · [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [Running Copilot CLI programmatically](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically) · [Automating with GitHub Actions](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-with-actions) · [Copilot CLI GA changelog (2026-02-25)](https://github.blog/changelog/2026-02-25-github-copilot-cli-is-now-generally-available/) · [ACP public preview changelog (2026-01-28)](https://github.blog/changelog/2026-01-28-acp-support-in-copilot-cli-is-now-in-public-preview/) · [Premium requests / usage docs](https://docs.github.com/en/copilot/managing-copilot/monitoring-usage-and-entitlements/about-premium-requests) · [Billing usage REST](https://docs.github.com/en/rest/billing/usage) · [Starting Copilot sessions (cloud agent)](https://docs.github.com/copilot/how-tos/agents/copilot-coding-agent/asking-copilot-to-create-a-pull-request)
- Spec: [ACP v1 overview](https://agentclientprotocol.com/protocol/v1/overview) · [session setup (load/resume/close)](https://agentclientprotocol.com/protocol/v1/session-setup) · [prompt turn (cancel, stopReason, usage_update)](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- Source: [github/copilot-cli](https://github.com/github/copilot-cli) (source-available; compare links per release) · npm `@github/copilot` (registry: latest 1.0.88, 2026-09-23)
- Live practitioners: [Sortie Copilot CLI adapter reference](https://docs.sortie-ai.com/reference/adapter-copilot) · [Sortie adapter working notes](https://github.com/sortie-ai/sortie/blob/main/docs/copilot-adapter-notes.md) · [hollis-labs copilotacp (live-verified 2026-08-21)](https://pkg.go.dev/github.com/hollis-labs/go-agent-wrapper/adapters/copilotacp) · [Band Copilot CLI integration](https://docs.band.ai/integrations/sdks/tutorials/github-copilot-cli) · [gunbark Open Agent View notes](https://gunbark.dev/content/52c32980-65c6-47c4-bb2b-16c7018fe7f7) · [Hermes ACP agent registry PR](https://github.com/NousResearch/hermes-agent/pull/68222/files) · [Hermes #17284 (deny ends turn)](https://github.com/NousResearch/hermes-agent/issues/17284) · [protoAgent coding-agents guide](https://agent.protolabs.studio/docs/guides/coding-agents.html)
- Issues with reproductions: [github/copilot-cli#4561 — stopReason end_turn after cancel](https://github.com/github/copilot-cli/issues/4561) · [github/copilot-cli#3942 — --agent ignored in ACP mode](https://github.com/github/copilot-cli/issues/3942)
- Billing/pricing analyses: [GitHub AI Credits coverage (michalhajduch.cloud)](https://michalhajduch.cloud/stop-blindly-paying-for-github-copilot-here-is-how-to-see-every-dollar-using-api) · [Metrics API ai_credits_used (byteiota)](https://byteiota.com/github-copilot-metrics-api-track-per-user-ai-spend-now) · [Usage API gaps (usagebox)](https://usagebox.com/articles/github-ai-credits-tracking-usage-api-2026)
- Internals (bundle analysis, unstable-by-definition): [genisisiq Copilot CLI — embedded server/ACP](https://copilot-cli.genisisiq.com/01-runtime-lifecycle/embedded-server-acp-protocol) · [session end-to-end](https://copilot-cli.genisisiq.com/04-sessions-persistence-remote/conversation-session-end-to-end) · [telemetry/update/shutdown](https://copilot-cli.genisisiq.com/05-hosted-agent-ops/telemetry-update-and-shutdown)
