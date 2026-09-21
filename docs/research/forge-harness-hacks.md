# Forge harness lane: LIVE-found hacks, workarounds, and lessons

Institutional memory for anyone implementing an interactive-adapter
(`src/forge/adaptive/adapters.py` and successors). Everything here was
found the hard way — on live lanes (GitLab CE 18.9.1 lab, GitHub Actions
hosted runners, Azure Pipelines) — and each item cites its anchor:
`src/forge/harness_entry.py`, `ci/templates/claude-code.gitlab-ci.yml`,
`ci/templates/forge-harness.github.yml`, `src/forge/harnesses/prompt.py`,
`docs/harnesses/claude-code.md`, or the fixing commit.

Companion ground truth: `docs/research/harness-interfaces.md` (§2 Claude
Code / Agent SDK), `docs/research/harness-config-best-practices.md`,
`docs/research/mcp-lane-live-evidence.md`.

---

## Current invocation (exact flags, env vars, their WHY)

### The Actions lane (the evolved posture — `render_driver_script`, harness_entry.py:693-765)

The rendered bash script, in order:

```bash
export API_TIMEOUT_MS=3000000 BASH_DEFAULT_TIMEOUT_MS=300000 BASH_MAX_TIMEOUT_MS=600000
export CLAUDE_CONFIG_DIR="$(mktemp -d /tmp/claude-lane-config.XXXXXX)"
if [ -n "$FORGE_REPAIR_CONTEXT" ]; then
  export MAX_THINKING_TOKENS="${FORGE_MAX_THINKING_TOKENS:-8000}"
fi
# ...npm pinned install preamble (3 retries) + claude --version...
cat > /tmp/forge-mcp.json <<'FORGE_MCP_EOF'
{"mcpServers": { ...from FORGE_HARNESS_MCP... }}
FORGE_MCP_EOF
claude -p 'Implement the approved task in .forge/brief.md. Read it first, then follow it exactly.' \
  --model <model> \
  --allowedTools "Bash(git status:*),...,Bash(set:*)[,mcp__name__*,mcp__name]" \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode bypassPermissions \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose 2>&1 \
  | tee -a .forge/events.jsonl | $FORGE_FILTER_PIPE
```

Run as `/bin/bash -o pipefail -c <script>` (harness_entry.py:1502-1504).

Flag-by-flag WHY:

