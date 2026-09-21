# Cost/usage accounting + mid-run budget enforcement — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: OpenTelemetry GenAI semantic
> conventions docs and guides, LiteLLM proxy docs, Anthropic/OpenAI cache
> pricing docs and analyses, LangSmith docs/write-ups, IETF Agent Audit
> Trail draft; fetched 2026-09-22. What forge already has — the
> reserve/reconcile budget machinery (`src/forge/durable/budgets.py`,
> ADR-0013, ADR-0018 §5) — is summarized from the tree, not re-researched.
> Confidence marks: **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

forge's budget machinery is genuinely ahead of most agent stacks:
reservations before the provider is contacted, single-statement
conditional updates (no read-modify-write races), `consumed + reserved +
unresolved` exposure where **unknown usage is never counted as zero** and
never released as headroom, idempotent usage-receipt ingestion keyed by
`(run, attempt, receipt_id)`, and episode/wall-clock gates for
non-intercepted lanes. [observed from `src/forge/durable/budgets.py`]

The gaps this research confirms:

1. **Denomination**: budgets are calls/tokens, not USD; there is no
   per-model price normalization, so "budget class" cannot express "this
   run may cost at most $4".
2. **Cache-token economics are absent**: multi-provider agent workloads
   are dominated by cached input (100:1 input:output ratios, >90 % cache
   hit rates in production agent fleets) and providers bill cache reads,
   writes and fresh input at *different multipliers* — token-count budgets
   that ignore cache classes mis-price runs by large factors.
3. **No standard telemetry export**: `llm_calls` + `/metrics.prometheus`
   are forge-internal schemas; there is no OTel GenAI emission, so forge
   runs don't compose with the observability stack an enterprise already
   runs.
4. **Mid-run enforcement for interactive lanes**: the budget gate fires at
   episode dispatch; an interactive driver mid-turn must be *interrupted*
   when the budget exhausts — the bridge from `budget_block_reason()` to
   the live interrupt surface is the missing piece.

## Findings

### 1. OpenTelemetry GenAI semantic conventions — the vendor-neutral vocabulary

