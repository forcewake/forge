# Evidence-based capability qualification — research (2026-09-23)

> E2E-qualification research for forge, topic 3 of 6. Sources:
> continuous-compliance platform practice (Vanta/Drata/Secureframe,
> 2026), FedRAMP ConMon and FedRAMP 20x KSI material, evidence-pipeline
> architecture write-ups, release-readiness gate practice, AI-system
> evidence-gate research; fetched 2026-09-23. Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

R32-19 asks for release artifacts assembled *from qualified evidence*,
and R32-14 for a way to evaluate research/qualification quality
itself. forge already has the right primitives — journals with
idempotent receipts, the capability manifest, guarantee matrices — but
"capability is qualified" currently means "a named suite passed at
some point". The security/compliance world has spent a decade
industrializing exactly this transition: from point-in-time snapshots
to **continuous, requirement-linked, tamper-evident evidence chains
that gates consume as queries**. Their mechanics port cleanly.

## Findings

### 1. Continuous control monitoring: evidence as a stream, not a snapshot

[documented] ([automate compliance evidence collection 2026](https://cipherssecurity.com/automate-compliance-evidence-collection-2026),
[SOC 2 platform comparison](https://riskpublishing.com/soc-2-compliance-automation-platform-comparison),
[Vanta vs Drata evidence compared](https://changeriskintel.com/posts/grc-evidence-vanta-drata-secureframe))

- CCM platforms (Vanta: 1,300–1,400 automated tests, hourly cadence;
  Drata: continuous checks, 24/7) poll each connected system on a
  schedule, check whether controls pass, and **store a timestamped
  evidence record per check**. "A control that passed in month eleven
  but failed in months one through ten is still an audit finding" —
  point-in-time screenshots do not prove continuous operation;
  time-series records do.
- The failure→evidence→remediation→re-evidence loop is the unit of
  governance: a control fails → ticket to the control owner → fix →
  *re-run produces a new timestamped record*. "This produces a
  complete audit trail for findings" — failure history is retained,
  not overwritten.
- Readiness is a **rolling count of passing controls vs in-scope
  controls** reviewed continuously; "remediation the day before
  fieldwork does not retroactively fix the missing evidence from the
  months before."
- Honest limitation repeatedly stated by practitioners: these
  platforms **collect and catalog evidence; they do not generate it**
  — the tests must be wired in, and "a green checkmark means evidence
  was collected; it doesn't mean the infrastructure behind it is
  sound."

### 2. FedRAMP ConMon: cadenced deliverables and the drift discipline

[documented] ([FedRAMP ConMon what it involves](https://safeguard.sh/resources/blog/fedramp-continuous-monitoring-what-conmon-really-involves),
[ConMon after ATO](https://boundera.io/blog/rev5/fedramp-continuous-monitoring-after-ato),
[ConMon deliverables guide](https://elevateconsult.com/insights/fedramp-conmon-deliverables-essential-evidence-requirements-guide-2026))

- The post-authorization operating rhythm: **monthly** authenticated
  scans + POA&M (every open finding, deadline, status) + inventory
  reconciliation; **annual** independent re-assessment of a control
  subset (all controls across a 3-year rotation) with *fresh evidence
  required — reusing previous evidence is not permitted*; **as-needed**
  significant-change requests with security impact analysis *before*
  the change.
- Inventory drift is a first-class finding: "the inventory says what
  exists, and the scans must reach all of it" — new services that
  appear but aren't in scan scope "read as loss of control." The
  forge analog: a recipe/harness/capability that exists but has no
  qualification evidence in scope.
- Remediation runs on fixed severity clocks (high 30 d / moderate
  90 d / low 180 d); late items trend for the reviewer. Scans must be
  **authenticated** and cover 100 % of the boundary — partial coverage
  is the classic first-year finding.
- **FedRAMP 20x (2025→)** replaces monthly documents with
  **machine-readable Key Security Indicators** validated continuously:
  "your authorization remains live only as long as your KSIs remain
  green"; Moderate machine-based resources validated at least every
  3 days. "Treat the monthly package as a rendering of underlying
  structured data, not the source of truth."
  ([FedRAMP 20x automation](https://boundera.io/blog/20x/fedramp-continuous-monitoring-automation))
  Three KSI families carry ~70 % of evidence weight (monitoring/
  logging/auditing, cloud-native architecture, IAM) — the "is your
  system what you say it is" families.
- The 20x automation loop worth copying verbatim: **scope → collect
  (from authoritative APIs on every change) → map (tag every artifact
  with its requirement ID at collection time — "mismatched mapping is
  the most common reason assessors reject evidence packs") → evaluate
  (deterministic, reproducible pass/fail) → route (a failed
  validation creates work, not a notification) → report (OSCAL JSON
  with integrity hashes)**.

### 3. The evidence-chain architecture: bind artifacts to stable requirement IDs

[documented] ([automating evidence collection from test results to audit package](https://us.fitgap.com/stack-guides/automating-evidence-collection-for-cta-compliance-from-test-results-to-an-audit-package))

- "We tested it" is not auditable. The four failure modes of evidence:
  produced but not *captured as evidence* (debugging-shaped, no
  metadata/retention), not *traceable to requirements*, manually
  packaged (gaps + rework), and mutable.
- The pattern: **every test run is an evidence event** — a
  standardized bundle (junit.xml, html, logs, screenshots +
  `evidence.json` metadata: build_id, commit_sha, environment, tool
  versions, executed_at/by, **requirement_ids covered**) pushed to
  immutable storage at a deterministic path
  (`{system}/{requirement_id}/{date}/{build_id}/`) with a lightweight
  index of *requirement → latest passing build*.
- **Separate storage from presentation**: raw artifacts in an
  immutable repository; the human-facing audit package (ZIP/PDF) is a
  generated *view*, assembled on demand — and generation is itself
  logged (chain of custody). Run a **monthly drill** that generates
  the package end-to-end "to prove the workflow" before the real
  audit.
- Weekly **gaps query**: requirements with no evidence in N days;
  requirements marked covered but missing required artifact types.
  Evidence freshness is a quality attribute in its own right — "a
  passing run from before a migration may be accurate about the old
  system and useless about the candidate release."
  ([quality metrics for release decisions](https://qaguardian.com/blog/software-quality-metrics-a-practice-guide-for-release-decisions))

### 4. Promotion gates that read evidence, not vibes

[documented] ([release readiness: 12 gates filled in](https://getautonoma.com/blog/release-readiness-checklist),
[evidence-driven release gates for LLM apps (arXiv)](https://arxiv.org/pdf/2603.15676v1.pdf),
[AI test evidence in CI/CD release gates](https://scrolltest.com/2026/07/12/ai-test-evidence-cicd-release-gates))

- The release-readiness pattern: twelve gates each with **owner,
  concrete pass condition, evidence source, and a three-value
  verdict: Pass / Waived / Blocked** — "Waived is a Pass a human
  accepted despite a known gap, with a reason and a name; one Blocked
  row wins the argument regardless of eleven greens." The
  go/no-go row itself records name, timestamp, and blocker reference.
- **Machine-signable vs human-signable rows**: suites, contract
  checks, rollback drills, migration dry-runs publish their own
  status; acceptance sign-off, risk waivers, threshold judgment and
  go/no-go need a name attached. "The checklist stopped being a
  document you fill in. It became a dashboard query you read."
- Research-grade version (arXiv 2026, 38 evaluation runs / 20+
  releases of a multi-agent LLM system): five empirically grounded
  dimensions (task success ≥ 80 %, context preservation ≥ 90 %, P95
  latency, safety pass rate ≥ 95 %, **evidence coverage ≥ 80 %**)
  mapped to a deterministic **PROMOTE / HOLD / ROLLBACK** decision,
  with dimensions merged when they perfectly correlate (redundant
  signal). "The gate should not rerun tests. It should evaluate
  evidence produced by earlier stages."
- AI-specific evidence pack (the shape forge's research-quality
  evaluation needs): eval-summary.json, failed-cases.jsonl,
  **dataset-version, prompt-version, retrieval-snapshot,
  tool-call-trace, human-review.md with per-build override expiry**.
  "Separate blocker failures from average quality scores — one unsafe
  action is not balanced by ninety-nine nice answers." Overrides
  expire with the build; every override creates a ticket.

### 5. Requirement→test traceability as a gate, not paperwork

[documented] ([test evidence for regulated teams](https://buildpulse.io/blog/test-evidence-compliance-ci-audit-trail))

- The pattern that closes the loop: define regulated-scope
  requirements as tags → emit the tags into JUnit properties at run
  time → **a CI step parses results and validates every required tag
  has ≥ 1 passing test, failing the build if coverage is missing** →
  retain that validation artifact alongside results. "This makes the
  traceability check part of the gate, not a separate manual
  exercise."
- Immutability mechanics at the smallest useful scale: artifact
  retention beyond the audit window (> 400 days on GH Actions,
  external S3 with Object Lock in compliance mode); upload on failure
  as well as success; names keyed by SHA + run ID so six-month-old
  retrieval is tractable.
- Flaky-test quarantines must themselves be evidence-backed: "we
  quarantined this test on this date because it had a 34 % failure
  rate over 90 days, and here's the evidence" — a defensible
  exclusion, with a documented remediation plan.

## Concrete recommendations (ranked by effort/impact)

1. **Capability manifest v2: requirement-ID-linked evidence events
   (medium effort, high impact — the R32-19 core).** Extend the
   capability manifest so every qualification assertion emits an
   evidence record tagged with a stable capability/requirement ID *at
   collection time* (the FedRAMP-20x "map at collection" rule), into
   the content-addressed store with journal receipts. Add the
   freshness rule: a capability is qualified only while its latest
   passing evidence is younger than N days or older than the last
   material change to the thing it covers. [documented patterns,
   inference composition — implements R32-19]
2. **Gaps query as a doctor check (low effort, high impact).** A
   `forge doctor --qualification-gaps` that lists: capabilities with
   no evidence in N days; capabilities marked qualified whose required
   artifact types are missing; evidence older than the artifact it
   certifies. This is the weekly ConMon gaps query transposed, and it
   is what makes "release artifacts from qualified evidence" auditable
   rather than aspirational. [documented pattern]
3. **Three-value verdicts + machine/human-signable split in the
   promotion gate (low effort).** When assembling release artifacts,
   record per-gate Pass / Waived (owner + reason) / Blocked, and
   generate the bundle as a *view* over the evidence store with a
   logged generation event (chain of custody). Never hand-curate the
   folder. [documented]
4. **HOLD band between promote and block for research/eval gates
   (medium effort — R32-14).** For research-quality evaluation,
   adopt the PROMOTE/HOLD/ROLLBACK shape with a small set of
   non-redundant dimensions, explicit thresholds, HOLD routing to a
   named human with expiry, and evidence packs that pin
   dataset/prompt versions and failed cases. Redundant metrics get
   merged, not accumulated. [documented + observed in 2026 practice]
5. **Traceability validation in CI (low effort).** The parse-and-
   validate step: every capability ID the promotion claims must
   appear with a passing test in the current evidence set, else the
   build fails. Turns the guarantee matrix into an enforced gate.
   [documented]
6. **Evidence drill (low effort, do monthly).** Monthly end-to-end
   generation of a release-evidence bundle from a cold query, timed
   and reviewed — proves the chain works before a design partner or
   auditor asks. [documented drill pattern]

Relationship to existing plans: implements the evidence side of
R32-19 and the evaluation shape of R32-14; consumes topic 1's layer
definitions and topic 2's environment runs as evidence sources; the
audit-visible output feeds the pilot reporting in topic 4.
