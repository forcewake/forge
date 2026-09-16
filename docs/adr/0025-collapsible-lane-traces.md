# ADR-0025: Collapsible structured lane traces

Status: accepted (2026-09-16)
Context: the lane job log is the human's window into what the coding agent
is doing — and a raw NDJSON stream is unreadable (the first dogfood run
flooded 3000+ lines of stream-json, hiding the one failed test among
`thinking_tokens` telemetry spam). The research base is
[research/harness-log-formats.md](../research/harness-log-formats.md)
(event schemas for all four drivers, verified against official docs).

## Decision

1. **One universal log filter** (`ci/templates/harness-log-filter.mjs`,
   driver via argv) renders every driver's stream into a single line
   grammar: timestamped tool calls (`🔧` start, `✅`/`❌` result with size
   or a ≤120-char error preview), throttled thinking/prose, `⏳` retries,
   turn separators with token math, counted suppression of routine events,
   and a summary footer ending in the byte-compatible `FORGE_USAGE:`
   receipt ([ADR-0016](0016-candidate-bundle-trusted-publisher.md) F22
   lite). Old per-driver filters are deleted; opencode gains
   `--format json` (which also closes its usage-null gap), and the claude
   filter's dead `api_error` branch is corrected to the documented
   `api_retry` shape.
2. **Collapsible groups per platform, decided by `FORGE_LOG_PLATFORM`:**
   - **GitHub Actions** — `::group::<title>` / `::endgroup::`
     [documented workflow commands]. Collapsed by default: the viewer sees
     one line per tool call; expanding shows the full tool output.
   - **GitLab CI** — `section_start:<epoch>:<id>[collapsed=true]` /
     `section_end:<epoch>:<id>` ANSI framing
     [documented custom collapsible sections]. `[collapsed=true]` gives the
     same collapsed-by-default behavior.
   - **Azure DevOps** — no log-section commands exist [documented]: the
     filter renders flat grammar lines (`flat` platform). The full log
     still downloads from the completed run.
   A tool call OPENS a group (title = the one-line summary) and the result
   CLOSES it with the full output inside — collapsed shows the trace,
   expanded shows the actual command output.
3. **The agent may run the repo's own tests**: the quality bar demands it
   (ADR-0008), so the allowlists now carry the repo's test/lint commands
   (`pytest`, `uv run pytest`, `uv run ruff`, `uv run mypy` — scoped to the
   in-repo venv + uv) on every driver. Everything else stays auto-denied;
   the mechanical commit/push deny is untouched. (LIVE-found: the dogfood
   candidate flailed on permission denials because "run the tests" was in
   the brief while pytest was not allow-listed.)
4. **Platform selection is per lane**: Actions/AzDO entry defaults to
   `actions`; GitLab templates set `gitlab`; the AzDO lane sets `azdo`
   (flat).

## Consequences

- Collapsed: one line per tool call — the "17:33:08 ✅ Bash grep …" view.
  Expanded: the full tool output, in place, chronologically.
- The full-fidelity event stream remains in the lane's events file
  (teed BEFORE the filter) and in the candidate artifact — nothing is
  lost, only re-rendered.
- The filter degrades to raw passthrough if it cannot start (unknown
  driver exits non-zero → the lane falls back), never blocking a
  candidate.
- Non-goals: coloring beyond glyphs (terminal-dependent), AzDO collapsing
  (impossible without platform support), streaming collapse markers for
  AzDO timeline groups (per-task grouping is native and sufficient).