| Flag / env | Value | WHY |
|---|---|---|
| `-p` | print mode | Headless one-shot; the ONLY unattended mode. In `-p` mode un-allowed tools are auto-DENIED, never prompted. |
| prompt | the SHORT pointer `TASK_PROMPT` | The brief file (`.forge/brief.md`) IS the prompt; the one-liner only points at it. Identical string for every driver (prompt.py:34-36). |
| `--model` | from RunSpec | Shipped only when non-empty (no flag otherwise). |
| `--allowedTools` | the 42-rule list + MCP grants | Now documentation/defense-in-depth only — bypassPermissions ignores it (see sandbox section). Kept because the GitLab twin still matches against it and because it states intent. |
| `--disallowedTools "Bash(git commit:*)" "Bash(git push:*)"` | mechanical deny | R5: deny beats EVERY permission mode, including bypass. The only CLI-level write boundary that survives everything. |
| `--permission-prompts none` | no interactive prompt ever | Guarantees unattended operation; requires claude-code v2.1.259+ (that is why the pin is 2.1.276 — harness_entry.py:206-209). |
| `--permission-mode bypassPermissions` | approve everything | See "sandbox/permission model" — the allowlist whack-a-mole was declared unfixable after three LIVE denial waves in one day (commit ce88e99). |
| `--max-turns 200` | turn bound | Hard ceiling on runaway runs; the CLI errors when reached. |
| `--mcp-config /tmp/forge-mcp.json --strict-mcp-config` | MCP isolation | Servers come ONLY from the `FORGE_HARNESS_MCP` CI variable; strict mode locks out the repo's own `.mcp.json` (prompt-injection surface reduction). The empty map is STILL rendered and passed — "no servers" must mean strict isolation, not "fall back to repo config" (mcp.py:64-67). `${VAR}` refs inside the config expand at runtime from the job env, so server keys stay in separate masked CI variables. |
| `--setting-sources ''` | load NO filesystem settings | Repo/user settings, hooks, and skills from outside the brief are not loaded. Same injection-surface posture as strict-mcp. Note: policy/managed settings and CLI flags always load and cannot be excluded (documented vendor behavior). |
| `--output-format stream-json --verbose` | NDJSON event stream | Per-turn `result` events carry `usage` (the usage receipt is summed from them); the log filter compacts it into the job trace LIVE. `--verbose` is required for stream events. |
| `2>&1` then `tee -a` then `$FORGE_FILTER_PIPE` | audit trail | `pipefail` keeps claude's exit code through tee/filter so a nonzero agent exit classifies the run failed WITHOUT aborting the artifact upload (`if: always()`); the tee'd `.forge/events.jsonl` is the usage source — "the usage receipt comes from the tee'd event log, never from a harness claim outside it" (harness_entry.py:1507-1508). |
| `API_TIMEOUT_MS=3000000` | 50 min | Long API turns must not die at the client default mid-run (R5 vendor timeout budgets). |
| `BASH_DEFAULT_TIMEOUT_MS=300000` / `BASH_MAX_TIMEOUT_MS=600000` | 5/10 min | Same, for long-running bash tool calls (test suites). |
| `CLAUDE_CONFIG_DIR=$(mktemp -d ...)` | ephemeral config | LIVE-found (commit e2adf02): the agent wrote claude auto-memory (`memory/MEMORY.md`) on the runner; on REUSED hosted runners the MEMORY.md index auto-loads into the NEXT run's context — cross-run contamination from a different issue on the same VM. A fresh config dir per lane guarantees a cold start; lane auth rides on env vars, so nothing stored is lost. The brief IS the run's memory. |
| `MAX_THINKING_TOKENS=8000` (repair only) | thinking cap | LIVE-measured (PRs #30/#31, commit d760152): 17% of turns (>40s) eat ~half the wall time; repair re-dispatches are GUIDED fixes (bounded failure context in the brief) so deep thinking is waste there. First cycles think freely. Override: `FORGE_MAX_THINKING_TOKENS`. |
| npm pin `@anthropic-ai/claude-code@2.1.276` | R15 pin | An unpinned install rides the npm `latest` dist-tag — a CLI release can silently change lane behavior (flags, permission semantics, event schema) between the plan gate and the run. Every preamble echoes `claude --version` into the job log so pin drift is VISIBLE, never silent. `FORGE_DRIVER_VERSIONS` (repo variable, JSON) overrides per driver; `"latest"` keeps unpinned; malformed values fail the lane CLOSED. |
| `$FORGE_FILTER_PIPE` | log filter | `node /tmp/harness-log-filter.mjs "<driver>"`, fetched from raw.githubusercontent over the pinned forge ref (`FORGE_PINNED_REF`); degrades to `cat` if unfetchable — the log degrades, the candidate never blocks. |

Credentials (Actions, gated per driver in the template — a driver never
sees another provider's key): `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` for claude-code (BYOK:
capability/credential PAIRS — a Claude seat or a z.ai
Anthropic-compatible gateway, never forge's own key). The checkout uses
`persist-credentials: false` and `permissions: contents: read, issues:
read` only.

### The GitLab lane template (the older twin — still `acceptEdits`)

`ci/templates/claude-code.gitlab-ci.yml` renders the same shape with an
OLDER permission posture and per-platform env:

```bash
claude -p "Implement the task described in .forge/brief.md for issue #$FORGE_ISSUE_IID. ..." \
  --model "$ANTHROPIC_MODEL" \
  --allowedTools "$CLAUDE_TOOLS" \
  --disallowedTools "Bash(git commit:*)" "Bash(git push:*)" \
  --permission-prompts none \
  --permission-mode acceptEdits \
  --max-turns 200 \
  --mcp-config /tmp/forge-mcp.json --strict-mcp-config \
  --setting-sources '' --output-format stream-json --verbose 2>&1 \
  | tee /tmp/harness.jsonl | node /tmp/filter.mjs claude-code
```

GitLab-only env and WHY:

| Env | Value | WHY |
|---|---|---|
| `ANTHROPIC_MODEL` | `glm-5.3-flash[1m]` | The `[1m]` suffix selects the 1M-token window at the z.ai gateway — without it Claude assumes 200k and warns. |
| `IS_SANDBOX=1` | job variable | GitLab docker executors run containers as ROOT; claude refuses relaxed-permission operation for root unless told it is sandboxed. The ephemeral CI container IS the execution profile (ADR-0002). |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | job variable | Cuts telemetry/update traffic from the lane. |
| `NO_PROXY=$CI_SERVER_HOST,127.0.0.1,localhost` | before_script | Runner-local hosts must bypass any configured proxy. Live lab note: long streaming turns hit connection resets on some networks — `FORGE_HARNESS_HTTPS_PROXY` fixed exactly this (identical prompt: 226 s direct vs 2 s via fast-path proxy; claude-code.md "Live notes"). |
| `API_TIMEOUT_MS=300000` → re-exported `3000000` | script | The job-level 300000 is superseded inside the script block by the R5 budgets. |
| `FORGE_DRIVER_EXIT` | completed/failed | The `|| FORGE_DRIVER_EXIT="failed"` idiom + `set -o pipefail`: pipefail keeps claude's exit code through tee/filter; nonzero does NOT abort the trace — artifacts still upload `when: always`. |

GitLab brief transport: `printf '%s\n' "$FORGE_PLAN" > .forge/brief.md`
(the approved plan arrives as a CI VARIABLE — ADR-0012: context passes
through GitLab CI variables, not forge-side state). Commit 6e4696d
(2026-09-21, the "recent fix"): this used to write `/tmp/brief.md` —
LIVE-found in the OPS-07 pilot that the agent could NOT READ ITS OWN
BRIEF, because /tmp was outside the claude workspace sandbox. The brief
now lives at `.forge/brief.md` (already created and git-excluded) and
the prompt references it there. The MCP config still lives at
`/tmp/forge-mcp.json` — that file is read by the CLI itself at startup,
not by the agent, so outside-workspace placement is fine there.

GitLab lane hygiene (before_script): `git remote set-url --push origin
FORBIDDEN` (the mechanical push kill), read-only fetch credential at
most, `.forge/` added to `.git/info/exclude` (fresh CI checkouts lack
`.git/info` — create it first; commit ec9db07), detached checkout of
`$FORGE_ATTEMPT_BASE` (the frozen attempt base).

Divergence warning for the adapter implementer: the GitLab template
still runs `acceptEdits` + a 15-rule allowlist (the original "GitLab Duo
external agents" posture), while the Actions entry point moved to
`bypassPermissions` + the 42-rule tuple kept as documentation. The
Actions posture is the evolved one; the GitLab twin has not been
re-migrated. Both share: mechanical commit/push deny,
`--permission-prompts none`, `--max-turns 200`, strict MCP,
`--setting-sources ''`, stream-json, pipefail+tee+filter, npm retry
preamble with version echo.

---

## Live-found hacks and their rationale

Chronological, with anchors. Each of these cost a live lane run.

1. **Allowlist whack-a-mole → bypassPermissions (the big one).**
   Commit ce88e99 (2026-09-17): THREE LIVE denial waves in one day —
   (a) quality gates (`make lint`, `uv run ruff`, bare pytest/ruff/mypy
   denied), (b) pipeline segments (`... | awk 'length > 100'`, `sed
   's/^+//'` denied — Claude Code requires EVERY segment of a compound
   command to be allowlisted, not just the head), (c) ANY redirection
   (`python3 -m pytest 2>&1` poisons segment matching even with an
   allowed prefix — `git diff -U0 > /tmp/forge.diff` was likewise
   denied). Conclusion, quoted: "Permission granularity cannot be
   completed by enumeration." The lane's real security boundary is
   ARCHITECTURAL: no write credentials, push FORBIDDEN at the remote,
   candidate travels as an artifact validated by the trusted publisher.
   The mechanical deny still applies because deny beats every mode.

2. **`--add-dir /tmp` (added, then made moot).** Commit 78eebb1 added
   `--add-dir /tmp` because a `git diff > /tmp/...` redirect was denied
   — redirects outside the workspace need an explicit additional working
   directory under acceptEdits ("ephemeral lane, no secrets: safe").
   Commit ce88e99 (one hour later, same day) REMOVED it: bypass mode
   makes redirects/`--add-dir` moot (everything allowed except the
   mechanical deny). Test comment at tests/test_harness_entry.py:1539
   preserves the memory. Lesson for the adapter: if you ever go back to
   a non-bypass mode, writes/redirects outside cwd need `--add-dir`
   (and acceptEdits auto-approves edits only inside cwd +
   additionalDirectories).

3. **The A09 glue bug (42 rules, one per literal).** Commit b8f4627
   (2026-09-18): `_CLAUDE_ALLOWED_TOOLS` had MISSING COMMAS between
   adjacent Python string literals inside a parenthesized concatenation
   — `"Bash(python3:*)" "Bash(python:*)" "Bash(.venv/bin/python:*)"`
   silently GLUED into ONE merged rule
   `Bash(python3:*)Bash(python:*)Bash(.venv/bin/python:*)` the driver
   could never match, defeating the allowlist expansion. Fix: ONE rule
   per literal in a tuple (`_CLAUDE_TOOL_RULES`, 42 entries — count
   them: 4 git + 7 read utils + 11 text-processing + 6 python/venv
   forms + 4 pip + 7 quality gates), serialized ONLY at the render site
   via `",".join((*_CLAUDE_TOOL_RULES, *mcp_rules))`. Never concatenate
   rule literals again. The tests pin this: tokens must equal the tuple
   exactly, every token contains exactly one `Bash(`, and GLUED
   SIGNATURES (`*)Bash(`, `)*shell(`, `*)'--allow`, ...) must appear
   nowhere in any rendered script.

4. **MCP grant quoting bug (same commit).** The old per-name
   `shlex.quote` on MCP grants shipped LITERAL QUOTE CHARACTERS inside
   the `--allowedTools` value — rules named `'mcp__x__*'` (with quotes)
   that could never match. MCP grants now ride the same plain-comma
   serialization: for each server, TWO grants — `mcp__<name>__*` and
   `mcp__<name>` — and the whole list is shell-quoted ONCE at the end.

5. **The brief must live INSIDE the workspace.** Commit 6e4696d
   (2026-09-21): `/tmp/brief.md` was unreadable by the agent (OPS-07
   pilot) — outside the claude sandbox. Now `.forge/brief.md`,
   git-excluded, referenced by the short `-p` pointer. Generalized
   lesson: anything the AGENT must read belongs under cwd; things only
   the CLI reads (mcp-config) may live in /tmp.

6. **Ephemeral config dir (no memory bleed).** Commit e2adf02: the
   agent wrote `memory/MEMORY.md`; on reused runners the stale index
   auto-loads into the next run's context and poisons it. Fresh
   `CLAUDE_CONFIG_DIR` per lane = guaranteed cold start.

7. **Hosted Actions runners have NO .venv.** LIVE-found early:
   `.venv/bin/python` allowlist patterns denied everything and the
   agent flailed — forge installs into the interpreter via pip on
   hosted runners. The lane NOW bootstraps `uv sync --frozen` when the
   repo has a `uv.lock` (so `uv run mypy` etc. are real), and the
   allowlist carries BOTH forms; "venv forms are harmless where the venv
   does not exist — a normal tool result beats a permission denial."

8. **Pin the lint tools to the repo's own lock.** LIVE-found on PR #30:
   a pip-latest ruff formats differently than the CI-pinned one and
   every candidate failed lint with "Would reformat" — the drift burned
   TWO repair cycles on a healthy candidate. The lane greps `uv.lock`
   for the ruff/pytest versions and installs exactly those when there
   is no full lock to sync.

9. **Job timeout above the harness deadline.** LIVE-found: GitHub
   killed the job at 60m as `cancelled`, so the harness deadline could
   not classify honestly as `harness_timeout`. The template now sets
   `timeout-minutes: 120`, above the run-side
   `FORGE_HARNESS_TIMEOUT_SECONDS` (5400s).

10. **Non-hidden artifact staging (A08).** `upload-artifact@v4` excludes
    hidden files by default AND its globber drops dot-directories
    BEFORE traversal — a leading-dot staging name (`.forge-output/`)
    uploaded NOTHING on a fresh repo (`if-no-files-found: error` fired
    every run). Staging is `forge-output/` (no leading dot); only the
    two contract files ever land there.

11. **AzDO undefined-variable literals.** LIVE-found (ADR-0024): an
    UNDEFINED Azure Pipelines variable reaches the process as its
    literal `$(NAME)` text. `harness_entry` treats a value that starts
    `$(` and ends `)` as UNSET instead of failing the fail-closed MCP
    parse on junk (harness_entry.py:1450-1455).

12. **Turn economy is a prompt problem.** LIVE-measured (run b6bbbd66,
    2026-09-16): 130 tool calls where a staff engineer spends ~40;
    service.py re-read 11×. The brief now carries the Working-method
    section (see brief pipeline below) — grep-then-read-once, ~60-call
    budget, narrowest test subset, compound commands.

13. **Thinking is the dominant wall-time cost.** LIVE: after codegraph
    + brief discipline cut turns 130 → 72-77, per-turn latency ROSE
    17.7s → ~24s; 17% of turns (>40s) eat ~half the run. Repair
    re-dispatches cap `MAX_THINKING_TOKENS` (default 8000); first
    cycles think freely.

14. **MCP tool renames absorbed by globs.** Live lab (job 651): the
    live Context7 server advertises `query-docs`, NOT the historically
    documented `get-library-docs`; the `mcp__context7__*` allow glob
    absorbed the rename without a job change. Always grant per-server
    GLOBS (`mcp__<name>__*` + `mcp__<name>`), never per-tool names.
    (Counter-example: grok's native MCP FAILED live — its `use_tool`
    bridge rejects `server__tool` naming with "Tool not found"; job
    654.)

15. **`[1m]` model suffix matters** (z.ai catalog) — without it Claude
    assumes a 200k window and warns.

16. **Connection resets on long streaming turns** — fixed on the lab by
    routing the driver through `FORGE_HARNESS_HTTPS_PROXY` (226 s → 2 s
    on an identical prompt). The stream-json events in the job trace
    are the diagnostic surface.

---

## The sandbox/permission model

The lane's security posture in one paragraph (harness_entry.py:729-736):
the allowlist whack-a-mole is unfixable in principle, so claude runs
`bypassPermissions`; the REAL boundary is architectural — (1) the lane
holds NO write credentials (checkout with `persist-credentials: false`,
`contents: read` only, read-only tokens at most), (2) the push URL is
FORBIDDEN (`git remote set-url --push origin FORBIDDEN` — nothing to
push even if the model tried), (3) the candidate travels as an ARTIFACT
validated and published by the trusted publisher, never as a commit the
agent made. On top of that, one mechanical rule survives every mode:
`--disallowedTools "Bash(git commit:*)" "Bash(git push:*)"` — deny beats
every permission mode, including bypass (documented vendor evaluation
order: hooks → deny rules → ask rules → permission mode → allow rules).

Supporting isolation layers, all LIVE-motivated:

- **`--permission-prompts none`** — the unattended guarantee. Combined
  with `-p` semantics, anything that would prompt is denied, never
  awaited. Requires CLI v2.1.259+.
- **`--setting-sources ''`** — no filesystem settings/hooks/skills load.
  Prompt-injection surface reduction. (Policy/managed settings and CLI
  flags always load — cannot be excluded.)
- **`--mcp-config <file> --strict-mcp-config`** — ONLY the variable's
  servers load; the repo's own `.mcp.json` is ignored. The empty map is
  still passed so "no servers" means strict isolation.
- **`CLAUDE_CONFIG_DIR` fresh per lane** — no auto-memory bleed across
  runs on reused runners.
- **`IS_SANDBOX=1`** (GitLab) — claude treats the root container as
  sandboxed instead of refusing.
- **Vendor timeout budgets** (`API_TIMEOUT_MS=3000000`,
  `BASH_DEFAULT/MAX_TIMEOUT_MS`) — long turns/tool calls must not die
  at client defaults mid-run.
- **Exit decoupling** — `.forge/exit` (completed|failed) is a FILE,
  written by the entry point; the entry ALWAYS exits 0 once its duties
  are done so the workflow's `if: always()` candidate steps still run
  and forge classifies from the artifact's meta exit. A red step would
  rob the audit trail.
- **Fail-closed config parsing** — malformed `FORGE_HARNESS_MCP` or
  `FORGE_DRIVER_VERSIONS` refuses the lane before any driver runs (a
  typo must never downgrade to an unpinned install or a serverless
  run).
- **Working-tree hygiene** — `.forge/`, `.codegraph/`, `__pycache__/`,
  `*.pyc`, `.venv/`, `forge-output/` are git-excluded via
  `.git/info/exclude` (create `.git/info` first on fresh checkouts),
  and the emit step `rm -rf .codegraph .venv` as belt-and-braces before
  `git add -A`. The graph DB and the lane venv are infrastructure,
  never deliverables.

Per-driver equivalents of the mechanical deny (the same doctrine, four
vocabularies): grok `--deny 'Bash(git commit:*)' --deny 'Bash(git
push:*)'` (deny beats `--always-approve`), opencode permission map
`"git commit *": "deny", "git push *": "deny"` riding
`OPENCODE_CONFIG_CONTENT` (plus `external_directory`/`doom_loop` set to
allow — ask-by-default keys HANG a headless run), copilot
`--deny-tool 'shell(git commit)' --deny-tool 'shell(git push)'`
(documented: deny beats every allow including `--allow-all`).

---

## What the interactive adapter MUST preserve

Non-negotiables for `ClaudeSDKAdapter` / any successor, derived from the
above and from the trust-boundary contracts already frozen in
`src/forge/adaptive/adapters.py`:

1. **Lane placement (EXE-02).** The adapter runs in the EXECUTION lane,
   next to its runner — never inside the privileged API process that
   owns credentials. Steering text and vendor SDK callbacks stay
   outside the trust boundary by construction.
2. **Steering is not permissioning.** A steering channel that could
   grant itself permissions is a privilege escalation path
   (CodexAppAdapter's contract: `steer_active_turn` is guidance only;
   what a turn may DO is decided elsewhere). Keep the R5 doctrine: the
   commit/push mechanical deny must be unreachable from steering.
3. **The architectural boundary, not an allowlist.** Whatever permission
   mode the interactive session runs in, the lane must remain:
   no write credentials, push FORBIDDEN, candidate-as-artifact through
   the trusted publisher, `--disallowedTools`-equivalent deny that beats
   every mode. Do not "fix" interactivity by handing the session a
   write credential.
4. **Cold start per session.** Fresh `CLAUDE_CONFIG_DIR` equivalent /
   `setting_sources=[]` (the SDK's own default isolation). No
   auto-memory, no filesystem settings, no repo `.mcp.json` unless the
   lane's variable names it. The brief IS the run's memory.
5. **The brief-file transport.** Short pointer prompt + full brief at
   `.forge/brief.md` INSIDE the workspace (the /tmp lesson), carrying
   the approved-bytes envelope (A03: digest-verified frozen task+plan;
   fail closed on post-approval edits) and, on repair, the bounded
   failure context + thinking cap.
6. **Unattended guarantees.** No code path may block on a prompt
   (`--permission-prompts none` equivalent; opencode's ask-keys
   allowed; grok's `--always-approve`). An interactive adapter adds
   steer/interrupt — it must never ADD a hang the scripted lane did not
   have.
7. **The audit trail.** Tee'd event log → usage receipts (per-turn
   `result` usage summed; unknown stays unknown, NEVER zero — opencode
   has no parseable receipt and reports None); exit classification via
   the meta file, decoupled from process exit; artifacts upload
   `if: always()`; non-hidden staging dir.
8. **Timeouts and bounds.** `--max-turns 200` equivalent,
   `API_TIMEOUT_MS`/`BASH_*_TIMEOUT_MS` budgets, lane timeout ABOVE the
   harness deadline so `harness_timeout` classifies honestly, and the
   repair-cycle thinking cap.
9. **Pinned CLIs + visible drift.** Version pins with the resolved
   `--version` echoed into the log; fail-closed override parsing.
10. **The execution profile doctrine (EXE-08).** `tool_allowlist` /
    `egress_policy` / `privileged_ok` from adapters.py: discovery =
    read-only; implementation adds write + run_tests + package-registry
    egress; verification can read and run tests but NOT write files ("a
    verifier that could change what it verifies verifies nothing"); the
    docker socket and testcontainers belong to the TRUSTED TEST
    EXECUTOR only — a coding agent holding them is the containment
    failure EXE-08 exists to prevent. Unknown modes/roles are refused,
    never defaulted.
11. **Outbound-only control (EXE-04).** The runner DIALS OUT to the
    control plane (`OutboundControlChannel`); no inbound listener to
    attack; the token travels as a broker-owned REFERENCE (`ref:<id>`,
    never a value — a ref that looks like a pasted secret is refused);
    monotonic sequence numbers so gaps/replays are detectable.
12. **Tested, not named (DriverMatrix).** Register only the
    (sdk, provider_route, credential_mode) combinations actually
    verified live; unregistered combinations fail during ONBOARDING,
    never mid-run. The BYOK reality: a proxied SDK may silently not
    deliver interrupt or mid-turn send — that is exactly what the
    matrix exists to catch.
13. **Bootstrap classification.** A FAILED environment bootstrap is
    infrastructure/config (blocked), never a code-repair candidate —
    `.forge/bootstrap` + `FORGE_BOOTSTRAP_FAILED` marker, carried into
    the meta.

## What it can improve

Where the interactive adapter can do BETTER than the scripted lane:

1. **Programmatic permission decisions instead of bypass.** The
   whack-a-mole happened because CLI prefix-allowlists cannot express
   policy. An SDK `can_use_tool` callback (or per-role allowlists in
   code — the `tool_allowlist` vocabulary: read_file/grep/run_tests,
   not `Bash(prefix:*)`) makes policy decidable: deny commit/push
   mechanically, allow by role, and never needs bypassPermissions on a
   credential-bearing host. bypass was only honest because the lane is
   credential-less; an interactive lane with richer capabilities should
   re-earn its boundaries in code.
2. **Real OS sandboxing.** Claude Code's sandbox mode (Seatbelt /
   bubblewrap; writes default to cwd + TMPDIR + `--add-dir`; protected
   paths write-denied) composes with a permission mode — worth
   revisiting now that the config surface is known
   (harness-config-best-practices.md).
3. **Typed event streams instead of log scraping.** Drive the session
   via the SDK's message objects; usage receipts come from typed
   per-turn receipts rather than parsing tee'd NDJSON (and opencode's
   missing receipt could become a real API read).
4. **Interrupt + steer + checkpoint.** The scripted lane burns full
   turns on wrong paths; the Claude profile already claims
   `interrupt`, `live_input`, `checkpoint_export`, `questions`.
   Interrupt-during-flail (the permission-denial flails, the 11×
   re-reads) is the direct cure for the measured turn waste — but each
   capability must be matrix-verified per provider route first.
5. **Thinking budget as steering, not just env.** The repair-cycle cap
   is a blunt env var; an interactive lane can observe slow turns and
   steer ("wrap up, run the tests now").
6. **Session resume for repair.** Repair re-dispatches currently start
   cold with a 2000-char failure context; a resumed session (SDK
   `resume`/`session_id`) plus the bounded context would be cheaper
   than a fresh 60-call budget — gated on matrix evidence.
7. **Brief interactivity.** Clarifying questions (`questions`
   capability) can replace the blind-repair loop for ambiguous plans —
   routed through the outbound control channel, never an inbound
   listener.

## The brief/render pipeline

One prompt builder, both lanes (`src/forge/harnesses/prompt.py`) —
single source of truth; per-CLI FLAGS live in the driver scripts, the
PROMPT is shared.

**Transport (Actions):** the lane FETCHES its own brief content —
`python -m forge.harness_entry --render-brief` reads the approved plan
comment (addressed EXACTLY by `FORGE_PLAN_NOTE_ID` — no scanning, no
identity heuristic; tamper guards: forge-bot author, `## Forge plan`
header, this run's id) plus the `FORGE_ENVELOPE_DIGEST` /
`FORGE_SPEC_DIGEST` binding (A03: the approved title/description/plan
sections are extracted and re-digested; ANY mismatch fails the lane
closed — "approved brief bytes changed after approval (re-approval
required)"). The live issue is never read for task text on the enforced
path. Legacy replay (inputs absent) keeps the loud UNENFORCED scan.
No dispatch input ever carries plan text, so no input size limit binds.
**Transport (GitLab):** `$FORGE_PLAN` CI variable → `.forge/brief.md`
(after 6e4696d). **Transport (AzDO):** work item + plan comment via
read-only `FORGE_AZDO_READ_TOKEN` (Basic auth, empty username); HTML
fields stripped defensively; the `$(NAME)` literal gotcha applies.

**Structure** (render order): Role ("staff engineer implementing an
APPROVED plan... work surgically: the plan was reviewed by a human and
its digest is bound to this run") → Task (issue snapshot) → Approved
plan VERBATIM (with digest line: "this exact text is what the human
approved"; deviations must be minimal + noted in a code comment) →
Constraints (dependency policy — default "do not add new dependencies,
note in a code comment instead of editing manifests"; denied paths —
`.gitlab-ci.yml`, `.github/workflows/**`, `Jenkinsfile`, `Dockerfile*`,
`*.toml` CI/tool config, `.forge/**` — "CI/config IS the execution
profile"; style — match surrounding code, type hints + docstrings, no
commented-out code; minimal diff, no drive-by refactors) → Quality bar
(run the repo's test subset and leave it GREEN — "a candidate that
breaks the existing tests is rejected"; run the repo's OWN static gates
with the exact commands CI enforces; conventions files AGENTS.md /
CLAUDE.md detected in the checkout and named explicitly — "project
conventions win") → Working method → [Codegraph section] → Output
contract.

**Working-method section** (turn-economy discipline, LIVE-measured):
"Every tool call costs a full model round trip; budget yourself to
~60 calls for the whole task" — locate before reading (grep the symbol,
read the range ONCE, never re-read — "if you feel the urge, write down
what you know instead"); start from the files the plan names; iterate
against the NARROWEST test subset, full suite once at the end; prefer
compound commands.

**Codegraph section** (conditional on the `codegraph` server being in
`FORGE_HARNESS_MCP` — the same variable that provisions the server
directs the brief to it): a pre-indexed code graph is mounted as MCP
server `codegraph` (`mcp__codegraph__*`, primary
`codegraph_explore`); ask it for symbol definitions, callers/callees,
impact — "one call answers what would otherwise take a dozen greps";
fall back to grep/Read only for the exact text about to be edited. The
lane indexes with `codegraph init` (timeout 300, telemetry off, pinned
@1.6.0) and git-excludes `.codegraph/`.

**Output contracts**: ci_lane — "Do NOT commit and do NOT push. You
have no write credential and the lane must stay that way. Leave ALL
your changes in the working tree: the forge publisher collects `git
diff` against the attempt base and publishes it after validation — its
commit, not yours. Never touch `.forge/` except reading
`.forge/brief.md`." dev — commit with the provided message, never push.

**Repair context** is APPENDED after render (harness_entry.py:1312-1320):
"## Repair context — previous candidate failed verification" +
`FORGE_REPAIR_CONTEXT[:2000]` — bounded so the failure context cannot
swamp the brief; its presence also triggers the thinking cap.

**`--emit-meta` schema** (the emit step, R16/R23 + A18/B12/C10/D08):
`candidate.meta.json` written to `forge-output/` beside the diff —
`schema_version: 2`; `run_id`; `attempt_id` (GitHub's
`<run_id>:<run_attempt>`, free from runner env, empty outside Actions —
never fabricated); `attempt_base_oid`; `driver`; `model`; `exit`
(completed|failed|unknown from `.forge/exit`); `bootstrap` (ok|failed
from `.forge/bootstrap`, unknown stays empty); `manifest_digest`
(sha256 over the exact candidate.diff bytes — the control plane
re-checks it after download); `usage` (inlined from `.forge/usage.json`:
input/cached_input/output tokens, completeness "aggregate", source
"stream-json" — or null); `profile_digest` (sha256 of the execution
profile derived from THIS checkout — best-effort, degrades to empty);
`observed_execution` (what the lane ACTUALLY did: driver, exit,
usage_completeness, candidate_changed, commands from the trusted
wrapper's `.forge/commands.tsv` receipts
`argv_head<TAB>exit<TAB>report`, and `receipts_producer:
"self_reported"` for workspace-file receipts — telemetry, never gate
evidence). A missing diff fails the emit LOUD (rc 1) so
`if-no-files-found: error` classifies as infrastructure, not a silent
partial artifact.

**Usage parsing** (`parse_usage`): claude — per-turn `result` events
summed (no run-level receipt exists); grok — per-response `usage` events
EXCEPT the final `end` event already carries the run aggregate and WINS
(summing both would double-count); opencode — no parseable stdout
receipt, returns None. Unparseable lines are skipped; token counts that
never appear stay None. Unknown stays unknown, never zero (F22 lite).

**Exit classification**: `.forge/exit` gets `completed` iff the driver
script's return code is 0 (pipefail preserves the agent's own exit
through tee/filter); the entry point then ALWAYS returns 0 — "a red
step here would rob the audit trail."
