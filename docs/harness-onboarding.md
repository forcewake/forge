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
| `ANTHROPIC_AUTH_TOKEN` | API key the harness uses (never forge's own key) |
| `ANTHROPIC_BASE_URL` | Harness API endpoint (self-hosted gateway/proxy) |
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

## Available harness templates

| Template | Harness | Provider protocol | Notes |
|---|---|---|---|
| `ci/templates/opencode.gitlab-ci.yml` | opencode | OpenAI-compatible (`/chat/completions`) | validated live on GLM via z.ai coding endpoint; tool loops complete in tens of seconds |
| `ci/templates/claude-code.gitlab-ci.yml` | claude code | Anthropic-compatible (`/v1/messages`) | validated for short prompts; long streaming turns can hit connection resets on some networks — stream-json events land in the trace for diagnosis |

## Monitoring and triage

**Primary window — the job trace, live.** The harness runs claude with
`--output-format stream-json --verbose` and pipes the stream through a
compacting filter (`claude-events-filter.mjs`) straight into the job trace:
tool calls, assistant messages, API errors and retries appear in the GitLab
job log *while the harness works*. The full-fidelity stream is kept at
`/tmp/claude-events.jsonl` inside the job. A healthy run shows a steady flow
of `tool:`/`say:`/`think:` lines; `API-ERROR retry N/M: ...` lines mean
network/provider trouble. The same pattern generalizes to any headless
harness that can stream machine-readable progress (opencode, copilot cli).

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
