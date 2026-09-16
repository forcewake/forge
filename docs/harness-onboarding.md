# Harness onboarding — delegating implementation to claude-code in project CI

ADR-0015 lets forge delegate the implementer step to a coding harness. The
harness never runs on forge infrastructure: it executes as a job
(``forge-agent``) in the **target project's** CI, inside the execution
profile approved at onboarding (ADR-0002). This page is the onboarding
checklist for a project that wants ``FORGE_IMPLEMENTER_BACKEND=ci_harness``.

## 1. Include the CI template (one-time, human-applied)

Add a single static include to the project's ``.gitlab-ci.yml``:

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/<org>/forge/main/ci/templates/claude-code.gitlab-ci.yml'
```

The template defines one job, ``forge-agent``. It is inert in normal
pipelines: its only rule is ``if: '$FORGE_RUN_ID'``, so it runs exclusively
when forge triggers a pipeline with run variables (ADR-0015 §5). It is
versioned with forge and must not be edited per project; per-project policy
belongs in the admin config repository (ADR-0011).

## 2. Required project CI variables

Configured in the target project (GitLab → Settings → CI/CD → Variables),
masked and protected per policy. Forge stores none of them — credentials
stay in GitLab (ADR-0015 §4):

| Variable | Purpose |
| --- | --- |
| per-driver credentials | see each driver's doc: [claude-code](harnesses/claude-code.md) · [grok-build](harnesses/grok-build.md) · [opencode](harnesses/opencode.md) · [copilot-cli](harnesses/copilot-cli.md) |
| `FORGE_BOT_READ_TOKEN` | OPTIONAL read-only PAT (repo read only). The proposal-only lane (ADR-0016) must NEVER receive a write token — the trusted publisher is the only writer. When unset, the lane fetches with the runner credential; push is disabled by construction (`git remote set-url --push origin FORBIDDEN`). |

Forge itself only sends non-secret run variables with the pipeline trigger:
`FORGE_RUN_ID`, `FORGE_ISSUE_IID`, `FORGE_ISSUE_TITLE`, `FORGE_PLAN`,
`FORGE_HARNESS_MODEL` and `FORGE_ATTEMPT_BASE` — the frozen attempt base the
lane works on (cycle 1: the approved source base; a repair: the last
verified candidate, ADR-0016 §4).

Optional: `FORGE_HARNESS_HTTPS_PROXY` — an HTTP proxy for the harness's
provider traffic only (git/npm stay direct via `NO_PROXY`). Some runner
networks throttle long-lived AI streaming responses to a crawl while short
requests remain fast; point this at a proxy on a fast path (verified on the
lab: identical headless prompt, 226 s direct vs 2 s via proxy).

## 3. Runner requirements (ADR-0002 execution profile)

- **Ephemeral docker executor** — an isolated, disposable container per job.
  Shell runners are not acceptable for autonomous agent execution.
- **No privileged mode, no Docker socket**, no access to production secrets,
  no broad internal network access.
- Outbound network to the harness API endpoint (`ANTHROPIC_BASE_URL`), the
  npm registry (harness install) and the GitLab origin (fetch; the lane
  never pushes — ADR-0016).
- Job wall-clock is bounded by `timeout: 30m` in the template
  (GitLab `maximum_timeout`); forge additionally enforces its own durable
  run-level deadline, `FORGE_HARNESS_TIMEOUT_SECONDS` (default 1800s), so a
  lost job can never wait forever (ADR-0013).

## 4. How a run flows with the harness backend

1. `@forge /implement` plans as usual (the planner stays an LLM agent);
   with `FORGE_IMPLEMENTER_BACKEND=ci_harness` the backend is frozen into
   the run's evidence at start.
2. After `@forge /go`, forge ensures the factory branch
   `factory/<issue-iid>/<run-id>` exists and triggers a pipeline on it with
   the run variables above. The run parks durably in `waiting_harness`
   (worker-free; the reconciler polls).
3. `forge-agent` checks out the frozen attempt base (`FORGE_ATTEMPT_BASE`,
   detached), runs the harness headless against the brief — the lane cannot
   commit-and-push (no write credential, push URL disabled) — and uploads
   the working-tree delta as CI artifacts: `.forge/candidate.diff`
   (`git diff --binary --full-index` vs the base) plus
   `.forge/candidate.meta.json` (attempt base, driver, model, exit,
   usage receipt). Its last trace line is
   `FORGE_CANDIDATE:{...}`; the legacy `FORGE_RESULT` line remains during
   the v0.2 → v0.3 migration and carries the attempt base, not a branch.
4. forge **publishes through the trusted publisher** (ADR-0016): the diff
   base is checked against the run's frozen attempt base, the publication
   grant / spec digest / fence are re-checked, the manifest is materialized
   against the authoritative base blobs (strict patch application, no
   fuzz), the resulting ChangeSet is validated against the write policy,
   and only then ONE journaled commit is written via the Changeset API —
   pinned to the attempt base. A Draft MR is created/updated and the run
   moves to `waiting_ci` for the normal quality contract → readonly review
   → ready-for-human evidence flow. The harness's own claim is never
   trusted.
5. Failures: empty diff on cycle 1 → `harness_no_changes`; empty diff on a
   repair → `repair_no_effect`; artifact base mismatch →
   `harness_attempt_base_mismatch`; missing/unreadable artifacts →
   `harness_artifact_missing`; binary diffs / non-applicable patches →
   `harness_candidate_invalid`; policy rejections → `candidate_rejected`;
   runner/auth/quota/timeout → infrastructure (`harness_timeout`, …) —
   everything blocks the run for a human; harness runs never enter the LLM
   repair loop in v1.

## 5. Forge-side configuration

```bash
FORGE_IMPLEMENTER_BACKEND=ci_harness   # or ci_harness:claude-code
FORGE_HARNESS_TIMEOUT_SECONDS=1800
FORGE_HARNESS_MODEL=glm-5.3-flash[1m]  # passed through as FORGE_HARNESS_MODEL
```

Backend selection is tighten-only (ADR-0011): once a project runs with a
harness backend, switching back to `builtin` is a deliberate operational
decision, because repair loops and budgets behave differently (builtin runs
repair with bounded LLM cycles; harness runs block instead).

### Harness preference (ADR-0023)

The scalar backend becomes an ordered, tighten-only preference list — in
`forge.yml` (`implement.harnesses: [claude-code, grok-build]`) or via env
(`FORGE_HARNESS_PREFERENCE=claude-code,grok-build`); empty keeps today's
single-backend behavior. Forge compiles the chain against the lanes whose
credentials the project onboarded (`forge doctor` reports the compilable
chain per driver), freezes the selection + fallback tail into the RunSpec,
and discloses it in the plan comment before `/go`. `FORGE_HARNESS_FALLBACK`
(off by default) lets an infrastructure-classified lane failure before any
candidate advance to the chain's next entry — journaled, never on code
failures; `forge doctor --project` shows which drivers would compile.

## 2b. MCP servers in the lane (`FORGE_HARNESS_MCP`, ADR-0022)

One project CI variable — canonical JSON in the Claude `mcpServers` shape —
provisions MCP servers for EVERY driver. Live-verified with Context7 and
Microsoft Learn on GitLab CI (claude-code + opencode; see
[research/mcp-lane-live-evidence.md](research/mcp-lane-live-evidence.md)).

```json
{
  "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
  "learn": {"type": "http", "url": "https://learn.microsoft.com/api/mcp"}
}
```

Set it per CI system:

- **GitLab:** project CI variable `FORGE_HARNESS_MCP` (plain — no secrets
  inside; masked not needed).
- **GitHub:** repo **variable** `FORGE_HARNESS_MCP` (Settings → Secrets and
  variables → Actions → Variables) — the harness workflow passes it via
  `vars.FORGE_HARNESS_MCP`.
- **Azure DevOps:** pipeline **plain** variable `FORGE_HARNESS_MCP`
  (or in the linked variable group).

What each driver does with it (the same JSON, rendered per dialect):

| Driver | Mechanism | Tool grants |
|---|---|---|
| claude-code | `--mcp-config` + `--strict-mcp-config` (ALWAYS on — a repo's own `.mcp.json` is never loaded) | `mcp__<server>__*` per server |
| grok-build | `mcpServers` in `~/.grok/settings.json` | settings-native |
| copilot | `~/.copilot/mcp-config.json` | explicit `--allow-tool <server>` |
| opencode | `mcp` key, schema-translated (`http` → `remote`, `stdio` → `local`) | config-native |

Rules of the road:

- **Secrets stay in separate masked variables.** `${VAR}` references inside
  the JSON expand driver-side at runtime, so a paid MCP server's key is
  `{"headers": {"CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}"}}` with
  `CONTEXT7_API_KEY` as its own masked CI variable.
- **Strict isolation is constant.** Claude always runs
  `--strict-mcp-config`: with no `FORGE_HARNESS_MCP` the lane still locks
  out the repository's own `.mcp.json` (prompt-injection surface).
- **A malformed config fails the lane closed** — the run refuses with a
  clear reason rather than silently executing without the servers the plan
  may depend on.
- MCP grants never weaken the mechanical deny: commit/push rules and the
  FORBIDDEN push URL hold regardless.

## Writing the task: what the agent actually reads

The harness does not see your issue raw. forge renders a **shared brief**
(`.forge/brief.md`, the same builder for the GitLab and Actions/Azure
lanes — `src/forge/harnesses/prompt.py`) with fixed sections:

1. **Role** — senior engineer on THIS repo, working in an ephemeral CI lane.
2. **Task** — the approved plan verbatim + the issue body snapshot.
3. **Constraints** — proposal-only: no commit, no push, no touching
   `.forge/`, stay inside `implement.paths` when scoped.
4. **Conventions** — the repo's `AGENTS.md` / `CLAUDE.md` / `.cursorrules`
   are detected and embedded; the brief directs the agent to load project
   skills from them.
5. **Quality bar** — run the relevant tests; match existing style; the
   review is diff-vs-plan.
6. **Output contract** — leave ALL changes in the working tree; the
   candidate is collected by forge, never committed by the agent.

Write `/implement` tasks so this brief lands well:

```text
# weak
/implement

