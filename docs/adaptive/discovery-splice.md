# Splicing the durable discovery stage into `/implement` (NXT-05)

Status: **patch note — NOT yet landed.** The stage lives in
`src/forge/adaptive/discovery_stage.py` and is fully tested
(`tests/test_adaptive_discovery_stage.py`); this document is the exact,
minimal change the orchestrator lands in the production path. Until it
is landed, the stage is reachable only through its own API and tests.

The review's M1 goal: a real `/implement` run performs durable
discovery, produces an evidence artifact, and the plan carries
verifiable citations. Today `LLMPlanner.plan()` plans from the issue
title/description only (12 000-char input cap) and
`GitHubRunService._plan_and_publish` invokes it directly — the
`DiscoveryService`/`DiscoveryRun` substrate is never on that path.

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

- **Citations (NXT-06 slice):** after the planner returns, call
  `enforce_plan_citations(plan, evidence_ids_of(record))` where `record`
  is the run's `evidence["discovery"]` blob. Steps may cite
  `evidence:<id>`; a citation that does not resolve to a recorded
  evidence id raises `InvalidPlanCitation` (fail-closed), while uncited
  claims stay allowed.

## The patch (exact insertion point)

File: `src/forge/runs/github_service.py`, method `_plan_and_publish`,
between line 527 (`await self._apply_run_budget(run_id)`) and line 528
(`try:`), i.e. immediately before the `plan = await
self._stack.planner.plan(` call:

```python
        # NXT-05: optional durable discovery before planning — OFF by default
        # (FORGE_DISCOVERY_ENABLED); see docs/adaptive/discovery-splice.md.
        issue_description = await maybe_run_discovery(
            DiscoveryRunContext.from_reader(
                run_id=run_id,
                project_id=project_id,
                session_factory=self._session_factory,
                reader=self._stack.reader,
                ref=self._target_branch(),
                repository_id=self._repo_full_name,
                allowed_globs=path_scope or None,
            ),
            issue_description,
        )
```

Plus the import at the top of the file:

```python
from forge.adaptive.discovery_stage import DiscoveryRunContext, maybe_run_discovery
```

And one recommended line change so a discovery failure parks the run
instead of bubbling to the webhook handler — extend the existing
except clause at line 535:

```python
        except (LLMError, LLMResponseError, DiscoveryStageError) as exc:
```

(with `DiscoveryStageError` added to the same import). The stage
persists its own `failed` record before raising, so the planning-failed
handling finds the durable evidence of why.

Why this point in the flow: the typed config read and `path_scope`
resolution are already done (line 508), so discovery respects the
monorepo scope; the budget is already open (line 527) and is untouched
— discovery is not a paid LLM call, it is bounded local read-only tool
execution; and the planner call that follows consumes the augmented
`issue_description` unchanged (`LLMPlanner.plan` concatenates title +
description and truncates at 12 000 chars; the digest is capped at
4 000 chars and `attach_digest` keeps the combined prompt within the
planner cap).

## Rollout

1. Land the patch with the stage OFF (the default) — production
   behavior is byte-for-byte identical; the tests prove the disabled
   path writes nothing and reads nothing.
2. Enable per environment with `FORGE_DISCOVERY_ENABLED=1` on ONE
   supported provider/profile combination (GitHub /implement first).
3. Wire a `ContentAddressedStore` into the context when the artifact
   volume justifies it (without one, the compact citation records ride
   the run's evidence blob — the artifact digest stays empty).

## Deferred (tracked separately)

- **NXT-07** — persisted clarification questions and answer-gated
  redispatch. The record carries no question workflow yet;
  `waiting_question`/`blocked` handling stays the caller's concern.
- Full NXT-06 authority binding (cross-repository/cross-host OID
  verification, revoked-access checks) — this slice validates citation
  RESOLUTION against the recorded evidence ids only.
- Dispatching the probes through the actual CI execution profile
  (`dispatch_target()` is recorded as the intent; the in-slice executor
  is the read-only `SnapshotToolbox`, which performs no writes, no
  shell, and no network egress beyond the repository read surface).
