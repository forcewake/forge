# Durable agent sessions across runner restarts — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: official Claude Agent SDK /
> Claude Code session docs, Codex CLI session-architecture write-ups,
> OpenCode session docs + storage analyses, Temporal/Restate/Inngest/DBOS
> agent-orchestration literature; fetched 2026-09-22. Companion to the
> existing [durable-execution.md](durable-execution.md) (which covers the
> Postgres controller patterns — not duplicated here). Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

forge's durable core (ADR-0004/0005/0017) survives worker crashes at six
checkpoints — but a *durable controller* is not a *resumable agent*: after
a runner restart the controller knows where the run stood, while the
harness's conversational context (files read, dead ends explored, decisions
made) lived in a process that is gone. The adaptive plan already states
the right principle — "native session IDs are not the durable task
identity… a portable checkpoint includes source snapshots, WIP, decisions,
active plan, pending questions and command offsets; native session files
are optional acceleration data" (architecture plan §8, EXE-03) — and P2's
gate demands "a paused job resumed on another runner." Today forge has
live proof of turn/steering/interrupt per driver
([2026-09-21 driver smokes](../evaluation/2026-09-21-drivers/README.md))
but **no live cross-runner resume and no codified recipe for carrying
vendor session state inside a portable checkpoint**. [observed from repo;
inference for the gap]

## Findings

### 1. Claude Code / claude-agent-sdk: sessions are files, indexed by path

[documented] ([code.claude.com SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions))

- The SDK auto-persists every session (prompts, tool calls, tool results,
  responses) to disk; returning to a session restores full context.
- Three resume modes: `continue_conversation=True` / `continue` (most
  recent session **in the current directory**), `resume=<session-id>`
  (specific session — required in multi-session apps), and **fork**
  (`fork_session` — new session ID starting from a copy of the history;
  original untouched; use for trying a different direction).
- `persistSession: false` for stateless one-shots (TypeScript; Python
  always persists).
- Session files live under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`;
  **resume across hosts = physically move the file and restore it at the
  same path, and the cwd must match** — storage is indexed by absolute
  project path, so moving the repo directory orphans old sessions.
- The CLI surface mirrors this: `claude --continue`, `claude --resume
  <name-or-id>`, `/resume`, `claude --continue --fork-session`; transcript
  JSONL entry format is explicitly **internal and may change between
  releases** — don't build parsers on it.
- Gotchas: session-scoped permission approvals do **not** carry over on
  fork; two processes appending to the same non-forked session corrupt it;
  resume restores conversation state, not the world (files/credentials may
  have changed — re-check repository state first).
  ([ClaudeWorld session management](https://claude-world.com/tutorials/s16-session-storage/))

[inference] For forge lanes: the workspace path of a lane must be
**stable across runner restarts** (or the session file relocated and the
cwd re-created identically), or claude resume silently finds nothing.
This is a concrete checkpoint-artifact requirement, not a nicety.

### 2. Codex: two-layer persistence (JSONL rollouts + SQLite index), headless resume, fork modes

[documented] ([Codex session history](https://codex.danielvaughan.com/2026/06/01/codex-cli-session-history-local-search-rollout-format-knowledge-mining),
[paginated thread history v0.145](https://codex.danielvaughan.com/2026/07/23/codex-cli-paginated-thread-history-sqlite-session-resume-search-memories),
[lifecycle](https://codex.danielvaughan.com/2026/06/08/codex-cli-session-lifecycle-archive-resume-fork-rollout-persistence-management))

- Sessions are append-only **JSONL rollout files** at
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (root moves wholesale if
  `CODEX_HOME` is set) — the source of truth — plus a **SQLite state DB**
  (`state_5.sqlite`) with thread metadata for listing/search/pagination;
  v0.145 replaced load-everything pickers with cursor pagination, v0.146
  added named/pinned threads.
- Resume: `codex resume` (picker), `codex resume --last [--all]`,
  `codex resume <id>`, and — the CI-relevant form — **`codex exec resume
  --last "<follow-up>"`** for non-interactive continuation, prompts
  pinnable by name (`codex exec --name "nightly-lint-pass"`).
- Fork: `codex fork --last` — three modes: full history, truncated to
  last N turns (`truncate_rollout_to_last_n_fork_turns`), or
  interrupted-snapshot with a `<turn_aborted>` marker; forked threads get
  new UUIDv7 IDs. Archive/unarchive lifecycle exists.
- `/compact` summarizes the conversation to free context — the sanctioned
  way to keep long threads small.
- App-server surface: threads survive process restarts; the
  forge adapter's `thread/resume` maps to this store.
  [documented in the codex-app-server research doc; live-verified turn
  lifecycle 2026-09-21]

### 3. OpenCode: sessions are rows in SQLite, export/import is a first-class command

[documented] ([OpenCode session management guide](https://opencode.runman.ai/en/2-daily/02-sessions.html),
[txcript opencode format analysis](https://github.com/skillsynchq/txcript/blob/main/docs/formats/opencode.md))

- Modern layout is a single SQLite DB at
  `~local/share/opencode/opencode.db` (Drizzle ORM; `session`/`message`/`part`
  tables; older per-file JSON under `storage/` is migrated). Sessions are
  keyed to a project/directory.
- Portability is built in: `opencode export <session-id>` → JSON
  `{info, messages:[{info, parts:[…]}]}`; `opencode import <file-or-share-url>`
  rehydrates (share links `https://opncd.ai/s/…` work as import sources);
  `/export`, `/share`, `/compact`, `/undo`, `/redo` in-TUI.
