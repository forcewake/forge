# ADR-0023: Task-aware harness selection (frozen chain, gate-approved)

Status: accepted (2026-09-15), grounded in
[research/harness-selection.md](../research/harness-selection.md)
Context: today the implementer harness is pinned per project
(`FORGE_IMPLEMENTER_BACKEND=ci_harness[:driver]`, tighten-only per
[ADR-0015](0015-pluggable-implementer-backends.md)). Four shipped drivers
(claude-code, grok-build, opencode, copilot) differ in capability and
economics (~10x per-run cost spread; F22 receipt quality differs per
driver), and a project pinned to one of them runs the wrong tool for a
docs one-liner and for a multi-file refactor alike. Research finding: the
industry splits into learned/provider-side per-prompt routers
(RouteLLM, OpenRouter `:auto`, Copilot auto — explicitly non-deterministic,
post-hoc visibility) versus declared-policy routing (LiteLLM config,
Claude `fallbackModel`, CI runner tags/labels — deterministic, visible,
fail-visibly). Forge's harnesses ARE CI jobs, and CI has no auto router
anywhere: selection is declared intent. Forge is family two by
construction ([ADR-0009](0009-human-gates-authorize-specific-decision.md),
[ADR-0011](0011-config-never-delegates-security-downward.md),
[ADR-0018](0018-immutable-run-spec.md)).

## Decision

1. **Selection happens at PLAN TIME and is frozen into the RunSpec — it
   is part of the decision the human gate approves.** The RunSpec's
   `backend_config` gains: `harness` (selected driver), `harness_fallbacks`
   (frozen ordered subset of the project's list; default `[]`), and
   `budget_class` (planner's cost/complexity estimate:
   `trivial | standard | heavy`). The plan comment gains a short
   **"Implementation" block** — selected harness + model, fallbacks,
   budget class, commit cycles — so `/go` authorizes the execution shape,
   not just the plan text (today the harness is invisible at the gate;
   this *strengthens* ADR-0009). All three fields join the policy digest:
   changing them invalidates pending gates, exactly like plan drift.

2. **Deterministic compilation first, LLM proposal inside it.** The
   project's scalar backend becomes a tighten-only ordered preference
   list (`.forge.yml` / `FORGE_HARNESS_PREFERENCE`); a length-1 list
   reproduces today's behavior byte-for-byte. The compiler intersects the
   list with the lanes the project actually onboarded (credential
   presence — `forge doctor` already knows each driver's variables). The
   planner may select any entry of the list and set `budget_class`, with
   a one-line reason that lands in the spec and the plan comment — a
   proposal can reorder, never extend. The planner is never the
   authority.

3. **Dispatch-time fallback: OFF by default; spec-contained when on.**
   Opt-in per project. When enabled it only "executes the spec's next
   entry", and only on deterministic, classified signals:
   `infrastructure`-kind harness failures (ADR-0008 classification —
   auth/quota/timeout patterns, never code failures), with LiteLLM-style
   hysteresis (N failures in a window before an entry is skipped). Only
   before any candidate exists. Every advance is journaled (`action_log`)
   and stated in the evidence comment; the F22 budget reservation is
   cancelled and re-reserved per switch. Anything else fails visibly and
   waits for a human — the CI-native posture (stuck job, not silent
   reroute). Reproducibility is defined as *same RunSpec → same
   selection*: the list, the selection, and the fallback policy are all
   frozen at gate time.

4. **Repair stays on the selected harness (v0.9).** A CI-code failure is
   a signal about the CHANGE, not about the harness (ADR-0008).
   Repair-leg switching between cycles is a v1.0+, per-project opt-in
   extension — never mid-candidate: one frozen attempt base → one
   candidate → one producer (ADR-0016).

5. **Telemetry ranking is a suggestion surface, not an actor.** Once
   per-project samples exist (v1.0+), `forge doctor` reports per-driver
   acceptance rates from the delivery ladder (candidate → CI-passed →
   ready → merged-without-rework, ADR-0021 §3) and the usage ledger as
   the cost feed — "on dependency-upgrade packages, opencode merged 4/5
   without rework vs claude-code 1/5". A human applies any reorder as
   config. The compiler never auto-reorders.

## Rejected

- **Learned per-request routers (RouteLLM-style) inside forge.** Trained
  on chat preferences (85% cost cut at ~95% quality is real — for chat);
  forge routes durable work packages judged by merge outcomes, with tiny
  per-project samples and days-weeks label latency. Non-deterministic
  across runs; wrong unit, wrong feedback loop.
- **Market/aggregate-spend routing (OpenRouter `openrouter/auto`).** "Can
  pick a different model on every turn" — an opaque third-party signal
  cannot appear in a human-approved RunSpec.
- **Dispatch-time LLM routing.** Spends tokens to choose the model,
  pre-gate — re-introduces the spend-before-admission problem ADR-0018
  killed.
- **Routing inside a run** (mid-candidate or per tool call). Breaks the
  candidate contract's single-producer invariant and the usage ledger's
  driver attribution.
- **Global (cross-project) auto-selection without project config.**
  Violates tighten-only (ADR-0015) and ADR-0011: a harness cannot be
  selected if the project has no credentials for it. A factory-supplied
  default list is acceptable only as something projects may tighten.
- **Provider-side "Auto" tiers as a default model route.** Permitted only
  as an explicit, gate-disclosed per-project opt-in ("model is
  non-deterministic within this tier"); the default remains explicit
  pins.

## Consequences

- `.forge.yml` / env gain the ordered preference list (default = today's
  single backend → byte-identical for existing projects); RunSpec
  schema_version bumps; the dispatch path already parameterizes the
  driver into the lane (per-driver template include, `FORGE_DRIVER`
  input), so v0.9 lands without CI changes.
- `forge doctor` extends its per-driver credential checks into the
  compiler's allowed-lane input.
- Phase 1–2 are v0.9 work; Decision 4's switching and Decision 5's
  ranking are v1.0+, gated on telemetry volume.
