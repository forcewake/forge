# Dynamic Harness Selection — Research (September 2026)

Purpose: should forge pick the implementer harness **per task** (quick doc fix vs multi-file refactor vs dependency upgrade vs migration) instead of the current per-project pin (`FORGE_IMPLEMENTER_BACKEND=ci_harness:claude-code`, tighten-only)? This doc surveys task-aware routing prior art (§1), lays out the option space against forge's iron principles (§2), recommends a phased rollout (§3), and closes with risks (§4) and rejected options (§5). Interface ground truth stays in `harness-interfaces.md` (HI), configuration policy in `harness-config-best-practices.md` (HCBP); both are cross-referenced, not redone.

Tagging: **[documented]** = verified against a cited live source in September 2026. **[observed]** = forge's own code, templates, ADRs, or previously verified findings. **[inference]** = this document's judgment. **[unverified]** = could not be confirmed from official sources.

---

## 1. Prior-art survey: what actually routes on what

### 1.1 RouteLLM (LMSYS) — learned per-query routing

**What it routes on [documented]:** a binary strong-vs-weak model choice per query. Routers are trained on Chatbot Arena human preference battles (~55k, augmented with golden-label datasets such as MMLU and an LLM judge — under 2% of total data) — there are no explicit "difficulty" labels; the router learns *which model wins on prompts similar to this one*.

**Router types [documented]:** similarity-weighted (SW) ranking, matrix factorization, BERT classifier, causal LLM classifier. A serving-time threshold tunes the cost/quality point.

**Results [documented]:** routing GPT-4 Turbo vs Mixtral 8x7B at a 95%-of-GPT-4-quality target: cost cuts of over 85% on MT Bench, 45% on MMLU, 35% on GSM8K; matrix factorization hit the target with only 14–26% of calls on the strong model; routers generalized to unseen pairs (Claude 3 Opus vs Llama 3 8B) without retraining.

**Determinism/auditability [inference]:** the decision is a learned score per prompt — observable post-hoc (which model served) but not explainable in operator terms; and the training distribution is *chat preferences*, not agentic software-engineering outcomes, so transfer to "which harness merges without rework" is unproven. There is no notion of an approval-bearing decision to route.

### 1.2 LiteLLM Router — config-declared deployment routing

**What it routes on [documented]:** explicit config, not learning. Strategies: `simple-shuffle` (default; weighted/random pick), `least-busy`, `usage-based-routing` (lowest TPM), `latency-based-routing`, `cost-based-routing` (cheapest deployment; custom `input_cost_per_token`/`output_cost_per_token`).

**Reliability semantics [documented]:** `allowed_fails` (default 3) + `cooldown_time` (default 5 s) put a deployment on cooldown; 429s cool down immediately, as do >50% failures in a minute and 401/404/408. `fallbacks` is an ordered list of model groups; `order` ranks deployments within a group; weighted failover is capped by `max_fallbacks` (default 5). `enable_pre_call_checks` filters deployments whose context window is smaller than the messages. Retries stay inside the failing model group; fallbacks escalate to the next group.

**Determinism/auditability [documented]:** explicitly non-deterministic in the default strategy ("randomly picks a deployment" without rpm/tpm), state-dependent otherwise. The routing *table* is config-declared and loggable — the policy is the operator's even when the outcome varies.

### 1.3 OpenRouter Auto Router (`openrouter/auto`) — provider-side opaque routing

**How it picks [documented]:** per prompt, "a fast, lightweight classifier assigns each prompt one of ~30 fine-grained task types" (e.g. `code:debugging`); models are then ranked by "aggregate anonymized spend statistics" over a trailing 7-day window ("the wisdom of the market") within a chosen `cost_tier`; account constraints (`allowed_models`, ZDR, modality) apply afterwards.

**Determinism/auditability [documented]:** rankings shift "within days, with no retraining" and "the Auto Router can pick a different model on every turn" (mitigated only by session stickiness). Auditability is opt-in metadata (`X-OpenRouter-Metadata` exposes the classified task type); there is no documented rationale for the ranking and the fallback list is not inspectable.