- Native continuation flags: `opencode --session <id>` and
  `opencode --session <id> --fork` (no separate resume verb). The live
  correction in forge's opencode research (v2.0.10 API) already pins the
  server-side session endpoints; export/import is the cross-machine path.
- Third parties (Reinstate, txcript, opencode2claude) demonstrate that the
  export shape is stable enough to build cross-harness session portability
  on — including Claude↔OpenCode transcript conversion.
  [observed]

### 4. How orchestrators model long-running agent work (pattern reference — forge keeps the Postgres controller)

[documented] ([reactify durable agents 2026](https://reactify-solutions.com/articles/durable-ai-agents-2026),
[particula.tech Temporal vs Inngest vs Restate](https://particula.tech/blog/durable-execution-ai-agents-temporal-inngest-restate),
[theagentecosystem](https://theagentecosystem.com/blog/durable-execution-ai-agent-runtimes))

- Who runs on what: **OpenAI's Codex web agent, Replit Agent 3 and
  Cursor's long-running automation run on Temporal**; DBOS is the
  Postgres-native option; Restate offers journaled steps + awakeables;
  Inngest Agent Kit is the serverless-shaped one.
- The universal pattern: the agent loop is a workflow; every LLM call,
  tool call and HTTP request is a journaled activity/step whose result is
  recorded once and replayed from history on recovery (never re-billed,
  never re-fired); **signals / `waitForEvent` / awakeables** model
  human approvals with dedup IDs; **durable timers** wait hours-to-days
  consuming zero compute; **update handlers** are the sanctioned steering
  surface (mutations on a live workflow, validated server-side).
- Pitfalls worth stealing as design notes: large LLM payloads saturate
  workflow history (offload to external storage via payload codecs —
  forge's content-addressed artifact store is exactly this); non-determinism
  inside workflow logic breaks replay (keep nondeterminism inside
  activities); four layers each shipping instrumentation double-counts
  tokens.
- [observed] forge's runbook decision record (§7) already defers
  Temporal adoption behind measurable criteria — this research found
  nothing that overturns that; the signal/awakeable/update-handler
  vocabulary maps cleanly onto the existing Mailbox + epoch design.

### 5. The synthesis: what a forge checkpoint must carry per driver

[inference] Combining vendor mechanics with EXE-03's portable-checkpoint
contract:

| Concern | claude-sdk | codex-app | opencode-server |
|---|---|---|---|
| Durable identity | session UUID ↔ `.jsonl` under encoded cwd | thread ID ↔ rollout JSONL (+ SQLite index) | `ses_*` row in opencode.db |
| Cross-host resume | copy session file; recreate identical cwd | copy rollout (+ `CODEX_HOME` state or accept index rebuild); `codex exec resume <id>` | `opencode export` → JSON → `opencode import` on target |
| Size control | `/compact` before checkpoint | `/compact` (turn-level compaction) | `/compact` |
| Fork semantics | `--fork-session` (approvals do NOT carry) | `codex fork` (full/truncated/interrupted) | `--session <id> --fork` |
| Stability caveat | transcript format internal, changes between releases | rollout is append-only and stable; index schema versioned (`state_5`) | export shape is the stable contract |

Plus the driver-independent parts the plan already names: workspace WIP
(git state / uncommitted diff), SnapshotSet OIDs, active PlanRevision,
pending questions, command offset, publication epoch.

## Concrete recommendations (ranked by effort/impact)

1. **Codify "vendor session state in the checkpoint" as three tested
   recipes (medium effort, high impact — it is the P2 gate).** For each
   driver: what files/exports the ExecutionCheckpoint references, how the
   target runner reconstructs the cwd/workspace, and a contract test that
   kills a lane mid-turn and resumes on a *second* runner with context
   intact (the driver-smoke `--e2e` harness is the natural place to grow
   this). Claude's path-indexing and Codex's `CODEX_HOME` relocation are
   the two gotchas the recipes must pin. [inference]
2. **Stable lane workspace paths (low effort, enabler for 1).** Lane
   spawners should pin a deterministic workspace directory (per run/epoch)
   rather than temp-dir-of-the-day, or explicitly relocate session state
   and recreate the identical path. [inference]
3. **Compact-before-checkpoint policy (low effort, medium impact).** On
   `/pause` and at checkpoint boundaries, invoke each driver's compaction
   (`/compact`) so checkpoints stay small and replay context is
   summarized; record the compaction in the journal (it changes what a
   resumed agent "remembers" — that must be evidence, not a side effect).
   [inference]
4. **Batch-CLI checkpoint-restart via native resume (low–medium effort).**
   The `checkpoint-restart` profile can use `claude -p --resume <id>`,
   `codex exec resume <id> --name <run>`, `opencode --session <id>`
   instead of cold re-briefing — cheaper resumes and preserved context for
   the fallback chain's batch tail. [inference]
5. **Keep vendor transcripts as acceleration data only (policy, zero
   effort, already decided — restate it loudly).** Vendor session ≠
   workspace; conversation fork ≠ workspace fork; a resumed session must
   re-verify repository state before acting (Claude's own docs say the
   resumed context "does not reverse or recreate reality"). forge's
   trusted-publisher validation must never trust "the session remembers
   tests passed". [documented principle, inference for enforcement]
6. **Journal-offload for large payloads (medium effort).** Adopt the
   payload-codec pattern: big tool results / transcripts live in the
   content-addressed artifact store, journals carry digests — keeps the
   Postgres controller lean under 3,000-turn agent sessions. [inference,
   pattern documented]
