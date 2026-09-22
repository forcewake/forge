# The durable discovery stage on `/implement` (NXT-05)

Status: **landed.** The splice below shipped with the NXT-05 slice and is
live in `GitHubRunService._plan_and_publish`
(`src/forge/runs/github_service.py`), gated by `FORGE_DISCOVERY_ENABLED`
(default OFF — the honest rollout). R28-15 (review 1ae5290 §5.7) then
froze the production call on ONE immutable source set; this document now
describes the call as it exists, not a patch to apply.

The review's M1 goal: a real `/implement` run performs durable
discovery, produces an evidence artifact, and the plan carries
verifiable citations. `LLMPlanner.plan()` alone would plan from the issue
title/description only (12 000-char input cap);
`maybe_run_discovery` is the seam that puts the evidence in front of it.

## The seam

`forge.adaptive.discovery_stage.maybe_run_discovery(run_ctx, planner_input) -> planner_input`

- **Disabled (the default, `FORGE_DISCOVERY_ENABLED` unset/0):** returns
  the planner input byte-for-byte unchanged, touches no database, and —
  because the context loads its repository snapshot lazily — never even
  reads the provider. The classic workflow is preserved exactly.
- **Enabled:** runs (or adopts) the durable stage — persists the
  discovery record (durable identity + read-only dispatch intent)
  BEFORE the probes, runs the bounded read-only `SnapshotToolbox`
  probes over the frozen snapshot, writes the evidence artifact into
  the content-addressed store (when the context carries one), journals
  `discovery.started` / `discovery.replayed` / `plan.research_mode`
  outbox rows in the same transactions, and returns the input with a
  delimited, bounded evidence digest section appended:

  ```
  <<<FORGE_DISCOVERY_EVIDENCE
  {"dropped":0,"evidence":[{"detail":"LLMPlanner","id":"ev-1","kind":"symbol",
   "line":1,"path":"src/app/planner.py"}, ...],"rules":"Plan steps may cite
   these entries as evidence:<id>. ...","schema":"forge.discovery.digest/1", ...}
  FORGE_DISCOVERY_EVIDENCE>>>
  ```

  A completed discovery is adopted on restart (no probes re-paid); a
  dispatched-but-never-completed record recovers under the same
  discovery id; a FAILED one raises `DiscoveryStageError` — never a
  silent fallback to an unresearched plan.

- **Citations (NXT-06):** the digest's evidence ids are the citation
  vocabulary; plan steps may cite `evidence:<id>`, and citation
  AUTHORITY is validated against the recorded snapshot tree at read
  time (cross-repo, stale-OID, out-of-scope and out-of-range citations
  fail closed).

## The production call (as landed, R28-15-frozen)

File: `src/forge/runs/github_service.py`, method `_plan_and_publish`.
Two properties the R28-15 review found missing and the call now has:

1. **One immutable source set.** The base SHA is resolved FIRST — before
   the typed config read, before the discovery context is built — and
   that ONE SHA is the ref for the config read, the discovery snapshot
   and the attempt base frozen into the RunSpec. The base is never
   re-read after planning: a branch that moves mid-planning cannot mix
   two snapshots into one approved plan. (When the branch head cannot
   be read, the run keeps the legacy honesty — no frozen base — and the
   reads fall back to the movable branch name.)

   ```python
       base_sha = await self._read_base_sha()
       frozen_ref = base_sha or self._target_branch()
       config_read = await read_project_config(
           self._stack.reader, project_id, ref=frozen_ref
       )
       ...
       issue_description = await maybe_run_discovery(
           DiscoveryRunContext.from_reader(..., ref=frozen_ref, ...),
           issue_description,
       )
   ```

2. **Inside the planning try.** The splice sits INSIDE the
   `try`/`except (LLMError, LLMResponseError, DiscoveryStageError)` that
   parks the run `failed(planning_failed: …)` and posts the operator
   note. A discovery failure (failed probes, unanswered clarification
   questions) gets the deliberate run-state handling — never a silently
   stuck preflight run and never an escape to the generic webhook
   wrapper.

Why this point in the flow: the typed config read and `path_scope`
resolution are already done, so discovery respects the monorepo scope;
the budget is already open and is untouched — discovery is not a paid
LLM call, it is bounded local read-only tool execution; and the planner
call that follows consumes the augmented `issue_description` unchanged
(`LLMPlanner.plan` concatenates title + description and truncates at
12 000 chars; the digest is capped at 4 000 chars and `attach_digest`
keeps the combined prompt within the planner cap).

## Rollout

1. The stage stays OFF by default; enabling it is a per-environment
   decision (`FORGE_DISCOVERY_ENABLED=1` on ONE supported
   provider/profile combination — GitHub /implement first).
2. A `ContentAddressedStore` can be wired into the context when the
   artifact volume justifies it (without one, the compact citation
   records ride the run's evidence blob — the artifact digest stays
   empty).

## Deferred (tracked separately)

- Dispatching the probes through the actual CI execution profile
  (`dispatch_target()` is recorded as the intent; the in-slice executor
  is the read-only `SnapshotToolbox`, which performs no writes, no
  shell, and no network egress beyond the repository read surface).
- The research-harness discovery mode (the review §7 three honestly
  named modes: unresearched fast path, lexical discovery,
  research-harness discovery) — lexical is what this stage implements.