[documented] ([TripleCloud guide](https://blog.triplecloud.tech/posts/instrument-llm-agent-opentelemetry),
[ai-infrastructure.net](https://ai-infrastructure.net/genai-observability-otel),
[genalphai agent spans](https://genalphai.com/agent-observability-with-opentelemetry-genai-conventions/))

- The `gen_ai.*` namespace standardizes: `gen_ai.request.model` /
  `gen_ai.response.model`, `gen_ai.usage.input_tokens` /
  `output_tokens`, finish reasons, and — the quantities that dominate
  agent bills — **`gen_ai.usage.cache_read.input_tokens`**,
  **`gen_ai.usage.cache_creation.input_tokens`** and
  `gen_ai.usage.reasoning.output_tokens`.
- Span taxonomy: `chat` / `execute_tool` / `invoke_agent` per model call,
  tool call and agent invocation; the agent-spans page (v1.41.1, May 2026)
  adds five agent operations (`create_agent`, `invoke_agent_client`,
  `invoke_agent_internal`, …). `gen_ai.conversation.id` is the grouping
  key for multi-turn sessions.
- Content capture (prompts/completions) is **opt-in via span events**, not
  attributes — PII-safe by default; attributes are indexed and size-limited.
- Stability varies: core usage attributes stable/RC (safe for dashboards);
  agent/tool spans still **Development** — names can change. Guard against
  churn with a thin mapping layer.
- Pitfall: four layers each shipping instrumentation double-counts tokens
  (an extra, plausible-looking span). Emit from exactly one layer — for
  forge that's the `LLMClient` / driver boundary.

### 2. Cache-token accounting per provider (why raw token counts mis-price)

[documented] ([Anthropic caching mechanics](https://technspire.com/en/blog/anthropic-prompt-caching-pricing-mechanics),
[provider-agnostic caching via gateway](https://truefoundry.webflow.io/blog/provider-agnostic-prompt-caching-llm-gateway),
[cost arithmetic](https://youngju.dev/blog/2026-07-17-llm-api-cost-arithmetic.en))

| Provider | Cache write | Cache read | Notes |
|---|---|---|---|
| Anthropic | **1.25×** base input (5-min TTL) / **2×** (1-hour TTL) | **0.1×** | Explicit breakpoints; min prefix 1,024 (Sonnet/Opus) / 2,048 (Haiku) tokens; usage fields `cache_creation_input_tokens`, `cache_read_input_tokens` (+ per-TTL breakdown) |
| OpenAI | not billed | **0.5×** (50 % off) | Automatic, longest-repeated-prefix, 128-token increments, ≥1,024 tokens; single `cached_tokens` under `prompt_tokens_details`; no write-side counter |
| GLM (via gateway) | — | — | Forge routes GLM through LiteLLM/the z.ai Anthropic gateway; cache fields must be normalized from whatever the route surfaces — treat missing cache fields as *unknown*, consistent with forge's unresolved-counter doctrine |

[observed] Realistic effect: Anthropic's own worked example — 50 k input /
15 k output session where 40 k input becomes cache reads — cuts input cost
72 % but **total** cost only 25.5 %, because output tokens price at 5×
input. Production agent fleets report input:output ratios passing 100:1
with > 90 % cache hit rates. A forge budget that counts "tokens" without
cache classes cannot predict either the bill or fair budget classes.
[documented numbers, observed fleet ratios]

### 3. Gateway-side budgets: LiteLLM virtual keys (forge already runs LiteLLM)

[documented] ([LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys),
[budget alerts](https://mintlify.wiki/BerriAI/litellm/proxy/budget-alerts),
[per-user budgets walkthrough](https://dreaming.press/posts/how-to-set-per-user-llm-budgets-litellm-virtual-keys.html))

- `/key/generate` mints virtual keys with `max_budget` (USD),
  `budget_duration` (`s/m/h/d`), model allowlists, rpm/tpm limits, team /
  user / end-user attribution; spend is tracked per key/user/team in
  Postgres and queryable via `/key/info`, `/spend/logs`, `/spend/teams`.
- Enforcement is **fail-closed**: over-budget requests get
  `ExceededTokenBudget` before reaching the model. Temporary raises exist
  (`temp_budget_increase` + expiry).
- Honest limitation: **spend is accounted post-call** — one huge request
  can overshoot slightly before the next is blocked; rate limits are the
  mitigations. (forge's reserve-*before*-dispatch model is stricter; the
  two compose — reserve in forge, enforce in LiteLLM as a second fence.)
- Alerting (Slack/SMTP/webhook) at thresholds; budget auto-reset per
  duration.

### 4. What LangSmith does at the observability layer (feature bar to match)

[documented] ([LangSmith in production](https://abhishekchauhan.it/blog/langsmith-production-observability-evaluation-debugging))
Trace trees per run (LLM calls, tool invocations, state transitions) with
token/cost breakdown split into input (incl. cache reads), output (incl.
reasoning tokens) and other; per-thread conversation rollups; online
evaluators running rules on production traces (PII, injection attempts,
schema validation); annotation queues; and (2026) LangSmith Engine —
automatic clustering of production failures into named issues.
[observed] The incident genre this exists for: an 11-day four-agent loop
that cost $47,000 unnoticed; a misbehaving session burning $48 k of GPT-4o
in 14 hours; a large enterprise hitting ~$2,000 per engineer per month.
([agent-ops write-ups](https://dev.to/max_quimby/agent-ops-is-eating-the-agent-stack-1jo))

### 5. Audit-side standardization: IETF Agent Audit Trail (and the agents.json clarification)

[documented] ([draft-sharif-agent-audit-trail](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/01))
An emerging IETF draft: JSON records with mandatory agent identity, action
classification (`tool_call`, `decision`, `delegation`, `escalation`,
`error`, `lifecycle`), outcome + trust level, **tamper-evident SHA-256
hash chaining** and optional ECDSA signatures; explicitly mapped to EU AI
Act logging duties, SOC 2, ISO 42001. 
[note] "agents.json" in the observability context resolves to **two
unrelated web-discovery specs** (`wild-card-ai/agents-json` on OpenAPI/
Arazzo, and site-manifest drafts) — neither is an observability standard
([agentswelcome protocol atlas](https://agentswelcome.dev/protocols)).
The standards that matter for forge here are **OTel GenAI** (telemetry)
and **AAT** (audit); don't build against agents.json for this purpose.

## Concrete recommendations (ranked by effort/impact)

1. **Cache-token + USD dimensions on `llm_calls` and `run_budgets`
   (medium effort, high impact).** Extend the usage schema with
   `cache_read` / `cache_creation` (or `cached_tokens`) and a per-model
   price table (versioned, per provider/route, cache-aware multipliers)
   so budgets gain an optional USD class alongside token/call classes —
   reserve/reconcile logic unchanged, one more priced dimension. Where a
   route reports no cache fields, they land in `unresolved_*` (existing
   doctrine: unknown ≠ zero). [inference]
2. **Mid-run budget stop for interactive lanes (medium effort, high
   impact).** Wire `budget_block_reason()` into the lane controller: on
   exhaustion, dispatch the driver interrupt (the same surface the smokes
   proved), checkpoint, and mark the run budget-exhausted with evidence —
   closing the loop between the strict ledger forge already has and the
   live process it now controls. [inference]
3. **Emit OTel GenAI spans from one boundary (medium effort, medium-high
   impact).** `LLMClient` + driver adapters emit `chat` /
   `execute_tool` / `invoke_agent` spans with `gen_ai.usage.*` (incl.
   cache fields) and `gen_ai.conversation.id` = run/episode ID; content
   stays opt-in via events. Gives forge dashboards in any OTLP backend
   and matches the enterprise-export demand from the BYOK research.
   [inference]
4. **LiteLLM as the second budget fence (low effort, medium impact).**
   Where a lane's model route already goes through forge's LiteLLM, mint
   a per-run (or per-project) virtual key with `max_budget` = the run's
   USD class; correlate LiteLLM `/spend/logs` with `usage_receipts`.
   Belt-and-suspenders with forge's stricter reservation model.
   [inference]
5. **Budget alerts + spend surfaces (low effort, visible).** Threshold
   alerts (webhook/issue-comment) at 50/80/100 % of a run's budget, and
   per-run/per-project spend rollups in the evidence comment — the
   "delivery-cohort economics" the README promises, made per-run.
   [inference]
6. **Align the audit journal toward AAT shapes (low effort, deferred
   value).** Hash-chain `action_log` + control-command records and use
   AAT's action-type vocabulary where it fits — cheap now, pays in
   enterprise audits later. [inference]

Relationship to existing plans: OPS-02 ("end-to-end usage lineage and
budget reporting") and MRP-08 ("hierarchical budgets, admission and fair
scheduling") already cover the hierarchy; this research pins the missing
*denominations* (cache tokens, USD), the *export standard* (OTel GenAI),
and the *interactive-lane enforcement* seam.
