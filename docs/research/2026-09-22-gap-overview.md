# Gap-analysis overview — what forge lacks next (2026-09-22)

> Master summary of the 2026-09-22 gap-analysis research pass. One file
> per topic, each with findings, links and ranked recommendations:
>
> 1. [2026-09-22-gap-agent-sandboxing.md](2026-09-22-gap-agent-sandboxing.md) — gVisor/Firecracker/bubblewrap, what Devin/Codex/Cursor/Claude runners actually use, lane recipes
> 2. [2026-09-22-gap-prompt-injection-steering.md](2026-09-22-gap-prompt-injection-steering.md) — defense layers for untrusted context + the live steering bridge
> 3. [2026-09-22-gap-durable-agent-sessions.md](2026-09-22-gap-durable-agent-sessions.md) — claude resume/fork, codex rollouts, opencode export/import, orchestrator patterns
> 4. [2026-09-22-gap-cost-accounting-budgets.md](2026-09-22-gap-cost-accounting-budgets.md) — cache tokens, USD budgets, OTel GenAI, LiteLLM fences, mid-run stops
> 5. [2026-09-22-gap-byok-multitenant.md](2026-09-22-gap-byok-multitenant.md) — per-project model keys, CMEK, audit export, enterprise procurement bar
> 6. [2026-09-22-gap-runner-scheduling.md](2026-09-22-gap-runner-scheduling.md) — ARC/GitLab-autoscaler fleets, WFQ/aging/priority, safe preemption
>
> Confidence marks inside the topic files: **[documented]** authoritative
> source (linked) · **[observed]** demonstrated live/measured ·
> **[inference]** this pass's synthesis for forge.

## Where forge stands

Proven live as of 2026-09-21: three providers end-to-end, four batch
harness drivers, **real interactive-driver clients** (claude-agent-sdk,
codex app-server, opencode serve) with turn/steering/interrupt verified
against vendor binaries, the DriverMatrix seeded from live evidence, and
a durable core proven under SIGKILL failure injection. The budget ledger
(reserve-before-dispatch, unknown ≠ zero, idempotent receipts) is ahead
of industry practice. [observed from repo]

What the research shows is missing is concentrated in five clusters:
**isolation of the lane itself**, **injection defense under steering**,
**session portability across runners**, **cost/telemetry denominations**,
and **multi-tenant/enterprise packaging**. None of them is solved by the
durable controller; all of them become load-bearing as the adaptive lane
(forge-owned processes, hours-long runs, enterprise buyers) replaces
provider-CI-only execution.

## The ranked master list

Ranked by (impact on the adaptive-lane roadmap × urgency given what is
now live) ÷ effort. Each entry points into its topic file for evidence
and the full recommendation set.

### 1. Lane sandbox hardening profile + gVisor tier (topic 1)

