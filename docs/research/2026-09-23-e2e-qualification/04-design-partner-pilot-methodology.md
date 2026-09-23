# Design-partner pilot methodology for developer tools — research (2026-09-23)

> E2E-qualification research for forge, topic 4 of 6. Sources:
> design-partner program guides (2026), paid-pilot contract practice,
> infrastructure-pilot field courses, developer-productivity
> measurement frameworks (DORA/SPACE/DevEx/DX Core 4), AI-coding-agent
> production field data; fetched 2026-09-23. Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

OPS-07 / R32-21 asks for a bounded design-partner pilot with explicit
success criteria: one business slice, one writable repo first, a cost
ceiling, human decision owners, expansion tied to observed exit
criteria "rather than repository-count marketing". The 2026 dev-tool
playbook is unusually converged on how to run this: paid, short,
single-outcome, evidence-collecting, with stop conditions agreed
before kickoff. It is also converged on the metrics trap — PR counts
and acceptance rates measure the wrong thing — which matters because
forge's pilot will be full of agent-authored PRs by construction.

## Findings

### 1. What a design partner is (and is not), and cohort size

[documented] ([design partner program guide](https://www.koji.so/docs/design-partner-program),
[design partner offer structure](https://tracsio.com/articles/design-partner-offer-b2b-saas),
[design partners for startups](https://bowora.com/guides/design-partners-for-startups))

- The taxonomy is now standard: **design partner** (3–12 months,
  contractual co-development, roadmap influence not veto) ≠ **beta
  user** (free, loose) ≠ **pilot customer** (30–90 days, paid,
  working product) ≠ friendly user. The defining trait is **mutual
  commitment**: 1–4 h/month synchronous + async feedback, in exchange
  for shipping-to-them-first and incorporating feedback.
- **Cohort size: 5–15, most programs land at 7–10.** Fewer than 5 =
  over-fit to one workflow; more than 15 = "the relationship degrades
  into a glorified mailing list". Partners should sit in the *same*
  persona with slightly different workflows "so cross-partner pattern
  matching reveals what's universal vs idiosyncratic".
- Qualification screen (4-for-4): problem described unprompted,
  **existing workaround they pay for** ("best leading indicator"),
  decision authority, real time willingness. "Three pilots with the
  same architecture and buying motion are evidence for repeatability.
  Three unrelated consulting projects are evidence that the wedge is
  still too broad." ([inference-company founder field course](https://html-docs.com/site/inference-company-founder-field-course/design-partners-and-company))
- One-page agreement, not an MSA: time commitment, feedback channels,
  pricing, roadmap vote (not veto), confidentiality, **exit clause
  (30 days, no penalty)**. "Do not manufacture legal consequences
  around a failed pilot… treat the design partner agreement like a
  learning deal." ([what is a design partner](https://dowhatmatter.com/guides/design-partner))

### 2. The pilot contract: scope, fee, success criteria, decision rule

[documented] ([paid pilot contract](https://jetthoughts.com/course/tech-for-non-technical-founders-2026/paid-pilot-charge-before-ship),
[design partner agreement terms](https://dowhatmatter.com/guides/design-partner-agreement))

- **Payment is the strongest demand test**: "Free feedback shows what
  people will discuss. Payment shows what they will prioritize."
  Typical: a deposit of 10–30 % of year-one ACV before kickoff,
  credited on conversion; the pilot fee "tests urgency and pays for
  focused learning. It does not need to optimize the final margin
  model."
- The infrastructure-pilot reference structure (closest to forge's
  shape): **one workload, a frozen baseline, representative traffic,
  a 4–8-week window, weekly operator access, security boundaries,
  success metrics, a fee, and a production conversion decision.**
  Week 1 reproduce baseline → week 2 deploy → week 3 shadow traffic
  + game day → week 4 final benchmark + security packet + decision.
  "If one named gap remains, extend once with an explicit decision
  date. Otherwise stop."
- Success criteria written as **2-of-3 measurable criteria by a named
  end date**, with auto-conversion unless opted out, and **walk-away
  defined if not met**. Operational commitments look like: "these
  three named users will complete this specific workflow, and we will
  measure it in our system" — never "use the product regularly."
- The decision vocabulary that keeps pilots honest: **expand, extend
  once for one named gap, or stop** — each with documented learning.
  "Free open-ended pilots hide urgency, encourage custom work without
  commitment, and make it impossible to distinguish learning from
  unpaid support."
- Stage-appropriate scoping for AI-agent pilots specifically:
  week 1 read/planning/small tests only; "distribution, DB migration
  and access to confidential information are excluded"; week 2 three
  real bugs/refactors to branch+PR; judged on "failure reproduction,
  CI passage, number of review modifications, and revert time" —
  not just merged-PR count. ([2-week AI-tool pilot guide](https://aq-score.com/blog/ai-accelerated-sdlc-pipeline-architect-led-guide-2026))

### 3. Escaping feedback theater

[documented] ([design partner expectations](https://dowhatmatter.com/guides/design-partner),
[technical founder's guide to pilots](https://www.amplifypartners.com/blog-posts/the-technical-founders-guide-to-pilots-and-pocs))

- "Do not ask what they think of a feature; study their past behavior
  and actual workflows… 'Show me how you handled this reporting step
  last week.'" Missing-feature complaints are a *positive* signal —
  "in most cases customers simply don't care enough to complain."
- **Silence is not validation**; objections must be actively
  extracted at the cadence. Track whether the same pain/trigger/outcome
  repeats across partners — "if feedback differs wildly across
  partners, the segment is probably too broad."
- Promise discipline: do promise early access, founder involvement,
  fast blocker handling, honest maturity visibility, a fair commercial
  path. Do not promise every requested feature, unlimited custom
  work, or "outcomes you cannot yet prove." "Early customers can
  handle rough edges when expectations are clear. They get frustrated
  when the founder sells maturity and delivers experimentation."

### 4. Metrics that matter — and the ones that lie

[documented] ([DORA/SPACE/DevEx/Core 4 overview](https://es.nl/2026/how-to-measure-developer-productivity-dora-space-devex-dx-core-4),
[AI developer productivity field guide](https://snowmanlabs.com/insights/how-to-measure-ai-developer-productivity),
[engineering productivity metrics 2026](https://snowmanlabs.com/insights/engineering-productivity-metrics))

- The stack: **DORA** (deployment frequency, lead time, change
  failure rate, recovery time — outcome-level, hard to inflate),
  **SPACE** (multi-dimensional; never one number), **DevEx** (cognitive
  load/flow via survey), **DX Core 4** (speed/effectiveness/quality/
  impact — "oppositional metrics: gaming one visibly degrades
  another"). The shared rules: **measure the system, not the person**;
  pair every speed metric with a quality counterweight; expect
  Goodhart's law; freeze a 60–90-day pre-pilot baseline.
- Under AI the activity metrics **invert**: "code volume, commit
  counts, suggestion throughput become nearly free to inflate — more
  code is now as likely a liability signal as a productivity signal."
  Suggestion-acceptance rate is "a vendor engagement metric; keep it
  out of leadership decks." Segment everything by code provenance
  (human / AI-assisted / agent-authored) from day one — retrofitting
  provenance is close to impossible.
- The perception gap is documented and large: METR's RCT found
  experienced developers **19 % slower with AI while believing they
  were 20 % faster**; self-reported time savings is disqualified as a
  headline number, corroboration by telemetry required. The 2025 DORA
  report frames AI as an **amplifier of the surrounding system** —
  "individual speedups pool at the next bottleneck (usually code
  review) and can surface as instability rather than throughput."
- The five metrics that hold up for agent fleets, read together:
  **autonomy rate** (merged-as-is ÷ agent-touched PRs — watch for
  rubber-stamping), **cost per merged PR**, **defect/rollback rate**
  on agent-authored PRs (with long observation windows — "quality
  lags speed by 8–12 weeks"), **intervention rate** (count partial
  steering, not just full takeovers), **cycle time vs a current
  baseline**. "A low cost-per-PR with a high rollback rate is not a
  win; it's deferred cost." ([warp: metrics that prove agents work](https://warp.dev/articles/metrics-that-prove-ai-coding-agents-are-working))
- Field benchmarks for expectations-setting: GitHub/Accenture RCT
  (+8.7 % PRs/developer, +11 % merge rate — "only when the gating
  discipline holds"); Devin's observed merge rate 34 % → 67 % over 18
  months (one in three PRs still rejected); SWE-bench Verified > 70 %
  for frontier models vs ~23 % on the contamination-resistant Pro
  split — **"the benchmark-to-merge gap"** is the distance between a
  passing harness score and a PR your product manager actually asked
  for. ([benchmarks vs production](https://lovex.dev/blog/ai-agent-benchmarks-vs-production))
- Reviewer-load is the hidden tax: "AI often reduces routine work but
  increases reviewer scrutiny — measure senior-engineer minutes per
  PR"; PR review times up 441 % YoY in one 2026 dataset. The J-curve
  warning: initial dip in cycle time + bump in defect/reviewer load
  "is where many pilots were killed. Successful orgs pre-committed to
  6- and 12-month measurement windows." ([enterprise AI ROI pilot-to-production](https://chatgptaihub.com/from-pilot-to-production-enterprise-dev-orgs-ai-roi-story))

### 5. Graduation criteria

[documented] ([design partners: find, contract, exit](https://bowora.com/guides/design-partners-for-startups))

- "Graduate to standard pricing when the same workflow works for a
  second partner without heroics." Success looks like: partner runs a
  core workflow weekly, asks for fewer custom exceptions, agrees to a
  commercial path with a date. Track **time-to-first-value** and
  tasks completed in-product, "not meeting count alone."
- Separate **segment-wide blockers from one-off requests** explicitly
  — the input to roadmap decisions, and the boundary that prevents
  "custom-build hell".

## Concrete recommendations (ranked by effort/impact)

1. **Write the forge pilot as a one-page learning contract with the
   2-of-3 rule (low effort, high impact).** One business slice; one
   writable repo, expansion to 2–3 only after the first gate (already
   the architecture plan's stance); frozen baseline; cost ceiling;
   named decision owners; **2-of-3 measurable criteria by a named end
   date with expand / extend-once-for-a-named-gap / stop** as the only
   exits. Include the staged capability ladder (read/plan → small
   fixes → branch+PR → gate) rather than full autonomy from week 1.
   [documented, implements OPS-07/R32-21]
2. **Freeze a pre-pilot baseline and instrument provenance before
   kickoff (medium effort).** 60–90 days of the partner's DORA four +
   PR cycle time by stage + quality indicators, with agent-authored
   vs human work labeled at creation. Without the baseline and
   provenance tags, none of the five holding-up metrics are
   computable later. [documented]
3. **Score the pilot on the five-agent metrics + reviewer load, not
   PR counts (low effort once instrumented).** Autonomy rate, cost
   per merged PR, defect/rollback rate on agent PRs, intervention
   rate (counting steering, per forge's own /steer semantics), cycle
   time vs the frozen baseline — plus senior-reviewer minutes per PR
   as the explicit capacity tax. Every speed number published next to
   its quality pair. [documented; matches forge's planned
   plan-corrections/interventions/WIP/spend metrics in OPS-07]
4. **Pre-commit the measurement window to survive the J-curve
   (organizational, zero build).** Agree with the partner that weeks
   1–4 dips are expected and reviewed at 6+ weeks; the documented
   pilot-killer is judging during the adjustment dip. Pair with the
   weekly operator cadence (the "weekly operator access" rule) and
   mandatory replan + operator-pause demonstrations already required
   by OPS-07's acceptance criteria. [documented]
5. **Feedback-extraction discipline over dashboards (low effort).**
   The cadence asks behavior-anchored questions ("show me how you did
   this last week"), treats missing-feature complaints as positive
   signal, and tracks segment-wide vs one-off requests in the pilot
   log. Silence is never validation. [documented]
6. **Charge something (product decision).** Even a nominal pilot fee
   changes the signal from "willing to try" to "willing to prioritize";
   the field course and contract practice agree free open-ended pilots
   cannot distinguish learning from unpaid support. [documented]

Relationship to existing plans: operationalizes OPS-07/R32-21; the
metrics set extends the delivery-ladder metrics in the adaptive plan;
evidence packs from topic 3 become the pilot's reporting artifacts
(security packet + benchmark + economics + decision, per the
infra-pilot week-4 bundle).