# good: the plan stage turns this into acceptance-checkable steps
/implement
Add a farewell(name) function to src/greeter.py mirroring greet()'s
conventions (strip + validate). Raise ValueError on empty input.
Cover both cases in tests/test_greeter.py.
```

What the human sees at the gate — the plan comment carries an
**Implementation** block, so `/go` approves the execution shape too:

```markdown
## Implementation
- Harness: **claude-code** · model glm-5.3-flash[1m]
- Fallbacks: grok-build
- Budget class: standard
- Commit cycles: 3
- Selection reason: planner selection
```

Useful task-writing habits: name exact files and behaviors (the planner
copies them into steps); state the negative cases you want tested; if the
repo has an AGENTS.md, keep it current — the brief embeds it verbatim.

## Available harness templates

| Template | Harness | Driver doc (variables, flags, gotchas, triage) |
|---|---|---|
| `ci/templates/claude-code.gitlab-ci.yml` | claude-code | [harnesses/claude-code.md](harnesses/claude-code.md) |
| `ci/templates/grok.gitlab-ci.yml` | grok-build | [harnesses/grok-build.md](harnesses/grok-build.md) |
| `ci/templates/opencode.gitlab-ci.yml` | opencode | [harnesses/opencode.md](harnesses/opencode.md) |
| `ci/templates/copilot.gitlab-ci.yml` | copilot | [harnesses/copilot-cli.md](harnesses/copilot-cli.md) |

Multi-harness selection (the ordered preference list, the compiler, the
Implementation block, fallback): **[harnesses/README.md](harnesses/README.md)**.

Every template carries a driver filter (`$FORGE_HARNESS_DRIVER`): a repo
that includes several forge templates still runs exactly one lane per run —
the driver forge dispatches (from the frozen harness chain, ADR-0023).

## GitHub Actions harness (E3b, ADR-0020)

A GitHub repo can run the same proposal-only lane in its own Actions
runner. The lane has **no write token and no forge secret**: its only
output is the candidate artifact (`.forge/candidate.diff` +
`.forge/candidate.meta.json`, uploaded as
`forge-candidate-<run-id>`), which forge downloads and pushes through the
same trusted publisher as the GitLab path (branch-CAS commit → Draft PR).

One-time, human-applied:

1. Copy `ci/templates/forge-harness.github.yml` into the target repo at
   `.github/workflows/forge-harness.github.yml`.
2. **Replace `<PINNED_REF>`** in the "Run harness driver" step with the
   forge ref the deployment pins to (a tag or commit SHA — never a moving
   branch): the lane installs forge from that ref and runs
   `python -m forge.harness_entry`, so the pin decides which driver
   contract the lane speaks. A changed workflow file invalidates pending
   approvals (the filename is part of the frozen RunSpec).
3. Add the harness provider keys as repo **Actions secrets** (per driver:
   `ANTHROPIC_API_KEY` / `ZAI_API_KEY` / `XAI_API_KEY`) — never forge's
   publisher credentials.

Forge-side configuration:

```bash
FORGE_GITHUB_HARNESS_WORKFLOW=forge-harness.github.yml  # empty = builtin in-worker
FORGE_HARNESS_MODEL=glm-5.3-flash[1m]
FORGE_HARNESS_TIMEOUT_SECONDS=1800
```

With the workflow set, an approved `/go` dispatches `workflow_dispatch`
with `run_id` / `attempt_base_oid` / `driver` / `model` on the factory
branch (`forge/<issue>/<run-id>`), parks the run in `waiting_harness`, and
the reconciler collects the candidate when the run completes — then walks
waiting_ci → review → `ready_for_human` exactly like the GitLab lane.
Actions checks on the head are the verification surface. Agents can also be
pointed at an issue with a label instead of a comment:
`issues.labeled` with the `FORGE_TRIGGER_LABEL` label (default `forge`,
case-insensitive) starts the same run command — the labeler must be in
`FORGE_APPROVERS`, or admission denies the run.

## Monitoring and triage

**Primary window — the job trace, live.** The harness runs claude with
`--output-format stream-json --verbose` and pipes the stream through a
compacting filter (`claude-events-filter.mjs`) straight into the job trace:
tool calls, assistant messages, API errors and retries appear in the GitLab
job log *while the harness works*. The full-fidelity stream is kept at
`/tmp/claude-events.jsonl` inside the job. A healthy run shows a steady flow
of `tool:`/`say:`/`think:` lines; `API-ERROR retry N/M: ...` lines mean
network/provider trouble. The same pattern generalizes to any headless
harness that can stream machine-readable progress (opencode can; GitHub
Copilot CLI currently has no documented stream-json for `-p` runs).

**Timeouts (two layers).** GitLab kills the job at the template `timeout`
(30m, first line of defence per ADR-0015 §6). Forge independently blocks the
run `harness_timeout` (infrastructure) after `FORGE_HARNESS_TIMEOUT_SECONDS`
from the journaled start time — the reconciler enforces it without the job.

**When a run blocks with `harness_*`:**
1. Open the pipeline of the factory branch → `forge-agent` job → read the
   `--- agent events (tail) ---` block: it names the failure class
   (`api_error`+retries = provider/network; permission/system errors = setup).
2. Verify independently: the run reason distinguishes `harness_no_changes`
   / `repair_no_effect` (empty candidate diff), `harness_attempt_base_mismatch`
   (artifact base ≠ frozen attempt base), `harness_artifact_missing`,
   `harness_candidate_invalid` (binary diff / patch does not apply) and
   `candidate_rejected` (write-policy violation) — forge never trusts the
   harness's own success claim; the publisher is the only writer.

**Runner-host triage (unraid/self-hosted, read-only):**

```bash
ssh <runner-host> 'docker ps --format "{{.Names}}\t{{.Status}}" | grep -i project-<id>'
docker exec <build-container> ps aux            # claude process alive? CPU time growing?
docker stats --no-stream <build-container>      # 0% CPU + no log growth = waiting on network
docker exec <build-container> sh -c 'ls -la /root/.claude/projects/-builds-*/; tail -c 4000 /root/.claude/projects/-builds-*/*.jsonl'
# the session jsonl is the authoritative agent transcript (api_error, retryAttempt, tool events)
```

**Known network pattern:** long streaming requests to z.ai from home
networks can die with `ECONNRESET` after minutes (short requests are fine).
claude retries up to 10 times; forge's run-level timeout caps the total.
Mitigations: smaller briefs, `API_TIMEOUT_MS` tuned down (300000 cuts dead
streams earlier), or route the harness through a local LiteLLM proxy
(Anthropic-compatible `/v1/messages`) for proxy-side retries and logs.