**The gap**: lanes isolate *credentials*, not *kernels*; forge has no
positive isolation recipe for the docker-executor class its customers
actually run, and no answer for forge-owned interactive lanes beyond
"no docker socket". Every major platform ships L2+ isolation for agent
code (gVisor at Anthropic/OpenAI/Google; Firecracker at Lambda/Fly/Vercel/
E2B); Codex's enterprise blueprint (Dev Container + bubblewrap, two-phase
network) and Claude's self-hosted runner hardening (per-session containers,
minted credentials, egress default-deny, repo-settings guard) are the two
closest public recipes.
**First moves**: hardened lane-profile-v2 CI templates (cap-drop, read-only
fs, non-root, metadata-endpoint block, egress allowlist) + opt-in gVisor
`runsc` runtime behind a DriverMatrix-style compatibility gate.
See [agent-sandboxing §recommendations](2026-09-22-gap-agent-sandboxing.md#concrete-recommendations-ranked-by-effortimpact).

### 2. Prompt-injection defense-in-depth for steering + untrusted context (topic 2)

**The gap**: `/steer`, `/answer` and issue-text ingestion are live; the
authority side is enforced (no grants via steering, revision gate for
material changes) but the *content* side has no defense layers — no
provenance marking of untrusted text, no egress/output choke point, no
injection regression corpus. The evidence says this is the one place
where cheap measures have outsized, measured effect (spotlighting
>50 %→<2 % ASR pre-adaptive; output filtering the only zero-leak layer;
metadata-gated policy 0/240 unsafe at 99 % usefulness).
**First moves**: egress allowlist + output filtering at the lane boundary;
randomized provenance envelopes for issue/repo/tool content; an
injection-canary suite next to the failure-injection suites.
See [prompt-injection-steering §recommendations](2026-09-22-gap-prompt-injection-steering.md#concrete-recommendations-ranked-by-effortimpact).

### 3. Durable-session portability: vendor session state in the checkpoint (topic 3)

**The gap**: the P2 gate demands "paused job resumed on another runner";
today there are live smokes for turns/steering/interrupt but no
cross-runner resume and no per-driver recipe for carrying native session
state. The vendor mechanics are now well mapped — claude sessions are
cwd-indexed JSONL files (move file + identical cwd), codex has rollouts +
SQLite index + headless `codex exec resume` + fork modes, opencode has
`export`/`import` JSON — and each has a gotcha (path indexing, CODEX_HOME,
per-DB keying) that a checkpoint recipe must pin.
**First moves**: stable lane workspace paths; three tested
checkpoint-recipes (one per driver) with a kill-and-resume-on-second-runner
contract test; compact-before-checkpoint policy.
See [durable-agent-sessions §recommendations](2026-09-22-gap-durable-agent-sessions.md#concrete-recommendations-ranked-by-effortimpact).

### 4. Cost/usage accounting: cache tokens, USD, OTel GenAI, mid-run stop (topic 4)

**The gap**: the reserve/reconcile machinery is excellent but denominated
in raw calls/tokens: no cache-token classes (Anthropic 1.25×/2× write,
0.1× read; OpenAI 50 % auto — agent fleets run 100:1 input:output with
>90 % cache hits), no USD budgets, no OTel GenAI export (enterprise
observability composes nowhere), and no path from `budget_block_reason()`
to interrupting a live interactive turn.
**First moves**: cache + USD dimensions on `llm_calls`/`run_budgets`;
wire budget exhaustion to the driver interrupt + checkpoint; OTel spans
from exactly one boundary.
See [cost-accounting-budgets §recommendations](2026-09-22-gap-cost-accounting-budgets.md#concrete-recommendations-ranked-by-effortimpact).

### 5. BYOK per-project model keys + audit export / enterprise pack (topic 5)

**The gap**: forge is single-tenant per install with env-configured model
keys. Enterprise buyers (64 % already hold direct provider contracts)
evaluate the control plane: key custody, SIEM-exportable audit, scope,
merge gate, SOC 2 artifacts. forge's journals contain the audit content
but there is no export surface, no per-project key binding, no
per-tenant encryption of checkpoints/artifacts (which hold customer
source), no compliance artifact pack.
**First moves**: map `CredentialBinding.credential_ref` → LiteLLM virtual
keys (per-project credential + USD cap, fail-closed without fallback);
JSONL/syslog audit export aligned with the IETF Agent Audit Trail
vocabulary; compliance pack assembled from existing docs.
See [byok-multitenant §recommendations](2026-09-22-gap-byok-multitenant.md#concrete-recommendations-ranked-by-effortimpact).

### 6. Fair scheduling / admission control for agent runs on few runners (topic 6)

**The gap**: provider CI hides this today; forge-owned adaptive lanes
won't. Hours-long agent runs saturate small pools on concurrency before
CPU; one project's burst must not starve another's `/implement`. The
mechanism set is settled practice — WFQ weights + aging, class tiers with
p95-wait SLOs, fairness reason codes, ARC/GitLab-autoscaler fleet recipes
for capacity — and safe preemption for agent work *must* route through
forge's pause/checkpoint protocol, never scheduler SIGKILL.
**First moves**: fleet recipes + queue-wait doctor checks; dispatch-side
admission control (works for both provider-CI and forge-owned lanes);
scheduler↔pause-protocol contract before any preempting priority tier.
See [runner-scheduling §recommendations](2026-09-22-gap-runner-scheduling.md#concrete-recommendations-ranked-by-effortimpact).

### 7–9. Cross-cutting items found during the pass (no dedicated file)

- **7. Vendor-version drift management** [inference]: the 2026-09-21 live
  smokes found six defects where vendor behavior diverged from docs
  (opencode v2.0.10's rewritten API being the largest). The DriverMatrix
  cites evidence per binary version; extend the practice with scheduled
  re-smokes (a nightly `--e2e` matrix) before drift becomes production
  surprise. Cheapest insurance in this list.
- **8. Idle-lane hygiene as measured behavior** [inference]: gate-wait and
  question-wait must release runner capacity (teardown + restore), the
  analog of Claude runners' `--release-idle-session-min` /
  `--kill-session-after-min`; fold into the adaptive pilot's metrics
  (topic 6, move 5).
- **9. Journal-offload for large payloads** [inference]: 3,000-turn agent
  sessions will bloat Postgres journals; adopt the Temporal payload-codec
  pattern (digests in journal, bodies in the content-addressed store)
  (topic 3, move 6; topic 4's single-instrumentation rule pairs with it).

## What the research says forge should *not* do

- **Do not adopt Temporal/LangGraph as a second authority engine** — the
  runbook's decision record holds; every orchestrator pattern forge needs
  (signals, awakeables, update handlers, payload codecs) is portable onto
  the existing Mailbox + epoch design. [documented patterns, inference
  conclusion]
- **Do not chase model-level injection immunity** — the multi-lab
  adaptive-attack results (>90 % bypass of published defenses) say spend
  the effort on architecture: envelopes, gates, egress. [documented]
- **Do not build scheduling before lanes are forge-owned or pools are
  shared** — dispatch-side admission control first; it is capacity-agnostic.
  [inference]
- **Do not treat vendor badges (SOC 2 etc.) as the bar** — buyers read the
  *scope section*; forge's honest guarantee-matrix discipline is the right
  shape, it needs the export/artifact surfaces to match. [documented]
