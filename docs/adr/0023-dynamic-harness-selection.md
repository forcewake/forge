# ADR-0023: Task-aware harness selection (frozen chain, gate-approved)

Status: proposed (2026-09-15) — pending review-ack of
[research/harness-selection.md](../research/harness-selection.md)
Context: today the implementer harness is pinned per project
(`FORGE_IMPLEMENTER_BACKEND=ci_harness[:driver]`, tighten-only per
[ADR-0015](0015-pluggable-implementer-backends.md)). Four shipped drivers
(claude-code, grok-build, opencode, copilot) have different strengths
(agentic depth vs speed vs cost vs subscription economics), and a project
pinned to one of them uses the wrong tool for a docs one-liner and the
wrong one for a multi-file refactor alike.

## Decision

1. **Selection happens at PLAN TIME and is frozen into the RunSpec — it
   is part of the decision the human gate approves.** The RunSpec document
   gains a `harness_chain` field: an ordered list of
   `{driver, model_route}` entries, compiled at plan acceptance from the
   project's configured harnesses. The plan comment shows the chosen
   entry ("will run on **claude-code** · glm-5.3-flash; fallback:
   grok-build") and its rationale — consistent with [ADR-0009](0009-human-gates-authorize-specific-decision.md)
   (the gate authorizes a specific decision) and [ADR-0018](0018-immutable-run-spec.md)
   (the spec is immutable; `spec_digest` binds it). `/go` approves the
   chain, not just the plan text.

2. **Deterministic compilation first, LLM proposal second.** Phase 1:
   the chain is compiled deterministically from `.forge.yml`
   (`implement.harnesses: [claude-code, grok-build]` — an ordered subset
   of lanes the project has actually configured credentials for; the
   default single backend is exactly a one-element chain, so current
   projects are byte-compatible). Phase 2: the planner may PROPOSE a
   different head of the chain as a structured plan field (with
   rationale); the compiler intersects the proposal with the configured
   subset — a proposal can reorder, never extend. The planner is never
   the authority: it proposes inside a human-configured envelope.

3. **Dispatch-time switching is deterministic, journaled, and bounded to
   the frozen chain.** Between chain entries the runtime may advance ONLY
   on classified, durable signals: `harness_infrastructure` failures
   (ADR-0008 classification), provider outage/cooldown
   (LiteLLM-Router-style project-scoped cooldowns — time-bounded,
   persisted), harness-credential absence, or budget exhaustion for the
   current model route (F22 reservation refusal). Every advance is a
   journaled action (`action_log`) and is stated in the run's evidence
   comment ("attempt 2 ran on grok-build because claude-code hit
   harness_infrastructure"). Position in the chain is durable run state —
   the RunSpec itself is never mutated.

4. **Repair legs may switch harnesses under the same budget.** A repair
   cycle (commit_cycle) re-enters the chain at the current position
   (or advances it under Decision 3's rules) — a CI-code failure is a
   signal about the CHANGE, not about the harness; an infrastructure
   failure is a signal about the harness. The distinction stays exactly
   ADR-0008's.

5. **Telemetry-driven ranking is a later phase, and only as a default
   ordering hint.** The delivery-ladder / acceptance telemetry (v0.7
   evidence, v0.8 `forge_delivery_ladder`) is recorded per driver; once
   per-project samples exist, the COMPILER (Decision 1) may use
   per-driver acceptance rates to reorder the DEFAULT chain. Ranking is
   computed offline from durable evidence — never an in-request learned
   router.

## Rejected

- **Learned per-request routers (RouteLLM-style) inside forge.** RouteLLM
  routes individual chat completions with a preference-trained
  classifier; forge routes durable work packages that cost minutes, run
  in CI, and end in human-reviewed evidence. The unit of routing and the
  feedback loop are both wrong; also non-deterministic across runs.
- **Market/aggregate-spend routing (OpenRouter `openrouter/auto`).** An
  opaque third-party signal cannot appear in a human-approved RunSpec —
  contradicts ADR-0009 auditability.
- **Dispatch-time LLM routing.** Spending model calls to choose the
  model, before admission, re-introduces the spend-before-gate problem
  ADR-0018 killed.
- **Routing mid-run per tool call.** A run is one coherent agent
  execution on one candidate branch; switching drivers mid-flight
  destroys the working-tree continuity the candidate contract depends on.
- **Global (cross-project) auto-selection without project config.**
  Violates tighten-only (ADR-0015) and project-owned credentials: a
  harness cannot be selected if the project has no credentials for it.

## Consequences

- `.forge.yml` gains `implement.harnesses` (ordered list; default =
  today's single backend → byte-identical behavior for existing
  projects).
- RunSpec schema_version bumps; `harness_chain` + compiler inputs join
  the spec digest, so tampering with the chain invalidates the gate
  (same as plan drift today).
- Forge checks lane credentials per driver at onboarding (`forge doctor`
  already knows each driver's variable names) — the compiler only ever
  emits lanes that passed.
- Phase 1–2 land without CI changes: the dispatch path already
  parameterizes the driver into the lane (template include per driver,
  `FORGE_DRIVER` input on Actions).