**Lesson [inference]:** this is precisely the "silent provider-side choice" forge's principles exclude: non-reproducible by construction, decided outside operator config, and visible only after the fact.

### 1.4 GitHub Copilot — auto model selection and the CLI `--model` flag

**Mechanism [documented]:** auto combines two systems — real-time model health/availability and task-complexity evaluation — and "routes the task to the optimal model." Routing happens "along natural cache boundaries to avoid additional cache related costs," because mid-session model switching "shown increased cost without ample improvements in quality."

**Tiers [documented]:** Efficiency / Balance / Intelligence are routing preferences (same models in each tier; "A simple prompt could still be routed to a smaller model"), available in VS Code, **Copilot CLI**, and the Copilot app. Task-optimization auto is GA in Copilot Chat on github.com, VS Code, Copilot CLI, the Copilot app, and the cloud agent.

**Billing and visibility [documented]:** usage "is charged based on the model auto selects, regardless of tier," with a 10% discount for paid plans while using auto; the chosen model is displayed *after the fact* (Copilot CLI: in the terminal). There is no way to pre-approve a specific model for a specific task. Copilot CLI also documents an explicit `--model` option (and interactive `/model`) [documented]. Legacy request-based billing multiplies one premium request per user prompt by a per-model rate (Pro 300/month, Pro+ 1500/month); GitHub is transitioning toward usage-based billing [documented].

**Lesson [inference]:** even the vendor's own auto mode keeps the decision inside the provider boundary with post-hoc visibility. Forge can consume the explicit `--model` pin (it does, HCBP §8) but cannot put "Auto" inside a pre-approved execution shape.

### 1.5 Claude Code `fallbackModel` — the static ordered chain

**Semantics [documented]:** `fallbackModel` is "Name backup models for when the primary is overloaded" (settings reference); it is "an ordered chain where position carries meaning, so Claude Code takes the whole value from the highest-precedence file that defines it" — no cross-file merging (managed > CLI `--settings` > local > project > user). The trigger condition is narrow: primary *overloaded* — it is an availability fallback, not a cost/quality router. The Agent SDK exposes the same knob as `fallback_model` [observed — HI §2].

**Lesson [inference]:** this is the closest vendor-native shape to what forge should build: static, config-declared, condition-limited, ordered — and it is already on forge's HCBP §7 item-8 upgrade list.

### 1.6 Agentic-IDE tools — Roo Code / Cline / Kilo Code / Aider

- **Roo Code [documented]:** per-mode model assignment — "Each mode remembers your last-used model," and users can "assign different models to different modes (e.g., Gemini 2.5 Preview for Architect mode, Claude Sonnet 3.7 for Code mode)"; switching modes switches models. A human-curated static table keyed on task *class*, not a learned router.
- **Cline [unverified]:** no documented auto-routing found in official docs; a single user-selected model. Secondary sources describe per-task routing arriving via forks, not Cline itself.
- **Kilo Code [observed]:** ships an "Auto model" routing feature (secondary reviews + changelog entries; not verified against official docs — model shown in the selector for every Auto choice, i.e. post-hoc disclosure).
- **Aider [documented]:** role-split models inside one job — the main chat model, `--editor-model` (applies the architect model's proposed edits), and `--weak-model` (cheap auxiliary work such as commit messages). Routing by *role*, static and explicit.

**Lesson [inference]:** the dominant real-world pattern in coding tools is not per-task inference — it is a human-curated mapping from task class or role to model. That is exactly the pattern forge can audit.

### 1.7 CI-native routing — the precedents that matter most

Forge harnesses **are** CI jobs (ADR-0015/0020), so CI's own routing idioms are the strongest precedent:

- **Temporal [documented]:** task queues pair work with workers; capability routing is realized as "dedicated task queues that only matching workers poll," plus a worker-specific task-queue pattern for host/capability affinity. Static topology, durable, and the routing decision is visible in which queue received the task.
- **Celery [documented]:** `task_routes` is a static mapping (dict/glob/regex) plus optional router callables ("a function that decides the routing options for a task") — dispatch-time logic is possible but programmable and operator-owned. (The routing page itself does not document "routing by capability"; capability routing is a convention built on dedicated queues.)
- **GitLab runner tags [documented]:** "For a runner to be selected to run a job, it must have all of the tags defined in the job script block." A job whose tags match no runner is **stuck** — pending, visibly, until a runner matches or it is cancelled. Tags may reference CI/CD variables (selection resolved at dispatch), but matching stays AND-based and config-declared. There is no fail-over to "some other runner."
- **GitHub Actions labels [documented]:** `runs-on` labels AND-match (`[self-hosted, linux, x64]` requires all three on one runner); `self-hosted` and OS/arch are default labels; and labels are **not validated** — "GitHub Actions accepts them as given and does not validate that the runner is actually using that operating system or architecture." No matching runner → the job queues indefinitely (the well-known "Waiting for a runner to pick up this job" state) [observed].

**Lesson [inference]:** CI routing is statically configured capability targeting with **fail-visible** semantics (stuck/queued jobs), not fail-over to whatever is available. The CI layer has no auto router anywhere — selection is the pipeline author's declared intent. That is the family forge belongs to.

### 1.8 Convergence

| System | Routes on | Decision point | Deterministic | Auditable |
|---|---|---|---|---|
| RouteLLM | learned prompt difficulty (Arena prefs) | per query | threshold-tuned, not reproducible | weak (opaque score) |
| LiteLLM Router | config: cost/latency/state/fallbacks | per request | partially (random/state) | yes (config + logs) |
| OpenRouter `:auto` | classifier + 7-day market spend | per prompt/turn | no ("different model on every turn") | weak (opt-in metadata) |
| Copilot auto | task complexity + provider health | per task/turn | no | post-hoc model display |
| Claude `fallbackModel` | overload of the primary only | session start | yes (ordered chain) | yes (settings file) |
| Roo/Aider | human-curated mode/role → model table | per mode/role | yes | yes (config) |
| Temporal / Celery / GitLab tags / Actions labels | declared capability (queue/route/tag/label) | per task dispatch | yes (AND matching) | yes (pipeline config) |

Two families exist. **Family 1** — learned or provider-side per-prompt routers optimizing a cost-quality trade-off (RouteLLM, OpenRouter, Copilot auto): non-deterministic, opaque, decided inside the provider boundary. **Family 2** — declared-policy routing (LiteLLM config, fallback chains, mode tables, CI tags/labels): config-owned, ordered, deterministic given the declaration, and failing *visibly* when nothing matches. Forge's iron principles — auditable decisions, no silent provider-side choices, tighten-only config, a human gate that authorizes one specific decision — put forge in family 2 by construction. The open question is not *whether* to route dynamically, but *where in forge's own decision ladder* the dynamics live.

Sources: lmsys.org/blog/2024-07-01-routellm/; arxiv.org/abs/2406.18665; github.com/lm-sys/routellm; docs.litellm.ai/docs/routing; docs.litellm.ai/docs/proxy/reliability; openrouter.ai/docs/guides/routing/routers/auto-router; docs.github.com/en/copilot/concepts/models/auto-model-selection; docs.github.com/copilot/concepts/agents/about-copilot-cli; docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference; docs.github.com/copilot/managing-copilot/monitoring-usage-and-entitlements/about-premium-requests; docs.github.com/copilot/reference/copilot-billing/models-and-pricing; code.claude.com/docs/en/settings; code.claude.com/docs/en/settings-reference; roocodeinc.github.io/Roo-Code/basic-usage/using-modes (redirect of docs.roocode.com); aider.chat/docs/config/options.html; aider.chat/docs/usage/modes.html; docs.temporal.io/task-queue; docs.temporal.io/design-patterns/worker-specific-taskqueue; docs.celeryq.dev/en/latest/userguide/routing.html; docs.gitlab.com/ci/runners/configure_runners/; docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/using-labels-with-self-hosted-runners; github.com/actions/runner/issues/3609.

---

## 2. The forge option space

### 2.0 Ground rules and current mechanics [observed]

- **ADR-0015:** backend selection is per-project configuration, tighten-only under ADR-0011; harnesses execute as jobs in the *target project's* CI; the readonly reviewer, quality contract, human gates, evidence and audit stay backend-independent; "multiple harnesses can coexist and be compared per project" is already a stated positive.
- **ADR-0018:** the RunSpec is frozen at plan acceptance and carries `backend_config`; dispatch inputs come FROM the document; "the run executes its spec or requests re-approval." Budget reservation happens before dispatch (F22).
- **ADR-0009:** the gate authorizes one specific decision, bound to plan digest + base SHA + effective policy digest; a changed policy invalidates the approval.
- **Code today:** `FORGE_IMPLEMENTER_BACKEND=builtin|ci_harness[:driver]` → `build_backend()` (`src/forge/runs/backends.py`, driver defaults `claude-code`); `FORGE_HARNESS_MODEL` flows as a GitLab pipeline variable / Actions `model` input; the RunSpec `backend_config` carries `backend`, `model`, `target_branch` (+ `harness_workflow`, `driver` on the Actions lane, `src/forge/runs/github_service.py`); `policy_digest` binds `harness_model` (+ `harness_driver`); F22 budgets (`wallclock_s`/`max_calls`/`max_tokens`, plus RunSpec `budgets.commit_cycles`/`harness_timeout`) are reserved before dispatch (`src/forge/durable/budgets.py`); `_HARNESS_INFRASTRUCTURE_PATTERNS` already classify auth/quota/timeout failures as `infrastructure`, which never trigger a repair.
- **Gap:** the plan comment (`_plan_comment`, `src/forge/runs/service.py`) shows plan + digest + `/go` + approvers — **not** which harness/model/budget the approver is authorizing. Today a `/go` approves a plan whose executor is pinned invisibly by project env.

### 2.1 Harness economics that constrain any selector

Facts reused from HI + HCBP, re-verified where noted:

| Constraint | claude-code | grok-build | opencode | copilot CLI |
|---|---|---|---|---|
| Model pin | `--model` / `ANTHROPIC_MODEL` [documented, HI §2] | `-m` [documented, HI §3] | `-m provider/model` [documented, HI §4] | `--model` [documented, §1.4] |
| Provider-side auto | none; static `fallbackModel` chain (overload only) | none documented | none documented | "Auto" tier routing, provider-decided [documented] |
| Native budget flags | `--max-turns`, `--max-budget-usd` | `--max-turns` (no dollar flag) | none CLI-side (server abort) | none documented |
| Usage receipt quality | per-turn `result.usage` + `total_cost_usd` (client-side estimate) | best-in-class: per-turn + aggregate `modelUsage`/`total_cost_usd`, `cost_is_partial` marker | none on CLI stdout (server API only) → forge records `usage: null` | none parseable (issue #52) → `usage: null` |
| Cost model | API key pay-per-token; Pro/Max subscription (5-hour rolling + weekly caps, shared with Claude apps) is ToS-limited for automation; gateway tier mapping (z.ai) | `XAI_API_KEY` pay-per-token vs OAuth subscription bundle (session token precedence footgun, HI §3) | BYOK provider keys via config `{env:}` | Copilot subscription behind the PAT; premium requests × per-model multiplier; 10% auto discount [documented] |
| Unattended failure shape | denied asks land in `permission_denials`; SIGTERM→143 | hangs without `--always-approve` (forge-verified); optionalDependency hang | `ask` hangs; exit codes undocumented | un-allowed tools fail without prompting |

**Implications [inference]:** (1) cost per run differs by an order of magnitude across drivers for the same task — subscription-subsidized Copilot vs metered API lanes; (2) F22 reconcile quality depends on the driver (exact-ish for grok/claude, unknown for opencode/copilot), so per-harness budget enforcement strength differs; (3) only Copilot offers a provider-side auto mode — for forge that is "the driver delegates the selection decision," which is §5-rejected as a default.

### 2.2 Option (a): static per-project ordered preference list

Replace the scalar `ci_harness:<name>` with an ordered list (e.g. `FORGE_HARNESS_PREFERENCE=ci_harness:claude-code,ci_harness:opencode`), tighten-only, length-1 = today's behavior.

- **Pros [inference]:** pure config — digest-able, tighten-only-friendly, zero new decision logic; it is the substrate every other option needs (you cannot fall back, propose, or rank without a list); ADR-0015 explicitly anticipates coexistence and comparison.
- **Cons [inference]:** static — the same harness serves a one-line doc fix and a migration campaign; the list alone does not say who picks when entry 1 is unavailable.

### 2.3 Option (b): plan-time selection frozen in the RunSpec

The planner (forge's own pre-gate LLM step) proposes `harness` + `model` + a budget class **within the project's preference list**; the proposal is frozen into `backend_config`; the human gate sees and approves it.

- **Pros [inference]:** perfectly aligned with the iron principles — the RunSpec stays immutable (dispatch inputs from the document, ADR-0018); the gate now authorizes the execution *shape*, strengthening ADR-0009; selection happens once, pre-spend, inside forge's journal (admission-before-spend preserved); the classification signal is the plan itself — files touched, work-package class (v0.7 `allowed_paths`, dependency-upgrade/migration profiles) — no per-prompt router, no learned model, fully deterministic given the plan.
- **Cons [inference]:** the planner can misjudge (mitigated: the human sees the choice at the gate); the planner prompt is new LLM surface that must sit under the F23 evidence policy and the F22 budget guard; ADR-0015 says selection is "per-project configuration" — resolved because the planner may only choose *within* the configured list: the project config stays the authority over what is allowed, the plan picks among the allowed. A project that pins one harness grants the planner no freedom, which keeps tighten-only intact.

### 2.4 Option (c): dispatch-time availability/fallback

At dispatch, if the selected harness cannot start — credential missing/expired in the project's CI variables, the driver's lane failing `harness_infrastructure` repeatedly, pipeline trigger rejected — move down the **frozen** list.

- **Pros [inference]:** recovers quota/auth outages without human toil; the signals already exist (`_HARNESS_INFRASTRUCTURE_PATTERNS`, `harness_timeout`, job `failure_reason`); the LiteLLM shape (allowed_fails → cooldown → ordered fallback) is a proven policy formulation; CI-native routing offers no analogue, but forge's lanes are triggered per run, so forge *is* the router here.
- **Cons and the ADR-0018 trap [inference]:** a fallback that is not in the spec violates "executes its spec." The consistent design: the whole ordered list is frozen in the RunSpec at gate time, and dispatch-time fallback is defined as **"execute the spec's next entry"** — allowed only for `infrastructure`-kind failures, only *before any candidate exists* (a candidate has exactly one producer), journaled (`harness_fallback_from`/`to` in evidence), surfaced in the evidence comment, and the F22 reservation must be cancelled and re-reserved for the new driver (cost models differ; unknown ≠ zero still holds). Without project policy enabling it, the honest default is the CI-native one: fail visibly and let a human re-`/go`.

### 2.5 Option (d): repair-leg switching

When the quality contract fails and a repair cycle starts (the `commit_cycles` budget exists today), allow the repair leg to run on a different harness from the frozen list (e.g. implementation on `opencode`, repair on `claude-code`).

- **Pros [inference]:** a repair is a fresh candidate leg with its own brief and attempt base (`FORGE_ATTEMPT_BASE` = last verified candidate, ADR-0016 §4), so switching between cycles does not break the one-candidate-one-producer rule; different failure modes benefit from different executors; ADR-0021's best-of-N verification semantics already contemplate competing candidates.
- **Cons [inference]:** attribution needs care (answerable — candidate meta already records `driver` per leg); cost shifts mid-run (re-reserve per cycle); forge's repair path today re-triggers the *same* harness pipeline with the bounded repair context appended (`service.py` repair path), so this is real new plumbing. Never switch harness *within* a cycle after a candidate exists — between cycles only, and per-project opt-in.

### 2.6 Option (e): telemetry-driven ranking

ADR-0021 §3 already ships the acceptance telemetry ladder (candidate → CI-passed → ready → merged-without-rework) and v0.8 delivery metrics (work-package lineage). Add harness+model as a dimension: per project and work-package class, compute merge-without-rework rate, `harness_infrastructure` failure rate, mean cost (usage ledger, ADR-0013), mean wallclock — and rank the preference list.

- **Pros [inference]:** forge's own ladder is a *better objective function* than Chatbot Arena preferences — it measures the actual outcome forge cares about, on data forge already collects; no router training, no proxy metric.
- **Cons [inference]:** per-project sample sizes are small; automatic (bandit-style) reordering would silently change execution policy and invalidate or bypass gates — so ranking must be a **suggestion surface** (`forge doctor`-style finding), applied by a human as a tighten-only config change that flows into future gates via the policy digest. Cross-project aggregation without project consent is rejected (§5).

Sources: docs/adr/0009, 0011, 0015, 0016, 0018, 0021; src/forge/runs/backends.py (`build_backend`, `_HARNESS_INFRASTRUCTURE_PATTERNS`, `HarnessFailureKind`); src/forge/runs/service.py (`_plan_comment`, `_build_run_spec_document`, `_policy_digest`, repair path); src/forge/runs/github_service.py (`_harness_driver`, `_build_run_spec_document`, Actions inputs); src/forge/durable/budgets.py (F22 reserve/reconcile); docs/research/2026-09-13-harness-interfaces.md; docs/research/2026-09-15-harness-config-best-practices.md §4, §7, §8 [observed].

---

## 3. Recommendation — phased rollout

### v0.9: "declared preference, plan-frozen, gate-visible" (no runtime intelligence)

1. **Preference list in project config.** The scalar backend value becomes an ordered list (env `FORGE_HARNESS_PREFERENCE` / the `.forge.yml` backend block), tighten-only; length-1 list reproduces today's behavior byte-for-byte [inference].
2. **RunSpec grows three fields.** `backend_config.harness` (selected driver), `backend_config.harness_fallbacks` (frozen ordered subset of the project list; default `[]`), `backend_config.budget_class` (planner's cost/complexity estimate, e.g. `trivial | standard | heavy`). The policy digest binds the list + fallback policy, so changing them invalidates pending gates — exactly like today's `harness_model` already does [inference, consistent with observed `github_service.py` `_policy_digest`].
3. **Planner proposes within the list.** The planner may select any entry (and set `budget_class`); it must attach a one-line reason that lands in the RunSpec and the plan comment. A trivial plan may select the cheapest allowed harness; a migration profile may select the strongest. If the list has one entry, the planner has no freedom [inference].
4. **The plan comment shows the execution shape.** Add an "Implementation" block to `_plan_comment`: selected harness + model, fallbacks if any, budget class, commit cycles — so `/go` authorizes the execution shape, not just the plan (this *strengthens* ADR-0009: today the harness is invisible at the gate) [inference].
5. **Dispatch-time fallback: off by default, spec-contained when on.** Enabled only by project policy; only for `infrastructure`-kind failures; only before any candidate exists; only down the frozen list; journaled + evidence-commented; F22 reservation cancelled and re-reserved per switch. Otherwise fail visibly (the CI-native posture) and let a human re-`/go` [inference].
6. **Repair stays on the selected harness** in v0.9 [inference].

### v1.0+ (gated on telemetry volume)

- **Repair-leg switching**, per-project opt-in, between cycles only (§2.5).
- **Telemetry-driven ranking as a suggestion surface:** `forge doctor` reports "on dependency-upgrade packages in this project, opencode merged 4/5 without rework vs claude-code 1/5" — a human applies the reorder as config; the ladder from ADR-0021 §3 is the metric; the usage ledger is the cost feed [inference].
- **Planner estimate calibration:** validate `budget_class` against realized usage receipts to close the F22 loop (and to catch systematically wrong estimates per harness).

### Explicitly out of scope

- Per-tool-call or per-turn routing inside one run.
- Provider-side "Auto" tiers as a default model route.
- Any harness/model decision taken after the gate that is not an execution of the frozen spec (the fallback list *is* part of the spec; anything beyond it is not).

Sources: same forge internals as §2; docs/research/2026-09-15-harness-config-best-practices.md §7 item 8 (RunSpec-sourced model pins, `fallbackModel` adoption) [observed].

---

## 4. Risks and mitigations

- **Cost unpredictability** (mixing subscription and metered lanes; planner mis-estimation; Copilot multipliers) [inference]: `budget_class` in the spec + plan-comment disclosure; F22 reservation stays authoritative (reserve → dispatch → reconcile; unknown ≠ zero); per-harness/per-model cost estimate table maintained in factory config (never per-repo); lanes without receipts (opencode/copilot) keep honest `usage: null` accounting and therefore favor conservative budget classes.
- **Flapping** (a harness alternates across runs because health signals are noisy) [inference]: fallback triggers only on `infrastructure`-kind classification (auth/quota/timeout patterns — deterministic string/failure-reason matching, not heuristics), with LiteLLM-style hysteresis (N recent failures within a window before entry 1 is skipped); every fallback journaled; no automatic re-ranking of the project list (ranking is human-applied).
- **Non-reproducible runs** (same issue → different harness on two runs) [inference]: redefine reproducibility as *same RunSpec → same selection* — the spec freezes the list, the selection, and the fallback policy at gate time; ladder telemetry and candidate meta record `driver` per candidate so comparisons stay attributable; a config change is a new policy digest and a new gate (existing ADR-0009/0018 mechanics, no new machinery).
- **Credential sprawl** (four drivers × per-project keys) [inference]: ADR-0015's rule unchanged — credentials live only in project CI variables; forge stores none; a project's preference list may only contain harnesses whose credentials it onboarded; dispatch-time fallback checks credential *presence at the CI layer* (the pipeline either has the variable or the job fails infrastructure), never forge-held secrets; token-rotation docs cover each driver's bundle (grok's long-lived OAuth refresh token especially, HCBP §6).
- **Plan-comment fatigue** [inference]: one extra block of four lines; the alternative — an approver authorizing an execution shape they cannot see — is strictly worse under ADR-0009.

---

## 5. Explicitly rejected options

1. **Dispatch-time LLM-based routing** (a router model picks the harness per run at `/go` or dispatch). Spends tokens to decide, is non-deterministic, and places the decision outside forge's config boundary — the exact inverse of ADR-0011/0018. Even RouteLLM, the best-in-class learned router, is serving-time optimization over chat preferences, not an approval-bearing decision procedure; OpenRouter `:auto` and Copilot auto show where provider-side versions of this end up: "a different model on every turn," post-hoc visibility [documented facts; rejection is inference].
2. **Routing inside a run** (switching harness mid-candidate, or per tool call). Breaks the candidate model (one frozen attempt base → one candidate → one producer, ADR-0016), the usage ledger's driver attribution, and the brief's single output contract (HCBP §2). A candidate must have exactly one producer; repair-leg switching between cycles is the sanctioned form of multi-harness participation in one run.
3. **Global (cross-project) auto selection** without per-project config. Violates ADR-0011 (config never delegates security downward) and ADR-0015 ("backend selection is per-project configuration"). A factory-supplied default *list* is acceptable only as something projects may tighten — never as an override of project config.
4. **Provider-side "Auto" (Copilot tier routing, OpenRouter `:auto`) as the default model route.** Silent provider-side choice, non-deterministic per turn, only post-hoc visibility, billing follows the provider's pick. Permitted only as an explicitly configured, gate-disclosed per-project opt-in where the human approves "model is non-deterministic within this tier"; forge's default remains explicit pins (consistent with HCBP §4: pin snapshots per phase for reproducibility).
5. **Learned routers (RouteLLM-style) trained on forge's own runs, now.** Per-project sample sizes are tiny and the label (merged-without-rework) arrives in days-weeks, not per-request; and an automatically-acting router is rejected under §5.1's logic even when the training data is forge's. Revisit after v1.0 telemetry exists, as a suggestion surface only.

Sources: lmsys.org/blog/2024-07-01-routellm/; openrouter.ai/docs/guides/routing/routers/auto-router; docs.github.com/en/copilot/concepts/models/auto-model-selection; docs/adr/0011, 0015, 0016, 0018; docs/research/2026-09-15-harness-config-best-practices.md §2, §4 [observed].
